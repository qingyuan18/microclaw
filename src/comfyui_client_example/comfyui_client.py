"""ComfyUI client helpers for generating images and videos from shots.

This module supports three workflows:

* Z-image text-to-image workflow (per-shot keyframe image)
* LTX-i2v image-to-video workflow (replaces Wan2-2)
* LTX-lipSync lip-sync workflow (replaces MultiTalk, image + audio)
"""

from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
import random
import time
import uuid
from typing import Dict

from config import (
	COMFYUI_SERVER_URL,
	MAX_SHOT_DURATION_SEC,
	MIN_SHOT_DURATION_SEC,
	Z_IMAGE_WORKFLOW_PATH,
	WAN2_WORKFLOW_PATH,
	MULTITALK_WORKFLOW_PATH,
)
from storyboard_llm import Shot


def queue_prompt(prompt: Dict, server_url: str) -> Dict:
	"""Queue a ComfyUI workflow prompt and return the raw JSON response."""
	import urllib.request
	import urllib.error

	client_id = str(uuid.uuid4())
	payload = {"prompt": prompt, "client_id": client_id}
	data = json.dumps(payload).encode("utf-8")
	url = f"{server_url}/prompt"
	req = urllib.request.Request(url, data=data)
	try:
		with urllib.request.urlopen(req) as resp:
			return json.loads(resp.read())
	except urllib.error.HTTPError as e:
		# Read error details from ComfyUI for easier debugging
		error_body = ""
		try:
			error_body = e.read().decode("utf-8")
		except Exception:
			pass
		print(f"[ComfyUI] HTTP Error {e.code}: {e.reason}")
		print(f"[ComfyUI] Details: {error_body}")
		raise RuntimeError(f"ComfyUI error {e.code}: {error_body}") from e


def _upload_file_to_comfyui(
	file_path: str,
	server_url: str,
	subfolder: str | None = "audio2video",
	default_mime: str = "application/octet-stream",
) -> str:
	"""Upload a local file to the remote ComfyUI server.

	This uses ComfyUI's ``/upload/image`` endpoint, which actually accepts
	*any* file bytes and stores them under the configured "input" directory.
	Returns the relative path for use in workflow nodes.
	"""

	import urllib.error
	import urllib.request

	if not os.path.exists(file_path):
		raise FileNotFoundError(f"File not found for upload: {file_path}")

	server = server_url.rstrip("/")
	upload_url = f"{server}/upload/image"
	filename = os.path.basename(file_path)
	mime_type = mimetypes.guess_type(filename)[0] or default_mime
	boundary = "---------------------------" + uuid.uuid4().hex

	body = bytearray()

	def _add_field(name: str, value: str) -> None:
		body.extend(f"--{boundary}\r\n".encode("utf-8"))
		body.extend(
			f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode("utf-8")
		)
		body.extend(str(value).encode("utf-8"))
		body.extend(b"\r\n")

	# Tell ComfyUI to store under the input directory (default) and optional subfolder
	_add_field("type", "input")
	if subfolder:
		_add_field("subfolder", subfolder)

	with open(file_path, "rb") as f:
		file_bytes = f.read()

	body.extend(f"--{boundary}\r\n".encode("utf-8"))
	body.extend(
		f"Content-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n".encode(
			"utf-8"
		)
	)
	body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
	body.extend(file_bytes)
	body.extend(b"\r\n")
	body.extend(f"--{boundary}--\r\n".encode("utf-8"))

	headers = {
		"Content-Type": f"multipart/form-data; boundary={boundary}",
		"Content-Length": str(len(body)),
	}
	req = urllib.request.Request(upload_url, data=body, headers=headers)

	try:
		with urllib.request.urlopen(req, timeout=60) as resp:
			resp_text = resp.read().decode("utf-8") or "{}"
	except urllib.error.HTTPError as e:
		# Read error body for easier debugging
		error_body = ""
		try:
			error_body = e.read().decode("utf-8")
		except Exception:
			pass
		print(f"[ComfyUI] Upload HTTP Error {e.code}: {e.reason}")
		print(f"[ComfyUI] Upload details: {error_body}")
		raise RuntimeError(
			f"ComfyUI upload error {e.code}: {error_body}"
		) from e

	try:
		info = json.loads(resp_text)
	except json.JSONDecodeError:
		raise RuntimeError(
			f"Invalid JSON from ComfyUI upload response: {resp_text[:200]}"
		)

	# Expected shape from /upload/image:
	#   {"name": "file.png", "subfolder": "...", "type": "input"}
	if isinstance(info, dict) and "name" in info:
		name = info["name"]
		remote_subfolder = info.get("subfolder") or subfolder or ""
		if remote_subfolder:
			return f"{remote_subfolder}/{name}"
		return name

	if isinstance(info, str):
		return info

	raise RuntimeError(
		f"Unexpected response from ComfyUI upload endpoint: {info!r}"
	)


def upload_audio_to_comfyui(
	audio_path: str,
	server_url: str,
	subfolder: str | None = "audio2video",
) -> str:
	"""Upload a local audio file (mp3) to the remote ComfyUI server."""
	return _upload_file_to_comfyui(audio_path, server_url, subfolder, "audio/mpeg")


def upload_image_to_comfyui(
	image_path: str,
	server_url: str,
	subfolder: str | None = "audio2video",
) -> str:
	"""Upload a local image file to the remote ComfyUI server."""
	return _upload_file_to_comfyui(image_path, server_url, subfolder, "image/png")


def _poll_history_for_output(
	prompt_id: str,
	server_url: str,
	timeout_sec: float,
	output_key: str,
) -> bytes:
	"""Poll ``/history/{prompt_id}`` until an image/video is ready and return bytes.

	``output_key`` should be ``"images"`` or ``"videos"`` to match the
	structure of ComfyUI's ``outputs`` section.

	不同插件版本下，视频类节点的输出 key 可能不完全一致，例如
	ComfyUI-VideoHelperSuite 的 ``VHS_VideoCombine`` 在某些版本中会把
	mp4 结果放在 ``"gifs"`` 字段里（即使实际是 mp4），这里做了兼容：
	当 ``output_key == "videos"`` 时，会同时尝试从 ``"videos"`` 和
	``"gifs"`` 中读取文件列表。
	"""
	import urllib.request

	start = time.time()
	while True:
		if time.time() - start > timeout_sec:
			raise TimeoutError("ComfyUI generation timed out")

		url = f"{server_url}/history/{prompt_id}"
		with urllib.request.urlopen(url) as resp:
			body = resp.read()
			data = json.loads(body or b"{}")

		# ComfyUI /history [36mreturns[0m {"<prompt_id>": {"outputs": {...}, ...}}
		history = None
		if isinstance(data, dict):
			if prompt_id in data:
				history = data[prompt_id]
			elif "outputs" in data:
				# Backward compatibility: some setups may put outputs at top level
				history = data

		if history and isinstance(history, dict) and "outputs" in history:
			for _, out in history["outputs"].items():
				if not isinstance(out, dict):
					continue

				# Collect candidate file entries from the primary key
				candidates = []
				primary_list = out.get(output_key, [])
				if isinstance(primary_list, list):
					candidates.extend(primary_list)

				# Backward/sideways compatibility:
				# Some video nodes (e.g. VHS_VideoCombine from VideoHelperSuite)
				# expose their outputs under a "gifs" field even when format is mp4.
				# If we are looking for videos and nothing is found yet, also
				# consider entries from "gifs".
				if output_key == "videos" and not candidates:
					alt_list = out.get("gifs", [])
					if isinstance(alt_list, list):
						candidates.extend(alt_list)

				for node_out in candidates:
					if not isinstance(node_out, dict):
						continue
					filename = node_out.get("filename")
					subfolder = node_out.get("subfolder", "")
					output_type = node_out.get("type", "output")
					if not filename:
						continue
					file_url = (
						f"{server_url}/view?filename={filename}"
						f"&subfolder={subfolder}&type={output_type}"
					)
					with urllib.request.urlopen(file_url) as f_resp:
						return f_resp.read()

		time.sleep(3.0)


def get_video_by_prompt_id(
	prompt_id: str,
	server_url: str,
	timeout_sec: float = 3600.0,
) -> bytes:
	"""Poll ComfyUI for the generated video and return raw bytes."""
	return _poll_history_for_output(prompt_id, server_url, timeout_sec, "videos")


def get_image_by_prompt_id(
	prompt_id: str,
	server_url: str,
	timeout_sec: float = 600.0,
) -> bytes:
	"""Poll ComfyUI for the generated image and return raw bytes."""
	return _poll_history_for_output(prompt_id, server_url, timeout_sec, "images")


def _clamp_duration(duration_sec: float) -> float:
	return min(MAX_SHOT_DURATION_SEC, max(MIN_SHOT_DURATION_SEC, duration_sec))


def _encode_image_to_base64(image_path: str) -> str:
	"""Read an image file and return a base64-encoded PNG string.

	Wan2-2 / MultiTalk workflows use ``ETN_LoadImageBase64`` nodes
	requiring a plain base64-encoded string. To ensure compatibility,
	we re-encode the image as PNG regardless of the source format.
	"""
	from io import BytesIO
	from PIL import Image

	# Open and re-encode as PNG to ensure compatibility
	with Image.open(image_path) as img:
		# Convert to RGB if necessary (e.g., for RGBA images)
		if img.mode in ("RGBA", "P"):
			img = img.convert("RGB")
		buffer = BytesIO()
		img.save(buffer, format="PNG")
		png_bytes = buffer.getvalue()

	print(f"[ComfyUI] Encoded image to base64: {image_path} -> {len(png_bytes)} bytes (PNG)")
	return base64.b64encode(png_bytes).decode("utf-8")


def _run_workflow_and_save_video(workflow: Dict, output_path: str, server: str) -> str:
	"""Queue a workflow that outputs a video and save it to ``output_path``.

	同时在同一目录下保存一份调试用的 workflow JSON，便于排查问题。
	"""

	# Save workflow for debugging (Wan2 / MultiTalk 等视频 workflow)
	os.makedirs(os.path.dirname(output_path), exist_ok=True)
	debug_base, _ = os.path.splitext(output_path)
	debug_workflow_path = f"{debug_base}_workflow.json"
	with open(debug_workflow_path, "w", encoding="utf-8") as f:
		json.dump(workflow, f, ensure_ascii=False, indent=2)
	print(f"[ComfyUI] Debug workflow saved: {debug_workflow_path}")

	result = queue_prompt(workflow, server)
	prompt_id = result.get("prompt_id")
	print(f"[ComfyUI] Queued video workflow, prompt_id={prompt_id}")
	if not prompt_id:
		raise RuntimeError(f"Invalid ComfyUI response: {result}")

	video_bytes = get_video_by_prompt_id(prompt_id, server)
	with open(output_path, "wb") as f:
		f.write(video_bytes)
	return output_path


def _run_workflow_and_save_image(workflow: Dict, output_path: str, server: str) -> str:
	"""Queue a workflow that outputs an image and save it to ``output_path``."""
	# Save workflow for debugging
	os.makedirs(os.path.dirname(output_path), exist_ok=True)
	debug_workflow_path = output_path.replace(".png", "_workflow.json")
	with open(debug_workflow_path, "w", encoding="utf-8") as f:
		json.dump(workflow, f, ensure_ascii=False, indent=2)
	print(f"[ComfyUI] Debug workflow saved: {debug_workflow_path}")

	result = queue_prompt(workflow, server)
	prompt_id = result.get("prompt_id")
	if not prompt_id:
		raise RuntimeError(f"Invalid ComfyUI response: {result}")

	image_bytes = get_image_by_prompt_id(prompt_id, server)
	with open(output_path, "wb") as f:
		f.write(image_bytes)
	return output_path


def generate_z_image_for_shot(
	shot: Shot,
	output_path: str,
	server_url: str | None = None,
	workflow_path: str | None = None,
) -> str:
	"""Generate a keyframe image for a shot using the Z-image text-to-image workflow.

	The function injects the per-shot English visual prompt into the workflow
	before queuing it on ComfyUI.
	"""

	server = server_url or COMFYUI_SERVER_URL
	wpath = workflow_path or Z_IMAGE_WORKFLOW_PATH
	if not os.path.exists(wpath):
		raise FileNotFoundError(f"ComfyUI Z-image workflow JSON not found: {wpath}")

	with open(wpath, "r", encoding="utf-8") as f:
		workflow = json.load(f)

	prompt_text = shot.visual_prompt or shot.lyric_text
	# 追加 "写实图像" 避免生成二次元/动漫风格
	prompt_text = f"{prompt_text}, 写实图像" if prompt_text else "写实图像"

	# Support two workflow formats:
	# 1) "nodes" list (old ComfyUI format)
	# 2) dict keyed by node id strings (API export)
	nodes_list = workflow.get("nodes") if isinstance(workflow, dict) else None
	if isinstance(nodes_list, list):
		# Text Multiline
		for node in nodes_list:
			if node.get("type") == "Text Multiline":
				widgets = node.get("widgets_values") or []
				if widgets:
					widgets[0] = prompt_text
				else:
					widgets = [prompt_text]
				node["widgets_values"] = widgets
				break

		# CLIPTextEncode
		for node in nodes_list:
			if node.get("type") == "CLIPTextEncode":
				widgets = node.get("widgets_values") or []
				if widgets:
					widgets[0] = prompt_text
				else:
					widgets = [prompt_text]
				node["widgets_values"] = widgets
				break

		# Randomize KSampler seed
		for node in nodes_list:
			if node.get("type") == "KSampler":
				widgets = node.get("widgets_values") or []
				if widgets:
					widgets[0] = random.randint(0, 2**63 - 1)
					node["widgets_values"] = widgets
				break
	else:
		# API-export format: workflow is a dict keyed by node id strings
		if isinstance(workflow, dict):
			# Force Z-image resolution to 1024x720 (landscape, matching LTX video workflows).
			# Both values are divisible by 32.
			try:
				if "14" in workflow and "inputs" in workflow["14"]:
					workflow["14"]["inputs"]["value"] = 1024
				if "15" in workflow and "inputs" in workflow["15"]:
					workflow["15"]["inputs"]["value"] = 720
			except Exception as e:
				print(f"[Z-image] Failed to override width/height: {e}")

			# Overwrite Text Multiline node input
			for node_id, node in workflow.items():
				if not isinstance(node, dict):
					continue
				if node.get("class_type") == "Text Multiline":
					inputs = node.setdefault("inputs", {})
					inputs["text"] = prompt_text

			# Randomize KSampler seed
			for node_id, node in workflow.items():
				if not isinstance(node, dict):
					continue
				if node.get("class_type") == "KSampler":
					inputs = node.setdefault("inputs", {})
					if "seed" in inputs:
						inputs["seed"] = random.randint(0, 2**63 - 1)

	return _run_workflow_and_save_image(workflow, output_path, server)


def generate_wan2_video_from_shot(
	shot: Shot,
	image_path: str,
	output_path: str,
	duration_sec: float,
	server_url: str | None = None,
	workflow_path: str | None = None,
) -> str:
	"""Generate a short video for one shot using the LTX-i2v workflow.

	This injects the visual prompt, base image and desired duration
	into the workflow JSON before queuing it.

	LTX-i2v.json node mapping:
	- 205: LoadImage (image filename)
	- 250: Text Multiline (prompt text)
	- 202: JWInteger (duration in seconds)
	- 200: JWInteger (width)
	- 201: JWInteger (height)
	- 227: RandomNoise (noise_seed)
	"""

	server = server_url or COMFYUI_SERVER_URL
	wpath = workflow_path or WAN2_WORKFLOW_PATH
	if not os.path.exists(wpath):
		raise FileNotFoundError(f"ComfyUI LTX-i2v workflow JSON not found: {wpath}")

	with open(wpath, "r", encoding="utf-8") as f:
		workflow = json.load(f)

	# Text prompt node ("250") - 组合 visual_prompt + style + camera_motion
	prompt_parts = [shot.visual_prompt or shot.lyric_text]
	if shot.style:
		prompt_parts.append(shot.style)
	if shot.camera_motion:
		prompt_parts.append(shot.camera_motion)
	# 添加后缀：不带字幕
	prompt_parts.append("不带字幕")
	combined_prompt = ", ".join(prompt_parts)

	if "250" in workflow and "inputs" in workflow["250"]:
		workflow["250"]["inputs"]["text"] = combined_prompt

	# Negative prompt node ("221") - 添加负向提示词后缀
	if "221" in workflow and "inputs" in workflow["221"]:
		neg_text = workflow["221"]["inputs"].get("text", "")
		if neg_text and isinstance(neg_text, str):
			workflow["221"]["inputs"]["text"] = neg_text + ", 镜头左右移动"
			print("[LTX-i2v] 已添加负向提示词: 镜头左右移动")

	# Base image via LoadImage (id=205) - upload image first
	remote_image_name = upload_image_to_comfyui(image_path, server)
	if "205" in workflow and "inputs" in workflow["205"]:
		workflow["205"]["inputs"]["image"] = remote_image_name

	# Duration control via JWInteger node (id=202) - duration in seconds
	# 使用 ceil 确保视频略长于音频，避免视频提前结束
	duration = _clamp_duration(duration_sec)
	ceiled_duration = math.ceil(duration)
	if "202" in workflow and "inputs" in workflow["202"]:
		workflow["202"]["inputs"]["value"] = ceiled_duration
	print(f"[LTX-i2v] shot时长: {duration_sec:.2f}s -> {ceiled_duration}s (ceil)")

	# Randomize RandomNoise seed (id=227)
	if "227" in workflow and "inputs" in workflow["227"]:
		workflow["227"]["inputs"]["noise_seed"] = random.randint(0, 2**63 - 1)

	return _run_workflow_and_save_video(workflow, output_path, server)


def _is_multitalk_workflow(workflow: Dict) -> bool:
	"""Return True if *workflow* is a MultiTalk.json-style workflow.

	Detection is based on the presence of node ``"212"`` with class_type
	``ETN_LoadImageBase64`` – this node only exists in MultiTalk.json.
	"""
	node = workflow.get("212")
	return isinstance(node, dict) and node.get("class_type") == "ETN_LoadImageBase64"


def generate_multitalk_video_from_shot(
	shot: Shot,
	image_path: str,
	audio_path: str,
	output_path: str,
	duration_sec: float,
	server_url: str | None = None,
	workflow_path: str | None = None,
) -> str:
	"""Generate a short lip-synced video for one shot.

	Supports two workflow formats (auto-detected from the loaded JSON):

	* **LTX-lipSync.json** (default) – LoadImage + ImpactInt duration (seconds)
	* **MultiTalk.json** – ETN_LoadImageBase64 + Primitive integer duration (frames)

	* ``image_path`` is the talking-head base image.
	* ``audio_path`` is the per-shot audio segment (mp3).
	* ``duration_sec`` is ignored; we derive duration from actual audio length.
	"""
	from pydub import AudioSegment as PydubAudioSegment

	server = server_url or COMFYUI_SERVER_URL
	wpath = workflow_path or MULTITALK_WORKFLOW_PATH
	if not os.path.exists(wpath):
		raise FileNotFoundError(f"ComfyUI lip-sync workflow JSON not found: {wpath}")

	with open(wpath, "r", encoding="utf-8") as f:
		workflow = json.load(f)

	# --- Read actual audio duration (common to both workflows) ---
	actual_audio = PydubAudioSegment.from_file(audio_path)
	actual_duration_sec = len(actual_audio) / 1000.0
	duration = _clamp_duration(actual_duration_sec)
	ceiled_duration = math.ceil(duration)

	if _is_multitalk_workflow(workflow):
		# ===== MultiTalk.json node mapping =====
		# 212: ETN_LoadImageBase64 (base64 image)
		# 125: LoadAudio (audio filename)
		# 187: Text Multiline (prompt)
		# 195: Primitive integer (num_frames, fps=25)
		# 128: WanVideoSampler (seed)
		tag = "MultiTalk"
		print(f"[{tag}] 检测到 MultiTalk workflow")

		# Image – base64 encoded
		b64_image = _encode_image_to_base64(image_path)
		if "212" in workflow and "inputs" in workflow["212"]:
			workflow["212"]["inputs"]["image"] = b64_image

		# Audio
		remote_audio_name = upload_audio_to_comfyui(audio_path, server, subfolder=None)
		if "125" in workflow and "inputs" in workflow["125"]:
			workflow["125"]["inputs"]["audio"] = remote_audio_name
		print(f"[{tag}] 音频已上传: {audio_path} -> {remote_audio_name}")

		# Text prompt
		if "187" in workflow and "inputs" in workflow["187"]:
			workflow["187"]["inputs"]["text"] = "图中人物在唱歌"

		# Duration → frame count (fps=25, frames = seconds * 25 + 1)
		num_frames = ceiled_duration * 25 + 1
		if "195" in workflow and "inputs" in workflow["195"]:
			workflow["195"]["inputs"]["int"] = num_frames
		print(f"[{tag}] 音频时长: {actual_duration_sec:.3f}s -> {num_frames} frames ({ceiled_duration}s × 25fps + 1)")

		# Seed
		if "128" in workflow and "inputs" in workflow["128"]:
			workflow["128"]["inputs"]["seed"] = random.randint(0, 2**63 - 1)
	else:
		# ===== LTX-lipSync.json node mapping =====
		# 240: LoadImage (image filename)
		# 243: LoadAudio (audio filename)
		# 367: Text Multiline (prompt text)
		# 355: ImpactInt (duration in seconds)
		# 358/359: ImpactInt (width/height)
		# 353: Float (audio start time)
		# 178: RandomNoise (noise_seed)
		tag = "LTX-lipSync"
		print(f"[{tag}] 检测到 LTX-lipSync workflow")

		# Image – upload file
		remote_image_name = upload_image_to_comfyui(image_path, server)
		if "240" in workflow and "inputs" in workflow["240"]:
			workflow["240"]["inputs"]["image"] = remote_image_name

		# Audio
		remote_audio_name = upload_audio_to_comfyui(audio_path, server, subfolder=None)
		if "243" in workflow and "inputs" in workflow["243"]:
			workflow["243"]["inputs"]["audio"] = remote_audio_name
		print(f"[{tag}] 音频已上传: {audio_path} -> {remote_audio_name}")

		# Text prompt
		if "367" in workflow and "inputs" in workflow["367"]:
			workflow["367"]["inputs"]["text"] = "图中人物在唱歌"

		# Negative prompt suffix
		if "165" in workflow and "inputs" in workflow["165"]:
			neg_text = workflow["165"]["inputs"].get("text", "")
			if neg_text and isinstance(neg_text, str):
				workflow["165"]["inputs"]["text"] = neg_text + ", 镜头左右移动"
				print(f"[{tag}] 已添加负向提示词: 镜头左右移动")

		# Duration (seconds)
		if "355" in workflow and "inputs" in workflow["355"]:
			workflow["355"]["inputs"]["value"] = ceiled_duration
		print(f"[{tag}] 音频时长: {actual_duration_sec:.3f}s -> {ceiled_duration}s (ceil)")

		# Audio start time
		if "353" in workflow and "inputs" in workflow["353"]:
			workflow["353"]["inputs"]["value"] = "0.00"

		# Seed
		if "178" in workflow and "inputs" in workflow["178"]:
			workflow["178"]["inputs"]["noise_seed"] = random.randint(0, 2**63 - 1)

		# Force output resolution 1024×720
		if "358" in workflow and "inputs" in workflow["358"]:
			workflow["358"]["inputs"]["value"] = 1024
		if "359" in workflow and "inputs" in workflow["359"]:
			workflow["359"]["inputs"]["value"] = 720
		print(f"[{tag}] 强制输出分辨率: 1024x720")

	return _run_workflow_and_save_video(workflow, output_path, server)


def generate_qwen_image_edit(
	reference_image_path: str,
	visual_prompt: str,
	output_path: str,
	server_url: str | None = None,
	workflow_path: str | None = None,
) -> str:
	"""Generate an image using Qwen image-edit workflow (image-to-image).

	This workflow takes a reference character image and generates a new image
	that maintains the character's appearance while placing them in a new scene
	described by the visual_prompt.

	Args:
		reference_image_path: Path to the reference character image.
		visual_prompt: Text prompt describing the desired scene/action.
		output_path: Path to save the generated image.
		server_url: ComfyUI server URL.
		workflow_path: Path to the qwen-image-edit workflow JSON.

	Returns:
		Path to the saved image.
	"""
	from config import QWEN_IMAGE_EDIT_WORKFLOW_PATH

	server = server_url or COMFYUI_SERVER_URL
	wpath = workflow_path or QWEN_IMAGE_EDIT_WORKFLOW_PATH

	if not os.path.exists(wpath):
		raise FileNotFoundError(f"Qwen image-edit workflow JSON not found: {wpath}")

	if not os.path.exists(reference_image_path):
		raise FileNotFoundError(f"Reference image not found: {reference_image_path}")

	with open(wpath, "r", encoding="utf-8") as f:
		workflow = json.load(f)

	# Encode reference image to base64
	ref_image_b64 = _encode_image_to_base64(reference_image_path)

	# Inject image and prompt into workflow nodes
	# Based on qwen-image-edit.json:
	# - Node 51 (ETN_LoadImageBase64): reference image input
	# - Node 35 (Text Multiline): text prompt for editing instruction

	# Inject reference image into node 51
	if "51" in workflow and "inputs" in workflow["51"]:
		workflow["51"]["inputs"]["image"] = ref_image_b64
		print(f"[ComfyUI] Injected reference image into node 51 (ETN_LoadImageBase64)")
	else:
		# Fallback: try common node IDs for image input
		image_input_nodes = ["1", "10", "100", "212"]
		for node_id in image_input_nodes:
			if node_id in workflow and "inputs" in workflow[node_id]:
				node = workflow[node_id]
				if "image" in node["inputs"]:
					node["inputs"]["image"] = ref_image_b64
					print(f"[ComfyUI] Injected reference image into node {node_id}")
					break

	# Inject prompt into node 35
	# 追加 "写实图像" 避免生成二次元/动漫风格
	final_prompt = f"{visual_prompt}, 写实图像" if visual_prompt else "写实图像"
	if "35" in workflow and "inputs" in workflow["35"]:
		workflow["35"]["inputs"]["text"] = final_prompt
		print(f"[ComfyUI] Injected prompt into node 35 (Text Multiline)")
	else:
		# Fallback: try common node IDs for text prompt
		prompt_nodes = ["2", "6", "20", "187"]
		for node_id in prompt_nodes:
			if node_id in workflow and "inputs" in workflow[node_id]:
				node = workflow[node_id]
			if "text" in node["inputs"]:
				node["inputs"]["text"] = final_prompt
				print(f"[ComfyUI] Injected prompt into node {node_id}: {final_prompt[:50]}...")
				break

	# Randomize seed if present
	for node_id, node in workflow.items():
		if isinstance(node, dict) and "inputs" in node:
			if "seed" in node["inputs"]:
				node["inputs"]["seed"] = random.randint(0, 2**63 - 1)

	# Force output resolution to 1024x720 (landscape, matching LTX video workflows)
	# Node 53 (ImageResize+) controls final output resolution
	if "53" in workflow and "inputs" in workflow["53"]:
		workflow["53"]["inputs"]["width"] = 1024
		workflow["53"]["inputs"]["height"] = 720
		print("[ComfyUI] Forced Qwen image-edit output resolution to 1024x720")

	print(f"[ComfyUI] Generating image with Qwen image-edit workflow")
	print(f"[ComfyUI] Reference: {reference_image_path}")
	print(f"[ComfyUI] Prompt: {visual_prompt[:100]}...")

	return _run_workflow_and_save_image(workflow, output_path, server)


def generate_story_shot_image(
	shot,  # StoryShot from story_shot.py
	character_image_paths: list[str],
	style_image_path: str | None,
	output_path: str,
	server_url: str | None = None,
	z_image_workflow_path: str | None = None,
	qwen_edit_workflow_path: str | None = None,
) -> str:
	"""Generate an image for a story shot.

	Chooses between Z-image (text-to-image) and Qwen image-edit (image-to-image)
	based on whether the shot involves a specific character.

	Args:
		shot: StoryShot object with visual_prompt, character_id, etc.
		character_image_paths: List of character reference image paths.
		style_image_path: Optional style reference image (for future use).
		output_path: Path to save the generated image.
		server_url: ComfyUI server URL.
		z_image_workflow_path: Path to Z-image workflow for scene shots.
		qwen_edit_workflow_path: Path to Qwen image-edit workflow for character shots.

	Returns:
		Path to the saved image.
	"""
	from config import Z_IMAGE_WORKFLOW_PATH, QWEN_IMAGE_EDIT_WORKFLOW_PATH
	from storyboard_llm import Shot

	server = server_url or COMFYUI_SERVER_URL
	z_path = z_image_workflow_path or Z_IMAGE_WORKFLOW_PATH
	qwen_path = qwen_edit_workflow_path or QWEN_IMAGE_EDIT_WORKFLOW_PATH

	# Check if shot involves a character
	if shot.character_id is not None and character_image_paths:
		char_idx = shot.character_id
		if 0 <= char_idx < len(character_image_paths):
			ref_image = character_image_paths[char_idx]
			print(f"[ComfyUI] Shot {shot.shot_index}: Using character {char_idx} image-edit")
			return generate_qwen_image_edit(
				reference_image_path=ref_image,
				visual_prompt=shot.visual_prompt,
				output_path=output_path,
				server_url=server,
				workflow_path=qwen_path,
			)

	# No character or invalid character_id: use text-to-image
	print(f"[ComfyUI] Shot {shot.shot_index}: Using Z-image text-to-image")

	# Create a minimal Shot-like object for generate_z_image_for_shot
	temp_shot = Shot(
		shot_index=shot.shot_index,
		start=0.0,
		end=shot.duration,
		lyric_text=shot.subtitle,
		visual_prompt=shot.visual_prompt,
		style=shot.style,
		camera_motion=shot.camera_motion,
	)
	return generate_z_image_for_shot(
		shot=temp_shot,
		output_path=output_path,
		server_url=server,
		workflow_path=z_path,
	)
