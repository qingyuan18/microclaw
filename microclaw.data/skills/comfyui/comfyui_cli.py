#!/usr/bin/env python3
"""ComfyUI CLI — standalone script for queuing workflows and saving outputs.

Usage:
    python comfyui_cli.py --workflow text_to_image --prompt "a cat on the moon"
    python comfyui_cli.py --workflow image_edit --prompt "make it sunset" --image /path/to/img.png
    python comfyui_cli.py --workflow image_to_video --prompt "cat waves" --image /path/to/img.png

Environment variables:
    COMFYUI_URL       — ComfyUI server URL (default: http://localhost:8188)
    COMFYUI_OUTPUT    — Output directory (default: /tmp/comfyui_output)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
WORKFLOW_DIR = SCRIPT_DIR / "workflows"
CONFIG_PATH = SCRIPT_DIR / "config.json"


def _load_skill_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


_skill_config = _load_skill_config()
SERVER_URL = os.environ.get("COMFYUI_URL", _skill_config.get("server_url", "http://localhost:8188")).rstrip("/")
OUTPUT_DIR = os.environ.get("COMFYUI_OUTPUT", _skill_config.get("output_dir", "/tmp/comfyui_output"))


# ---------------------------------------------------------------------------
# ComfyUI HTTP helpers
# ---------------------------------------------------------------------------

def queue_prompt(workflow: dict, server_url: str) -> dict:
    client_id = str(uuid.uuid4())
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(f"{server_url}/prompt", data=payload)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        raise RuntimeError(f"ComfyUI queue error (HTTP {e.code}): {body}") from e


def upload_image(image_path: str, server_url: str, filename: str | None = None) -> str:
    """Upload image to ComfyUI /upload/image, return remote filename."""
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")

    fname = filename or os.path.basename(image_path)
    mime = "image/jpeg" if fname.lower().endswith((".jpg", ".jpeg")) else "image/png"
    boundary = "----MicroClawBoundary" + uuid.uuid4().hex

    with open(image_path, "rb") as f:
        file_bytes = f.read()

    body = bytearray()
    # type field
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(b'Content-Disposition: form-data; name="type"\r\n\r\ninput\r\n')
    # image field
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="image"; filename="{fname}"\r\n'.encode())
    body.extend(f"Content-Type: {mime}\r\n\r\n".encode())
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"{server_url}/upload/image",
        data=bytes(body),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            info = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()
        except Exception:
            pass
        raise RuntimeError(f"Upload error (HTTP {e.code}): {err_body}") from e

    name = info.get("name", "")
    subfolder = info.get("subfolder", "")
    return f"{subfolder}/{name}" if subfolder else name


def poll_and_download(
    prompt_id: str,
    server_url: str,
    output_key: str,
    timeout_sec: int,
    output_dir: str,
) -> str:
    """Poll /history until output is ready, download and save file. Returns local path."""
    os.makedirs(output_dir, exist_ok=True)
    start = time.time()

    while True:
        if time.time() - start > timeout_sec:
            raise TimeoutError(
                f"ComfyUI generation timed out after {timeout_sec}s (prompt_id: {prompt_id})"
            )

        url = f"{server_url}/history/{prompt_id}"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception:
            time.sleep(3)
            continue

        history = data.get(prompt_id)
        if history is None and "outputs" in data:
            history = data

        if not history or not isinstance(history, dict):
            time.sleep(3)
            continue

        # Check execution error
        status = history.get("status", {})
        if status.get("status_str") == "error":
            msgs = json.dumps(status.get("messages", ""))
            raise RuntimeError(f"ComfyUI execution error: {msgs}")

        outputs = history.get("outputs")
        if not outputs or not isinstance(outputs, dict):
            time.sleep(3)
            continue

        for _node_id, out in outputs.items():
            if not isinstance(out, dict):
                continue

            candidates = []
            primary = out.get(output_key, [])
            if isinstance(primary, list):
                candidates.extend(primary)
            # VHS_VideoCombine compat: mp4 may appear under "gifs"
            if output_key == "videos" and not candidates:
                alt = out.get("gifs", [])
                if isinstance(alt, list):
                    candidates.extend(alt)

            for entry in candidates:
                if not isinstance(entry, dict):
                    continue
                filename = entry.get("filename")
                if not filename:
                    continue
                subfolder = entry.get("subfolder", "")
                otype = entry.get("type", "output")

                file_url = (
                    f"{server_url}/view?filename={filename}"
                    f"&subfolder={subfolder}&type={otype}"
                )
                with urllib.request.urlopen(file_url, timeout=120) as f_resp:
                    file_bytes = f_resp.read()

                ext = "mp4" if output_key == "videos" else (
                    "jpg" if filename.endswith((".jpg", ".jpeg")) else "png"
                )
                local_name = f"{prompt_id}.{ext}"
                local_path = os.path.join(output_dir, local_name)
                with open(local_path, "wb") as f:
                    f.write(file_bytes)

                abs_path = os.path.abspath(local_path)
                return abs_path

        time.sleep(3)


# ---------------------------------------------------------------------------
# Workflow helpers
# ---------------------------------------------------------------------------

def set_node_input(workflow: dict, node_id: str, key: str, value):
    if node_id in workflow and "inputs" in workflow[node_id]:
        workflow[node_id]["inputs"][key] = value


def random_seed() -> int:
    return random.randint(0, 2**63 - 1)


def load_workflow(name: str) -> dict:
    path = WORKFLOW_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Workflow not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_schema(workflow_name: str) -> dict | None:
    stem = Path(workflow_name).stem
    schema_path = WORKFLOW_DIR / f"{stem}.schema.json"
    if not schema_path.exists():
        return None
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Workflow analysis — auto-detect parameters by class_type
# ---------------------------------------------------------------------------

PROMPT_CLASS_TYPES = {
    "Text Multiline", "CLIPTextEncode",
    "TextEncodeQwenImageEditPlusAdvance_lrzjason",
}
IMAGE_INPUT_CLASS_TYPES = {
    "LoadImage", "ETN_LoadImageBase64",
}
SEED_CLASS_TYPES = {
    "KSampler", "RandomNoise",
}
SEED_FIELD_NAMES = {"seed", "noise_seed"}
OUTPUT_CLASS_TYPES = {
    "SaveImage": "images",
    "VHS_VideoCombine": "videos",
}


def analyze_workflow(workflow: dict) -> dict:
    """Scan workflow JSON and produce a schema mapping of detected parameters."""
    params: dict[str, dict] = {}
    output_type = "images"

    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type", "")
        inputs = node.get("inputs", {})
        meta_title = node.get("_meta", {}).get("title", "")

        if ct in PROMPT_CLASS_TYPES:
            # Only match nodes with a direct string "text" field (not a link like ["nodeId", 0])
            field = "text" if ("text" in inputs and isinstance(inputs["text"], str)) else None
            if field:
                key = f"prompt_{node_id}" if "prompt" in params else "prompt"
                params[key] = {
                    "node_id": node_id, "field": field, "type": "string",
                    "class_type": ct, "description": meta_title or f"Text prompt ({ct})",
                }

        elif ct in IMAGE_INPUT_CLASS_TYPES:
            field = "image" if "image" in inputs else "images"
            image_type = "image_base64" if ct == "ETN_LoadImageBase64" else "image_upload"
            key = f"image_{node_id}" if "image" in params else "image"
            params[key] = {
                "node_id": node_id, "field": field, "type": image_type,
                "class_type": ct, "description": meta_title or f"Image input ({ct})",
            }

        elif ct in SEED_CLASS_TYPES:
            for sf in SEED_FIELD_NAMES:
                if sf in inputs:
                    key = f"seed_{node_id}" if "seed" in params else "seed"
                    params[key] = {
                        "node_id": node_id, "field": sf, "type": "seed",
                        "class_type": ct, "description": meta_title or f"Random seed ({ct})",
                    }
                    break

        if ct in OUTPUT_CLASS_TYPES:
            output_type = OUTPUT_CLASS_TYPES[ct]

    return {"parameters": params, "output_type": output_type}


def cmd_analyze(args):
    """Analyze a workflow JSON and write schema.json next to it."""
    wf_path = Path(args.file)
    if not wf_path.exists():
        print(f"ERROR: File not found: {wf_path}", file=sys.stderr)
        sys.exit(1)

    with open(wf_path, encoding="utf-8") as f:
        workflow = json.load(f)

    schema = analyze_workflow(workflow)

    if args.output:
        out_path = Path(args.output)
    else:
        out_path = wf_path.parent / f"{wf_path.stem}.schema.json"

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(schema, f, ensure_ascii=False, indent=2)

    print(json.dumps(schema, ensure_ascii=False, indent=2))
    print(f"\nSchema written to: {out_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Generic workflow runner (schema-driven)
# ---------------------------------------------------------------------------

def run_generic(
    workflow_name: str,
    prompt: str | None,
    image_path: str | None,
    duration: int | None,
    server_url: str,
    output_dir: str,
    timeout: int,
) -> str:
    wf = load_workflow(workflow_name)
    schema = load_schema(workflow_name)

    if schema is None:
        raise RuntimeError(
            f"No schema found for {workflow_name}. "
            f"Run: python comfyui_cli.py analyze <workflow_file> to generate one."
        )

    params = schema.get("parameters", {})
    output_type = schema.get("output_type", "images")

    for _key, spec in params.items():
        nid = str(spec["node_id"])
        field = spec["field"]
        ptype = spec["type"]

        if ptype == "string" and prompt:
            set_node_input(wf, nid, field, prompt)
        elif ptype == "image_base64" and image_path:
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            set_node_input(wf, nid, field, b64)
        elif ptype == "image_upload" and image_path:
            remote = upload_image(
                image_path, server_url,
                filename=f"microclaw_{uuid.uuid4().hex[:8]}.png",
            )
            set_node_input(wf, nid, field, remote)
        elif ptype == "seed":
            set_node_input(wf, nid, field, random_seed())
        elif ptype == "integer" and duration is not None:
            set_node_input(wf, nid, field, duration)

    result = queue_prompt(wf, server_url)
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"No prompt_id returned: {result}")

    return poll_and_download(prompt_id, server_url, output_type, timeout, output_dir)


# ---------------------------------------------------------------------------
# Legacy workflow runners (kept for backward compat with existing SKILL.md)
# ---------------------------------------------------------------------------

def run_text_to_image(prompt: str, server_url: str, output_dir: str, timeout: int) -> str:
    wf = load_workflow("Z-image.json")
    set_node_input(wf, "16", "text", prompt)
    set_node_input(wf, "4", "seed", random_seed())
    set_node_input(wf, "14", "value", 1024)
    set_node_input(wf, "15", "value", 720)

    result = queue_prompt(wf, server_url)
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"No prompt_id returned: {result}")

    return poll_and_download(prompt_id, server_url, "images", timeout, output_dir)


def run_image_edit(prompt: str, image_path: str, server_url: str, output_dir: str, timeout: int) -> str:
    wf = load_workflow("qwen-image-edit.json")
    set_node_input(wf, "35", "text", prompt)

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    set_node_input(wf, "51", "image", b64)
    set_node_input(wf, "44", "seed", random_seed())

    result = queue_prompt(wf, server_url)
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"No prompt_id returned: {result}")

    return poll_and_download(prompt_id, server_url, "images", timeout, output_dir)


def run_image_to_video(
    prompt: str, image_path: str, duration: int, server_url: str, output_dir: str, timeout: int
) -> str:
    remote_name = upload_image(
        image_path, server_url,
        filename=f"microclaw_{uuid.uuid4().hex[:8]}.png",
    )

    wf = load_workflow("LTX-i2v.json")
    set_node_input(wf, "250", "text", prompt)
    set_node_input(wf, "205", "image", remote_name)
    set_node_input(wf, "202", "value", duration)
    set_node_input(wf, "227", "noise_seed", random_seed())
    set_node_input(wf, "200", "value", 1024)
    set_node_input(wf, "201", "value", 720)

    result = queue_prompt(wf, server_url)
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"No prompt_id returned: {result}")

    return poll_and_download(prompt_id, server_url, "videos", timeout, output_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ComfyUI CLI for MicroClaw")
    sub = parser.add_subparsers(dest="command")

    # --- analyze subcommand ---
    p_analyze = sub.add_parser("analyze", help="Analyze a workflow JSON and generate schema.json")
    p_analyze.add_argument("file", help="Path to workflow JSON file")
    p_analyze.add_argument("--output", help="Output schema path (default: <stem>.schema.json next to workflow)")

    # --- run subcommand (generic, schema-driven) ---
    p_run = sub.add_parser("run", help="Run any workflow using its schema.json")
    p_run.add_argument("--workflow", required=True, help="Workflow JSON filename (in workflows/ dir)")
    p_run.add_argument("--prompt", help="Text prompt")
    p_run.add_argument("--image", help="Input image path")
    p_run.add_argument("--duration", type=int, help="Duration in seconds (for integer params)")
    p_run.add_argument("--server", default=SERVER_URL)
    p_run.add_argument("--output-dir", default=OUTPUT_DIR)
    p_run.add_argument("--timeout", type=int, default=0)

    # --- legacy subcommand-less mode for backward compat ---
    parser.add_argument("--workflow", dest="legacy_workflow", choices=["text_to_image", "image_edit", "image_to_video"])
    parser.add_argument("--prompt", dest="legacy_prompt")
    parser.add_argument("--image", dest="legacy_image")
    parser.add_argument("--duration", dest="legacy_duration", type=int, default=6)
    parser.add_argument("--server", dest="legacy_server", default=SERVER_URL)
    parser.add_argument("--output-dir", dest="legacy_output_dir", default=OUTPUT_DIR)
    parser.add_argument("--timeout", dest="legacy_timeout", type=int, default=0)

    args = parser.parse_args()

    try:
        if args.command == "analyze":
            cmd_analyze(args)
            return

        if args.command == "run":
            timeout = args.timeout or 600
            path = run_generic(
                args.workflow, args.prompt, args.image, args.duration,
                args.server.rstrip("/"), args.output_dir, timeout,
            )
            print(path)
            return

        # Legacy mode: --workflow text_to_image|image_edit|image_to_video
        if not args.legacy_workflow:
            parser.print_help()
            sys.exit(1)

        server = args.legacy_server.rstrip("/")
        output_dir = args.legacy_output_dir

        if args.legacy_workflow == "text_to_image":
            timeout = args.legacy_timeout or 600
            path = run_text_to_image(args.legacy_prompt, server, output_dir, timeout)

        elif args.legacy_workflow == "image_edit":
            if not args.legacy_image:
                print("ERROR: --image is required for image_edit", file=sys.stderr)
                sys.exit(1)
            timeout = args.legacy_timeout or 600
            path = run_image_edit(args.legacy_prompt, args.legacy_image, server, output_dir, timeout)

        elif args.legacy_workflow == "image_to_video":
            if not args.legacy_image:
                print("ERROR: --image is required for image_to_video", file=sys.stderr)
                sys.exit(1)
            timeout = args.legacy_timeout or 3600
            path = run_image_to_video(
                args.legacy_prompt, args.legacy_image, args.legacy_duration,
                server, output_dir, timeout,
            )

        print(path)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
