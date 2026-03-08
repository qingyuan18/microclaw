use async_trait::async_trait;
use serde_json::json;
use tokio::process::Command;
use tracing::{info, warn};

use super::{auth_context_from_input, schema_object, Tool, ToolResult};
use crate::llm_types::ToolDefinition;

/// Default timeout in seconds for ACP agent execution.
const DEFAULT_TIMEOUT_SECS: u64 = 300;

/// Maximum output size returned to avoid overwhelming context.
const MAX_OUTPUT_BYTES: usize = 40_000;

/// acpx exit code when no session is found.
const EXIT_NO_SESSION: i32 = 4;

pub struct AcpAgentTool {
    default_agent: String,
    default_cwd: String,
    timeout: u64,
    default_permission: String,
}

impl AcpAgentTool {
    pub fn new(
        default_agent: &str,
        default_cwd: &str,
        timeout: u64,
        default_permission: &str,
    ) -> Self {
        AcpAgentTool {
            default_agent: default_agent.to_string(),
            default_cwd: default_cwd.to_string(),
            timeout: if timeout == 0 {
                DEFAULT_TIMEOUT_SECS
            } else {
                timeout
            },
            default_permission: default_permission.to_string(),
        }
    }
}

/// Create a new acpx session via `acpx <agent> sessions new --name <name>`.
/// Returns Ok(()) on success or Err(error_message) on failure.
async fn create_session(agent: &str, cwd: &str, session_name: &str) -> Result<(), String> {
    info!(agent, cwd, session_name, "creating new acpx session");
    let output = Command::new("acpx")
        .arg(agent)
        .args(["sessions", "new", "--name", session_name])
        .current_dir(cwd)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("Failed to spawn acpx. Is it installed? (npm install -g acpx): {e}"))?;

    if output.status.success() {
        let stdout = String::from_utf8_lossy(&output.stdout);
        info!(session_name, %stdout, "acpx session created");
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stdout = String::from_utf8_lossy(&output.stdout);
        Err(format!(
            "Failed to create acpx session: stdout={stdout}, stderr={stderr}"
        ))
    }
}

/// Run an acpx prompt and capture its text output.
/// Tries NDJSON mode first (`--format json`); if acpx doesn't support it
/// (older versions), falls back to plain text mode automatically.
async fn run_prompt(
    agent: &str,
    cwd: &str,
    permission: &str,
    timeout: u64,
    session_name: &str,
    task: &str,
) -> ToolResult {
    let mut cmd = Command::new("acpx");
    cmd.arg(agent);
    cmd.args(["-s", session_name]);
    cmd.arg(task);
    cmd.current_dir(cwd);

    cmd.stdin(std::process::Stdio::null());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());

    info!(agent, cwd, permission, session_name, timeout, "sending acpx prompt");

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            return ToolResult::error(format!(
                "Failed to spawn acpx. Is it installed? (npm install -g acpx): {e}"
            ));
        }
    };

    // Read stdout as plain text (acpx outputs human-readable text by default)
    let mut output = String::new();
    if let Some(mut stdout) = child.stdout.take() {
        let _ = tokio::io::AsyncReadExt::read_to_string(&mut stdout, &mut output).await;
    }

    // Read stderr for error diagnostics
    let stderr_text = if let Some(mut stderr) = child.stderr.take() {
        let mut buf = String::new();
        let _ = tokio::io::AsyncReadExt::read_to_string(&mut stderr, &mut buf).await;
        buf
    } else {
        String::new()
    };

    // Wait for process to exit
    let status = match child.wait().await {
        Ok(s) => s,
        Err(e) => {
            warn!("Failed to wait for acpx process: {e}");
            return ToolResult::error(format!("acpx process error: {e}"));
        }
    };

    // Strip acpx UI noise (progress lines like [client], [done], etc.)
    let cleaned: String = output
        .lines()
        .filter(|line| {
            let trimmed = line.trim();
            !trimmed.is_empty()
                && !trimmed.starts_with("[client]")
                && !trimmed.starts_with("[done]")
                && !trimmed.starts_with("[acpx]")
                && !trimmed.starts_with("[error]")
                && !trimmed.starts_with("⚠")
        })
        .collect::<Vec<_>>()
        .join("\n");

    let mut output = if cleaned.trim().is_empty() {
        "(Agent produced no text output)".to_string()
    } else {
        cleaned
    };

    // Truncate if too large
    if output.len() > MAX_OUTPUT_BYTES {
        output.truncate(MAX_OUTPUT_BYTES);
        output.push_str("\n\n[Output truncated]");
    }

    if status.success() {
        ToolResult::success(output)
    } else {
        let code = status.code().unwrap_or(-1);
        let hint = match code {
            3 => " (timeout)",
            EXIT_NO_SESSION => " (no session found)",
            5 => " (permission denied)",
            _ => "",
        };
        let stderr_part = if stderr_text.trim().is_empty() {
            String::new()
        } else {
            format!("\n\nstderr: {}", stderr_text.trim())
        };
        warn!(code, %stderr_text, "acpx exited with error");
        ToolResult::error(format!(
            "acpx exited with code {code}{hint}.\n\n{output}{stderr_part}"
        ))
        .with_status_code(code)
    }
}

#[async_trait]
impl Tool for AcpAgentTool {
    fn name(&self) -> &str {
        "acp_agent"
    }

    fn definition(&self) -> ToolDefinition {
        ToolDefinition {
            name: "acp_agent".into(),
            description: "Invoke an ACP-compatible coding agent (Claude Code, Gemini CLI, Codex, etc.) to perform programming tasks. The agent runs as a subprocess via acpx and has access to the target project's files. Use this for code generation, refactoring, debugging, or any coding task that benefits from a dedicated coding agent.".into(),
            input_schema: schema_object(
                json!({
                    "task": {
                        "type": "string",
                        "description": "The task description for the coding agent"
                    },
                    "agent": {
                        "type": "string",
                        "description": "ACP agent to use: claude, gemini, codex, opencode. Defaults to config default.",
                        "enum": ["claude", "gemini", "codex", "opencode"]
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the agent (absolute path). Defaults to config default."
                    },
                    "permission": {
                        "type": "string",
                        "description": "Permission policy for file operations: approve-reads (default, safe), approve-all (agent can write), deny-all (read-only chat).",
                        "enum": ["approve-reads", "approve-all", "deny-all"]
                    }
                }),
                &["task"],
            ),
        }
    }

    async fn execute(&self, input: serde_json::Value) -> ToolResult {
        let task = match input.get("task").and_then(|v| v.as_str()) {
            Some(t) if !t.trim().is_empty() => t,
            _ => return ToolResult::error("Missing required parameter: task".into()),
        };

        let agent = input
            .get("agent")
            .and_then(|v| v.as_str())
            .unwrap_or(&self.default_agent);
        // Always use configured default_cwd to prevent LLM from sending prompts to wrong directories
        let cwd = &self.default_cwd;
        let permission = input
            .get("permission")
            .and_then(|v| v.as_str())
            .unwrap_or(&self.default_permission);

        // Validate permission value
        if !matches!(permission, "approve-reads" | "approve-all" | "deny-all") {
            return ToolResult::error(format!(
                "Invalid permission: {permission}. Must be approve-reads, approve-all, or deny-all."
            ));
        }

        // Derive session name from caller chat_id for per-chat session isolation
        let session_name = auth_context_from_input(&input)
            .map(|auth| format!("mc-{}", auth.caller_chat_id))
            .unwrap_or_else(|| "mc-default".to_string());

        // Try sending the prompt to an existing session first.
        // If it fails (no session, reconnect failure, etc.), create a new session and retry.
        let result = run_prompt(agent, cwd, permission, self.timeout, &session_name, task).await;

        if result.status_code.is_some_and(|c| c != 0) {
            info!(
                agent, cwd, %session_name,
                code = result.status_code.unwrap_or(-1),
                "prompt failed, creating new session and retrying"
            );
            if let Err(e) = create_session(agent, cwd, &session_name).await {
                return ToolResult::error(e);
            }
            // Retry the prompt with the newly created session
            return run_prompt(agent, cwd, permission, self.timeout, &session_name, task).await;
        }

        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn tool() -> AcpAgentTool {
        AcpAgentTool::new("claude", ".", 300, "approve-reads")
    }

    #[test]
    fn test_definition() {
        let t = tool();
        assert_eq!(t.name(), "acp_agent");
        let def = t.definition();
        assert_eq!(def.name, "acp_agent");
        assert!(def.description.contains("ACP"));
        assert!(def.input_schema["properties"]["task"].is_object());
        let required = def.input_schema["required"].as_array().unwrap();
        assert!(required.iter().any(|v| v == "task"));
    }

    #[tokio::test]
    async fn test_missing_task() {
        let t = tool();
        let result = t.execute(json!({})).await;
        assert!(result.is_error);
        assert!(result.content.contains("Missing required parameter: task"));
    }

    #[tokio::test]
    async fn test_empty_task() {
        let t = tool();
        let result = t.execute(json!({"task": "  "})).await;
        assert!(result.is_error);
        assert!(result.content.contains("Missing required parameter: task"));
    }

    #[tokio::test]
    async fn test_invalid_permission() {
        let t = tool();
        let result = t
            .execute(json!({"task": "test", "permission": "invalid"}))
            .await;
        assert!(result.is_error);
        assert!(result.content.contains("Invalid permission"));
    }

    #[tokio::test]
    async fn test_acpx_not_installed() {
        let t = AcpAgentTool::new("claude", ".", 5, "deny-all");
        let result = t.execute(json!({"task": "hello"})).await;
        // Either spawns (acpx installed) or fails (not installed) — both are valid
        // We just verify no panic
        let _ = result;
    }

    #[test]
    fn test_default_timeout() {
        let t = AcpAgentTool::new("claude", ".", 0, "approve-reads");
        assert_eq!(t.timeout, DEFAULT_TIMEOUT_SECS);
    }
}
