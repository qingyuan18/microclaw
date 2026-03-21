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

SERVER_URL = os.environ.get("COMFYUI_URL", "http://localhost:8188").rstrip("/")
OUTPUT_DIR = os.environ.get("COMFYUI_OUTPUT", "/tmp/comfyui_output")
SCRIPT_DIR = Path(__file__).resolve().parent
WORKFLOW_DIR = SCRIPT_DIR / "workflows"


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


# ---------------------------------------------------------------------------
# Workflow runners
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

    # Encode image as base64 for ETN_LoadImageBase64 node
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
    # Upload image to ComfyUI (LoadImage needs server-side file)
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
    parser.add_argument("--workflow", required=True, choices=["text_to_image", "image_edit", "image_to_video"])
    parser.add_argument("--prompt", required=True, help="Text prompt")
    parser.add_argument("--image", help="Input image path (required for image_edit and image_to_video)")
    parser.add_argument("--duration", type=int, default=6, help="Video duration in seconds (default: 6)")
    parser.add_argument("--server", default=SERVER_URL, help=f"ComfyUI server URL (default: {SERVER_URL})")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help=f"Output directory (default: {OUTPUT_DIR})")
    parser.add_argument("--timeout", type=int, default=0, help="Timeout in seconds (0 = auto per workflow)")
    args = parser.parse_args()

    server = args.server.rstrip("/")
    output_dir = args.output_dir

    try:
        if args.workflow == "text_to_image":
            timeout = args.timeout or 600
            path = run_text_to_image(args.prompt, server, output_dir, timeout)

        elif args.workflow == "image_edit":
            if not args.image:
                print("ERROR: --image is required for image_edit", file=sys.stderr)
                sys.exit(1)
            timeout = args.timeout or 600
            path = run_image_edit(args.prompt, args.image, server, output_dir, timeout)

        elif args.workflow == "image_to_video":
            if not args.image:
                print("ERROR: --image is required for image_to_video", file=sys.stderr)
                sys.exit(1)
            timeout = args.timeout or 3600
            path = run_image_to_video(args.prompt, args.image, args.duration, server, output_dir, timeout)

        print(path)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
