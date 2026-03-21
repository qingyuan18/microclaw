---
name: comfyui
description: >
  AI image and video generation via ComfyUI. USE THIS SKILL when the user asks to
  generate/create/draw an image, edit an image, or create a video from an image.
  当用户要求画图、生成图片、创建图像、编辑图片、或生成视频时，必须激活此 skill。
  Supports text_to_image, image_edit, and image_to_video workflows.
deps:
  - python3
---

# ComfyUI — AI Image & Video Generation Skill

This skill provides AI image generation and video creation via a ComfyUI backend server.

## Available Workflows

| Workflow | Description | Image required? |
|----------|-------------|----------------|
| `text_to_image` | Generate an image from a text description | No |
| `image_edit` | Edit/transform an existing image based on text instruction | Yes |
| `image_to_video` | Animate a static image into a short video | Yes |

**NOTE:** Text-to-video is NOT directly supported. To generate a video from text, first use `text_to_image` to create an image, then use `image_to_video` on that image.

## How to Use

The CLI script is located in this skill's directory. Find the skill directory path from the activation info above (the "Skill directory" line).

### Basic Commands

**Text to Image:**
```bash
python3 <skill_dir>/comfyui_cli.py \
  --workflow text_to_image \
  --prompt "<detailed English prompt describing the image>" \
  --server "<COMFYUI_URL>" \
  --output-dir "<output_directory>"
```

**Image Edit:**
```bash
python3 <skill_dir>/comfyui_cli.py \
  --workflow image_edit \
  --prompt "<instruction for editing>" \
  --image "<path_to_input_image>" \
  --server "<COMFYUI_URL>" \
  --output-dir "<output_directory>"
```

**Image to Video:**
```bash
python3 <skill_dir>/comfyui_cli.py \
  --workflow image_to_video \
  --prompt "<motion/scene description>" \
  --image "<path_to_input_image>" \
  --duration <seconds> \
  --server "<COMFYUI_URL>" \
  --output-dir "<output_directory>"
```

### Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `--workflow` | Yes | — | One of: `text_to_image`, `image_edit`, `image_to_video` |
| `--prompt` | Yes | — | Text prompt describing what to generate |
| `--image` | For edit/video | — | Local file path to input image |
| `--duration` | No | 6 | Video duration in seconds (image_to_video only) |
| `--server` | No | env `COMFYUI_URL` or `http://localhost:8188` | ComfyUI server URL |
| `--output-dir` | No | env `COMFYUI_OUTPUT` or `/tmp/comfyui_output` | Where to save output files |
| `--timeout` | No | auto | Timeout in seconds (600 for images, 3600 for video) |

### Important: Server URL

The ComfyUI server URL must be provided. Check the bot's config for `comfyui_url`. You can either:
- Pass it via `--server` flag
- Or set the `COMFYUI_URL` environment variable

### Important: Timeout

Image generation can take 1-5 minutes, video generation can take 10-60 minutes. Always use `timeout_secs` in your `bash` tool call:
- For `text_to_image` / `image_edit`: use `timeout_secs: 600`
- For `image_to_video`: use `timeout_secs: 3600`

### Output

The script prints the **absolute path** of the generated file to stdout on success. Use this path with `send_message` and `attachment_path` to deliver the result to the user.

On error, it prints `ERROR: <message>` to stderr and exits with code 1.

## Usage Pattern

1. Call `bash` with the appropriate command and `timeout_secs` set high enough
2. The command outputs a file path on success
3. Use `send_message` with `attachment_path` set to that file path to send the result to the user

### Example: Generate and send an image

```
Step 1: Call bash tool
  command: python3 /path/to/skills/comfyui/comfyui_cli.py --workflow text_to_image --prompt "a golden retriever playing in autumn leaves, warm sunlight, photorealistic" --server http://192.168.1.100:8188 --output-dir /tmp/comfyui_output
  timeout_secs: 600

Step 2: Parse the output path from stdout

Step 3: Call send_message with attachment_path=<output_path>
```

### Example: Edit an image then make a video

```
Step 1: Edit the image
  bash: python3 .../comfyui_cli.py --workflow image_edit --prompt "transform into sunset scene" --image /path/to/original.png --server http://... --output-dir /tmp/comfyui_output
  timeout_secs: 600

Step 2: Generate video from the edited image
  bash: python3 .../comfyui_cli.py --workflow image_to_video --prompt "camera slowly pans across the sunset scene" --image <edited_image_path> --duration 6 --server http://... --output-dir /tmp/comfyui_output
  timeout_secs: 3600

Step 3: Send video to user via send_message with attachment_path
```

## Image Input Handling

- When the user provides an image in chat, it is saved to a local file. The message will contain a path like `[图片已保存到本地: /tmp/feishu_upload_xxx.jpg]`. Extract this path and use it as `--image`.
- When using output from a previous generation, use that output path directly as `--image`.
- Prefer `--image` (file path) over base64 encoding. The script handles file reading internally.

## Workflow Extension

To add new workflows, place the workflow JSON file in the `workflows/` subdirectory of this skill. Then update this SKILL.md with instructions for using the new workflow, and add the corresponding function in `comfyui_cli.py`.
