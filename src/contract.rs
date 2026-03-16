//! AgentCore Runtime contract layer.
//!
//! Implements the required HTTP protocol for Amazon Bedrock AgentCore Runtime:
//!   - `GET  /ping`        -> Health check (returns "Healthy")
//!   - `POST /invocations` -> Chat message handler
//!
//! MicroClaw is Rust — cold start < 1s, so no lightweight-agent shim is needed.
//! The contract server binds on port 8080 (AgentCore requirement) and routes
//! incoming messages through the standard `process_with_agent` agentic loop.

use std::sync::Arc;

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tracing::{error, info, warn};

use crate::agent_engine::{process_with_agent, AgentRequestContext};
use crate::channel::deliver_and_store_bot_message;
use crate::db::{call_blocking, StoredMessage};
use crate::runtime::AppState;

/// AgentCore requires the contract server on this port.
pub const AGENTCORE_CONTRACT_PORT: u16 = 8080;

// ---------------------------------------------------------------------------
// Request / Response types
// ---------------------------------------------------------------------------

/// Inbound payload from the Router Lambda via `invoke_agent_runtime`.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InvocationRequest {
    /// Action type — currently only "chat" is supported.
    #[serde(default = "default_action")]
    pub action: String,

    /// Internal user ID (from DynamoDB identity table, e.g. "user_abc123").
    pub user_id: String,

    /// Channel-scoped actor ID used as the S3/DynamoDB namespace
    /// (e.g. "telegram_123456", "feishu_oc_xxx").
    pub actor_id: String,

    /// Source channel name (e.g. "telegram", "slack", "feishu").
    pub channel: String,

    /// The channel-specific chat/conversation ID where the reply should go
    /// (e.g. Feishu chat_id "oc_xxx", Telegram chat_id "123456").
    /// The Router Lambda uses this to send the response back to the correct chat.
    #[serde(default)]
    pub external_chat_id: Option<String>,

    /// The user's message. Can be a plain string or a structured object
    /// with text + image references.
    pub message: serde_json::Value,

    /// Optional display name for the sender.
    #[serde(default)]
    pub sender_name: Option<String>,
}

fn default_action() -> String {
    "chat".into()
}

/// Response returned to the Router Lambda.
#[derive(Debug, Serialize)]
pub struct InvocationResponse {
    pub response: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

/// `GET /ping` — AgentCore health check.
///
/// Must return quickly. Returns "Healthy" to signal the container is ready
/// to accept invocations. AgentCore uses this to manage idle termination.
async fn ping() -> impl IntoResponse {
    Json(json!({ "status": "Healthy" }))
}

/// `POST /invocations` — Main message entry point.
///
/// Receives a chat message from the Router Lambda, resolves/creates the
/// internal chat, runs the agentic loop, and returns the response.
async fn invocations(
    State(state): State<Arc<AppState>>,
    Json(req): Json<InvocationRequest>,
) -> Result<Json<InvocationResponse>, (StatusCode, Json<InvocationResponse>)> {
    if req.action != "chat" {
        return Err((
            StatusCode::BAD_REQUEST,
            Json(InvocationResponse {
                response: String::new(),
                error: Some(format!("unsupported action: {}", req.action)),
            }),
        ));
    }

    let message_text = extract_message_text(&req.message);
    if message_text.trim().is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            Json(InvocationResponse {
                response: String::new(),
                error: Some("empty message".into()),
            }),
        ));
    }

    info!(
        user_id = %req.user_id,
        actor_id = %req.actor_id,
        channel = %req.channel,
        msg_len = message_text.len(),
        "Contract: invocation received"
    );

    // Extract image data if present in the structured message.
    let image_data = extract_image_data(&req.message);

    // Derive a chat_type string for DB storage (e.g. "agentcore_telegram").
    let chat_type = format!("agentcore_{}", req.channel);

    // Resolve or create the internal chat_id using channel + actor_id as the
    // external identity. This maps the AgentCore user namespace to a MicroClaw
    // chat that persists across container restarts (once S3 sync is added).
    let channel = req.channel.clone();
    let actor_id = req.actor_id.clone();
    let chat_type_for_db = chat_type.clone();
    let sender_display = req
        .sender_name
        .clone()
        .unwrap_or_else(|| req.actor_id.clone());
    let chat_id = call_blocking(state.db.clone(), move |db| {
        db.resolve_or_create_chat_id(
            &channel,
            &actor_id,
            Some(&sender_display),
            &chat_type_for_db,
        )
    })
    .await
    .map_err(|e| {
        error!("Failed to resolve chat_id: {e}");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(InvocationResponse {
                response: String::new(),
                error: Some(format!("chat resolution failed: {e}")),
            }),
        )
    })?;

    // Store the user's message.
    let sender_name = req
        .sender_name
        .unwrap_or_else(|| req.actor_id.clone());
    let user_msg = StoredMessage {
        id: uuid::Uuid::new_v4().to_string(),
        chat_id,
        sender_name,
        content: message_text,
        is_from_bot: false,
        timestamp: chrono::Utc::now().to_rfc3339(),
    };
    call_blocking(state.db.clone(), move |db| db.store_message(&user_msg))
        .await
        .map_err(|e| {
            error!("Failed to store user message: {e}");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Json(InvocationResponse {
                    response: String::new(),
                    error: Some(format!("message storage failed: {e}")),
                }),
            )
        })?;

    // Run the agentic loop.
    let response = process_with_agent(
        &state,
        AgentRequestContext {
            caller_channel: &req.channel,
            chat_id,
            chat_type: &chat_type,
        },
        None,
        image_data,
    )
    .await
    .map_err(|e| {
        error!("Agent processing failed: {e}");
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(InvocationResponse {
                response: String::new(),
                error: Some(format!("agent error: {e}")),
            }),
        )
    })?;

    // Store the bot's response.
    let bot_username = state.config.bot_username.clone();
    deliver_and_store_bot_message(
        &state.channel_registry,
        state.db.clone(),
        &bot_username,
        chat_id,
        &response,
    )
    .await
    .map_err(|e| {
        warn!("Failed to store bot response (non-fatal): {e}");
        // Non-fatal — the response was already generated, still return it.
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(InvocationResponse {
                response: response.clone(),
                error: Some(format!("response storage failed: {e}")),
            }),
        )
    })?;

    info!(
        chat_id = chat_id,
        response_len = response.len(),
        "Contract: invocation completed"
    );

    Ok(Json(InvocationResponse {
        response,
        error: None,
    }))
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Extract plain text from the message field.
///
/// The message can be either a plain JSON string or a structured object
/// like `{"text": "hello", "images": [...]}`.
fn extract_message_text(message: &serde_json::Value) -> String {
    match message {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Object(map) => map
            .get("text")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        _ => message.to_string(),
    }
}

/// Extract image data from a structured message if present.
///
/// Expects format: `{"text": "...", "images": [{"url": "s3://...", "media_type": "image/jpeg"}]}`
/// Returns `Some((base64_data, media_type))` for the first image, or `None`.
fn extract_image_data(message: &serde_json::Value) -> Option<(String, String)> {
    let obj = message.as_object()?;
    let images = obj.get("images")?.as_array()?;
    let first = images.first()?.as_object()?;

    // If the image has inline base64 data, use it directly.
    if let (Some(data), Some(media_type)) = (
        first.get("data").and_then(|v| v.as_str()),
        first.get("media_type").and_then(|v| v.as_str()),
    ) {
        return Some((data.to_string(), media_type.to_string()));
    }

    // S3 URL images will be handled when S3 integration is added (Phase 2).
    // For now, log and skip.
    if let Some(url) = first.get("url").and_then(|v| v.as_str()) {
        warn!("Image URL received but S3 fetch not yet implemented: {url}");
    }

    None
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

/// Build the AgentCore contract router.
fn build_contract_router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/ping", get(ping))
        .route("/invocations", post(invocations))
        .with_state(state)
}

/// Start the AgentCore contract server on port 8080.
///
/// This MUST be the first thing to bind — AgentCore health-checks `/ping`
/// very quickly after container start. MicroClaw's Rust binary starts in
/// < 1 second, so no lightweight-agent shim is needed.
pub async fn start_contract_server(state: Arc<AppState>) {
    let router = build_contract_router(state);
    let addr = format!("0.0.0.0:{AGENTCORE_CONTRACT_PORT}");

    let listener = match tokio::net::TcpListener::bind(&addr).await {
        Ok(listener) => listener,
        Err(e) => {
            error!(
                "Failed to bind AgentCore contract server at {addr}: {e}. \
                 AgentCore health checks will fail!"
            );
            return;
        }
    };

    info!("AgentCore contract server listening on {addr}");
    if let Err(e) = axum::serve(listener, router).await {
        error!("AgentCore contract server error: {e}");
    }
}
