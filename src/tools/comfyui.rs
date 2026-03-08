use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::Duration;

use async_trait::async_trait;
use base64::Engine;
use serde_json::json;

use super::{schema_object, Tool, ToolResult};
use crate::llm_types::ToolDefinition;

// Embed workflow JSON templates (paths relative to this source file)
const WORKFLOW_Z_IMAGE: &str =
    include_str!("../comfyui_client_example/workflow/Z-image.json");
const WORKFLOW_QWEN_EDIT: &str =
    include_str!("../comfyui_client_example/workflow/qwen-image-edit.json");
const WORKFLOW_LTX_I2V: &str =
    include_str!("../comfyui_client_example/workflow/LTX-i2v.json");

fn http_client() -> &'static reqwest::Client {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .timeout(Duration::from_secs(600))
            .connect_timeout(Duration::from_secs(10))
            .user_agent("MicroClaw/1.0")
            .build()
            .expect("failed to build HTTP client")
    })
}

fn random_seed() -> i64 {
    // Use UUID bytes as entropy source (no rand crate dependency)
    let bytes = uuid::Uuid::new_v4().as_bytes()[..8].try_into().unwrap_or([0u8; 8]);
    (i64::from_ne_bytes(bytes)).unsigned_abs() as i64
}

pub struct ComfyUiTool {
    server_url: String,
    output_dir: PathBuf,
}

impl ComfyUiTool {
    pub fn new(server_url: &str, data_dir: &str) -> Self {
        let output_dir = Path::new(data_dir).join("runtime").join("comfyui_output");
        let _ = std::fs::create_dir_all(&output_dir);
        ComfyUiTool {
            server_url: server_url.trim_end_matches('/').to_string(),
            output_dir,
        }
    }
}

#[async_trait]
impl Tool for ComfyUiTool {
    fn name(&self) -> &str {
        "comfyui_generate"
    }

    fn definition(&self) -> ToolDefinition {
        ToolDefinition {
            name: "comfyui_generate".into(),
            description: concat!(
                "USE THIS TOOL when the user asks to generate/create/draw an image, edit an image, ",
                "or create a video from an image. This is the AI image and video generation tool.\n\n",
                "当用户要求画图、生成图片、创建图像、编辑图片、或生成视频时，必须使用此工具。\n\n",
                "Workflow types:\n",
                "- text_to_image: Generate an image from a text description. No image input needed.\n",
                "- image_edit: Edit/transform an existing image based on text instruction. Image required.\n",
                "- image_to_video: Animate a static image into a short video. Image required.\n\n",
                "NOTE: Text-to-video is NOT supported. To generate a video from text, first use ",
                "text_to_image to create an image, then use image_to_video on that image.\n\n",
                "Image input: use image_path (local file path) OR image_base64 (base64 string). ",
                "Prefer image_path when the image is already a local file (e.g. output from a previous ",
                "text_to_image call). Use image_base64 only when the image comes from user chat.\n\n",
                "Returns a local file path. Use send_message with attachment_path to send the result to the user.",
            )
            .into(),
            input_schema: schema_object(
                json!({
                    "workflow_type": {
                        "type": "string",
                        "enum": ["text_to_image", "image_edit", "image_to_video"],
                        "description": "Which workflow to use. No text_to_video: use text_to_image + image_to_video instead."
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Text prompt describing what to generate."
                    },
                    "image_path": {
                        "type": "string",
                        "description": "Local file path to an image. Use this when the image is already on disk (e.g. from a previous text_to_image generation). Preferred over image_base64."
                    },
                    "image_base64": {
                        "type": "string",
                        "description": "Base64-encoded image data from user chat input. Use image_path instead if the image is already a local file."
                    },
                    "duration_seconds": {
                        "type": "integer",
                        "description": "Video duration in seconds. Used by image_to_video (default 6)."
                    }
                }),
                &["workflow_type", "prompt"],
            ),
        }
    }

    async fn execute(&self, input: serde_json::Value) -> ToolResult {
        let workflow_type = match input.get("workflow_type").and_then(|v| v.as_str()) {
            Some(t) => t,
            None => return ToolResult::error("Missing required parameter: workflow_type".into()),
        };
        let prompt = match input.get("prompt").and_then(|v| v.as_str()) {
            Some(p) => p.to_string(),
            None => return ToolResult::error("Missing required parameter: prompt".into()),
        };
        let image_path = input.get("image_path").and_then(|v| v.as_str());
        let image_base64 = input.get("image_base64").and_then(|v| v.as_str());
        let duration_seconds = input.get("duration_seconds").and_then(|v| v.as_u64());

        // Resolve image: prefer image_path (read from disk), fall back to image_base64
        let image_bytes: Option<Vec<u8>> = if let Some(path) = image_path {
            match tokio::fs::read(path).await {
                Ok(bytes) => Some(bytes),
                Err(e) => return ToolResult::error(format!("Failed to read image_path '{path}': {e}")),
            }
        } else if let Some(b64) = image_base64 {
            match base64::engine::general_purpose::STANDARD.decode(b64) {
                Ok(bytes) => Some(bytes),
                Err(e) => return ToolResult::error(format!("Invalid image_base64: {e}")),
            }
        } else {
            None
        };

        match workflow_type {
            "text_to_image" => {
                self.run_text_to_image(&prompt).await
            }
            "image_edit" => {
                let img = match &image_bytes {
                    Some(b) => b.as_slice(),
                    None => return ToolResult::error(
                        "image_edit requires image_path or image_base64".into(),
                    ),
                };
                self.run_image_edit(&prompt, img).await
            }
            "image_to_video" => {
                let img = match &image_bytes {
                    Some(b) => b.as_slice(),
                    None => return ToolResult::error(
                        "image_to_video requires image_path or image_base64".into(),
                    ),
                };
                let dur = duration_seconds.unwrap_or(6) as i64;
                self.run_image_to_video(&prompt, img, dur).await
            }
            _ => ToolResult::error(format!(
                "Unknown workflow_type: {workflow_type}. Must be one of: text_to_image, image_edit, image_to_video"
            )),
        }
    }
}

impl ComfyUiTool {
    /// Z-image: text → image
    /// Nodes: 16(Text Multiline prompt), 4(KSampler seed), 14(width), 15(height)
    async fn run_text_to_image(&self, prompt: &str) -> ToolResult {
        let mut wf: serde_json::Value = match serde_json::from_str(WORKFLOW_Z_IMAGE) {
            Ok(v) => v,
            Err(e) => return ToolResult::error(format!("Failed to parse Z-image workflow: {e}")),
        };

        // Inject prompt into node 16 (Text Multiline)
        set_node_input(&mut wf, "16", "text", json!(prompt));
        // Randomize KSampler seed (node 4)
        set_node_input(&mut wf, "4", "seed", json!(random_seed()));
        // Force resolution 1024x720
        set_node_input(&mut wf, "14", "value", json!(1024));
        set_node_input(&mut wf, "15", "value", json!(720));

        self.queue_and_save(wf, "images", 600).await
    }

    /// qwen-image-edit: image + text → edited image
    /// Nodes: 35(Text Multiline), 51(ETN_LoadImageBase64), 44(KSampler seed)
    async fn run_image_edit(&self, prompt: &str, image_bytes: &[u8]) -> ToolResult {
        let mut wf: serde_json::Value = match serde_json::from_str(WORKFLOW_QWEN_EDIT) {
            Ok(v) => v,
            Err(e) => {
                return ToolResult::error(format!("Failed to parse qwen-image-edit workflow: {e}"))
            }
        };

        // Inject prompt (node 35)
        set_node_input(&mut wf, "35", "text", json!(prompt));
        // Inject image (node 51 ETN_LoadImageBase64, needs base64 string)
        let b64 = base64::engine::general_purpose::STANDARD.encode(image_bytes);
        set_node_input(&mut wf, "51", "image", json!(b64));
        // Randomize seed (node 44)
        set_node_input(&mut wf, "44", "seed", json!(random_seed()));

        self.queue_and_save(wf, "images", 600).await
    }

    /// LTX-i2v: upload image + text → video
    /// Nodes: 250(Text Multiline), 205(LoadImage filename), 202(duration sec),
    ///        227(RandomNoise seed), 200(width), 201(height)
    async fn run_image_to_video(
        &self,
        prompt: &str,
        image_bytes: &[u8],
        duration_sec: i64,
    ) -> ToolResult {
        // Upload raw image bytes to ComfyUI server (LoadImage needs a server-side file)
        let remote_filename = match upload_image_bytes_to_comfyui(
            &self.server_url,
            image_bytes,
            &format!("microclaw_{}.png", uuid::Uuid::new_v4()),
        )
        .await
        {
            Ok(name) => name,
            Err(e) => return ToolResult::error(format!("Failed to upload image to ComfyUI: {e}")),
        };

        let mut wf: serde_json::Value = match serde_json::from_str(WORKFLOW_LTX_I2V) {
            Ok(v) => v,
            Err(e) => return ToolResult::error(format!("Failed to parse LTX-i2v workflow: {e}")),
        };

        // Inject prompt (node 250)
        set_node_input(&mut wf, "250", "text", json!(prompt));
        // Set uploaded image filename (node 205 LoadImage)
        set_node_input(&mut wf, "205", "image", json!(remote_filename));
        // Duration (node 202)
        set_node_input(&mut wf, "202", "value", json!(duration_sec));
        // Randomize noise seed (node 227)
        set_node_input(&mut wf, "227", "noise_seed", json!(random_seed()));
        // Resolution (nodes 200, 201)
        set_node_input(&mut wf, "200", "value", json!(1024));
        set_node_input(&mut wf, "201", "value", json!(720));

        self.queue_and_save(wf, "videos", 3600).await
    }

    /// Queue workflow on ComfyUI, poll for output, download and save file locally.
    async fn queue_and_save(
        &self,
        workflow: serde_json::Value,
        output_key: &str,
        timeout_sec: u64,
    ) -> ToolResult {
        // Queue prompt
        let client_id = uuid::Uuid::new_v4().to_string();
        let payload = json!({ "prompt": workflow, "client_id": client_id });
        let queue_url = format!("{}/prompt", self.server_url);

        let resp = match http_client()
            .post(&queue_url)
            .json(&payload)
            .timeout(Duration::from_secs(30))
            .send()
            .await
        {
            Ok(r) => r,
            Err(e) => return ToolResult::error(format!("Failed to connect to ComfyUI: {e}")),
        };

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return ToolResult::error(format!("ComfyUI queue error (HTTP {status}): {body}"));
        }

        let queue_result: serde_json::Value = match resp.json().await {
            Ok(v) => v,
            Err(e) => return ToolResult::error(format!("Invalid JSON from ComfyUI /prompt: {e}")),
        };

        let prompt_id = match queue_result.get("prompt_id").and_then(|v| v.as_str()) {
            Some(id) => id.to_string(),
            None => {
                return ToolResult::error(format!(
                    "ComfyUI did not return prompt_id: {queue_result}"
                ))
            }
        };

        // Poll and save
        match poll_and_save_output(
            &self.server_url,
            &prompt_id,
            output_key,
            timeout_sec,
            &self.output_dir,
        )
        .await
        {
            Ok(info) => ToolResult::success(info),
            Err(e) => ToolResult::error(e),
        }
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Set a node input value in a ComfyUI API-format workflow JSON.
fn set_node_input(workflow: &mut serde_json::Value, node_id: &str, key: &str, value: serde_json::Value) {
    if let Some(inputs) = workflow
        .get_mut(node_id)
        .and_then(|n| n.get_mut("inputs"))
    {
        if let Some(obj) = inputs.as_object_mut() {
            obj.insert(key.to_string(), value);
        }
    }
}

/// Upload raw image bytes to ComfyUI `/upload/image` endpoint.
/// Returns the remote filename for use in LoadImage nodes.
async fn upload_image_bytes_to_comfyui(
    server_url: &str,
    image_bytes: &[u8],
    filename: &str,
) -> Result<String, String> {
    let mime = if filename.ends_with(".jpg") || filename.ends_with(".jpeg") {
        "image/jpeg"
    } else {
        "image/png"
    };

    let file_part = reqwest::multipart::Part::bytes(image_bytes.to_vec())
        .file_name(filename.to_string())
        .mime_str(mime)
        .map_err(|e| format!("Multipart error: {e}"))?;

    let form = reqwest::multipart::Form::new()
        .text("type", "input")
        .part("image", file_part);

    let upload_url = format!("{}/upload/image", server_url);
    let resp = http_client()
        .post(&upload_url)
        .multipart(form)
        .timeout(Duration::from_secs(60))
        .send()
        .await
        .map_err(|e| format!("Upload failed: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("ComfyUI upload error (HTTP {status}): {body}"));
    }

    let info: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("Invalid upload response: {e}"))?;

    // Response: {"name": "file.png", "subfolder": "...", "type": "input"}
    let name = info
        .get("name")
        .and_then(|v| v.as_str())
        .ok_or_else(|| format!("Unexpected upload response: {info}"))?;
    let subfolder = info
        .get("subfolder")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if !subfolder.is_empty() {
        Ok(format!("{subfolder}/{name}"))
    } else {
        Ok(name.to_string())
    }
}

/// Poll `/history/{prompt_id}`, download the output file, save locally, return path.
async fn poll_and_save_output(
    server_url: &str,
    prompt_id: &str,
    output_key: &str,
    timeout_sec: u64,
    output_dir: &Path,
) -> Result<String, String> {
    let start = tokio::time::Instant::now();
    let timeout = Duration::from_secs(timeout_sec);
    let poll_interval = Duration::from_secs(3);

    loop {
        if start.elapsed() > timeout {
            return Err(format!(
                "ComfyUI generation timed out after {timeout_sec}s (prompt_id: {prompt_id})"
            ));
        }

        let history_url = format!("{}/history/{}", server_url, prompt_id);
        let resp = http_client()
            .get(&history_url)
            .timeout(Duration::from_secs(15))
            .send()
            .await
            .map_err(|e| format!("Failed to poll history: {e}"))?;

        if !resp.status().is_success() {
            tokio::time::sleep(poll_interval).await;
            continue;
        }

        let data: serde_json::Value = resp
            .json()
            .await
            .map_err(|e| format!("Invalid history JSON: {e}"))?;

        let history = data.get(prompt_id).or_else(|| {
            if data.get("outputs").is_some() {
                Some(&data)
            } else {
                None
            }
        });

        if let Some(history) = history {
            // Check execution error
            if let Some(status) = history.get("status") {
                if status
                    .get("status_str")
                    .and_then(|v| v.as_str())
                    == Some("error")
                {
                    let msgs = status.get("messages").map(|m| m.to_string()).unwrap_or_default();
                    return Err(format!("ComfyUI execution error: {msgs}"));
                }
            }

            if let Some(outputs) = history.get("outputs").and_then(|o| o.as_object()) {
                for (_node_id, out) in outputs {
                    let out_obj = match out.as_object() {
                        Some(o) => o,
                        None => continue,
                    };

                    let mut candidates: Vec<&serde_json::Value> = Vec::new();
                    if let Some(list) = out_obj.get(output_key).and_then(|v| v.as_array()) {
                        candidates.extend(list.iter());
                    }
                    // VHS_VideoCombine compat: mp4 may appear under "gifs"
                    if output_key == "videos" && candidates.is_empty() {
                        if let Some(gifs) = out_obj.get("gifs").and_then(|v| v.as_array()) {
                            candidates.extend(gifs.iter());
                        }
                    }

                    for entry in &candidates {
                        let filename = match entry.get("filename").and_then(|v| v.as_str()) {
                            Some(f) => f,
                            None => continue,
                        };
                        let subfolder =
                            entry.get("subfolder").and_then(|v| v.as_str()).unwrap_or("");
                        let otype =
                            entry.get("type").and_then(|v| v.as_str()).unwrap_or("output");

                        let file_url = format!(
                            "{}/view?filename={}&subfolder={}&type={}",
                            server_url, filename, subfolder, otype
                        );

                        let file_resp = http_client()
                            .get(&file_url)
                            .timeout(Duration::from_secs(120))
                            .send()
                            .await
                            .map_err(|e| format!("Failed to download output: {e}"))?;

                        if !file_resp.status().is_success() {
                            continue;
                        }

                        let bytes = file_resp
                            .bytes()
                            .await
                            .map_err(|e| format!("Failed to read bytes: {e}"))?;

                        let ext = if output_key == "videos" {
                            "mp4"
                        } else if filename.ends_with(".jpg") || filename.ends_with(".jpeg") {
                            "jpg"
                        } else {
                            "png"
                        };

                        let local_name = format!("{prompt_id}.{ext}");
                        let local_path = output_dir.join(&local_name);

                        tokio::fs::write(&local_path, &bytes)
                            .await
                            .map_err(|e| format!("Failed to save file: {e}"))?;

                        let abs_path = local_path
                            .canonicalize()
                            .unwrap_or(local_path.clone())
                            .to_string_lossy()
                            .to_string();

                        return Ok(format!(
                            "Generation complete.\n\
                             prompt_id: {prompt_id}\n\
                             output_file: {abs_path}\n\
                             size: {} bytes\n\
                             type: {ext}\n\n\
                             Use send_message with attachment_path=\"{abs_path}\" to deliver this to the user.",
                            bytes.len()
                        ));
                    }
                }
            }
        }

        tokio::time::sleep(poll_interval).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_embedded_workflows_parse() {
        let _: serde_json::Value = serde_json::from_str(WORKFLOW_Z_IMAGE).unwrap();
        let _: serde_json::Value = serde_json::from_str(WORKFLOW_QWEN_EDIT).unwrap();
        let _: serde_json::Value = serde_json::from_str(WORKFLOW_LTX_I2V).unwrap();
    }

    #[test]
    fn test_set_node_input() {
        let mut wf = json!({
            "4": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 10}}
        });
        set_node_input(&mut wf, "4", "seed", json!(12345));
        assert_eq!(wf["4"]["inputs"]["seed"], 12345);
    }

    #[test]
    fn test_definition() {
        let tool = ComfyUiTool::new("http://localhost:8188", "/tmp/test");
        assert_eq!(tool.name(), "comfyui_generate");
        let def = tool.definition();
        assert!(def.description.contains("text_to_image"));
        assert!(def.description.contains("send_message"));
        let required = def.input_schema["required"].as_array().unwrap();
        assert!(required.iter().any(|v| v == "workflow_type"));
        assert!(required.iter().any(|v| v == "prompt"));
    }

    #[tokio::test]
    async fn test_missing_params() {
        let tool = ComfyUiTool::new("http://localhost:8188", "/tmp/test");
        let r = tool.execute(json!({})).await;
        assert!(r.is_error);
        assert!(r.content.contains("workflow_type"));

        let r = tool
            .execute(json!({"workflow_type": "image_edit", "prompt": "test"}))
            .await;
        assert!(r.is_error);
        assert!(r.content.contains("image_path or image_base64"));
    }

    #[tokio::test]
    async fn test_unknown_workflow_type() {
        let tool = ComfyUiTool::new("http://localhost:8188", "/tmp/test");
        let r = tool
            .execute(json!({"workflow_type": "unknown", "prompt": "test"}))
            .await;
        assert!(r.is_error);
        assert!(r.content.contains("Unknown workflow_type"));
    }
}
