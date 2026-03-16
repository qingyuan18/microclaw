//! S3 workspace sync — persist runtime data (SQLite DB, memory, skills) to/from S3.
//!
//! On container start:  restore from `s3://{bucket}/{namespace}/` → local `data_dir/runtime/`
//! On periodic timer:   save local → S3 (every `sync_interval`)
//! On SIGTERM:          final save → S3 (with timeout)
//!
//! Only active when `agentcore_mode = true` and `S3_DATA_BUCKET` env var is set.

use std::path::PathBuf;
#[cfg(feature = "agentcore")]
use std::path::Path;
use std::sync::Arc;
use tokio::sync::Notify;
use tracing::{error, info};
#[cfg(feature = "agentcore")]
use tracing::warn;

/// S3 sync configuration, resolved from environment variables.
#[derive(Clone, Debug)]
pub struct S3SyncConfig {
    pub bucket: String,
    /// S3 key prefix, e.g. "bot_my-assistant/" (derived from BOT_ID or MICROCLAW_S3_PREFIX).
    pub prefix: String,
    /// Local directory to sync (the runtime data dir).
    pub local_dir: PathBuf,
    /// Sync interval in seconds (default 300 = 5 min).
    pub sync_interval_secs: u64,
}

impl S3SyncConfig {
    /// Build config from environment variables. Returns None if S3_DATA_BUCKET is not set.
    pub fn from_env(local_dir: &str) -> Option<Self> {
        let bucket = std::env::var("S3_DATA_BUCKET").ok()?;
        if bucket.trim().is_empty() {
            return None;
        }

        let prefix = std::env::var("MICROCLAW_S3_PREFIX")
            .or_else(|_| std::env::var("BOT_ID").map(|id| format!("{id}/")))
            .unwrap_or_default();

        let sync_interval_secs = std::env::var("S3_SYNC_INTERVAL_SECS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(300);

        Some(Self {
            bucket,
            prefix,
            local_dir: PathBuf::from(local_dir),
            sync_interval_secs,
        })
    }
}

/// Files/directories to skip when syncing.
#[cfg(feature = "agentcore")]
const SKIP_NAMES: &[&str] = &[
    "microclaw.db-wal",
    "microclaw.db-shm",
    ".DS_Store",
];

/// Max file size to sync (50 MB).
#[cfg(feature = "agentcore")]
const MAX_FILE_SIZE: u64 = 50 * 1024 * 1024;

// ---------------------------------------------------------------------------
// S3 operations (only compiled with the `agentcore` feature)
// ---------------------------------------------------------------------------

#[cfg(feature = "agentcore")]
mod s3_ops {
    use super::*;
    use aws_sdk_s3::Client as S3Client;
    use aws_sdk_s3::primitives::ByteStream;

    /// Create an S3 client using default credential chain (IAM role in AgentCore).
    pub async fn create_client() -> S3Client {
        let config = aws_config::load_defaults(aws_config::BehaviorVersion::latest()).await;
        S3Client::new(&config)
    }

    /// Restore files from S3 to local directory.
    pub async fn restore(client: &S3Client, cfg: &S3SyncConfig) -> anyhow::Result<u64> {
        let mut count = 0u64;
        let mut continuation_token: Option<String> = None;

        loop {
            let mut req = client
                .list_objects_v2()
                .bucket(&cfg.bucket)
                .prefix(&cfg.prefix);

            if let Some(token) = continuation_token.take() {
                req = req.continuation_token(token);
            }

            let resp = req.send().await?;

            if let Some(objects) = resp.contents() {
                for obj in objects {
                    let Some(key) = obj.key() else { continue };
                    let rel = key.strip_prefix(&cfg.prefix).unwrap_or(key);
                    if rel.is_empty() || rel.ends_with('/') {
                        continue;
                    }

                    let local_path = cfg.local_dir.join(rel);
                    if let Some(parent) = local_path.parent() {
                        tokio::fs::create_dir_all(parent).await?;
                    }

                    let get_resp = client
                        .get_object()
                        .bucket(&cfg.bucket)
                        .key(key)
                        .send()
                        .await?;

                    let body = get_resp.body.collect().await?.into_bytes();
                    tokio::fs::write(&local_path, &body).await?;
                    count += 1;
                }
            }

            if resp.is_truncated() == Some(true) {
                continuation_token = resp.next_continuation_token().map(|s| s.to_string());
            } else {
                break;
            }
        }

        Ok(count)
    }

    /// Save local files to S3.
    pub async fn save(client: &S3Client, cfg: &S3SyncConfig) -> anyhow::Result<u64> {
        let mut count = 0u64;
        save_dir(client, cfg, &cfg.local_dir, &mut count).await?;
        Ok(count)
    }

    async fn save_dir(
        client: &S3Client,
        cfg: &S3SyncConfig,
        dir: &Path,
        count: &mut u64,
    ) -> anyhow::Result<()> {
        let mut entries = match tokio::fs::read_dir(dir).await {
            Ok(e) => e,
            Err(e) => {
                warn!("Cannot read dir {}: {e}", dir.display());
                return Ok(());
            }
        };

        while let Some(entry) = entries.next_entry().await? {
            let path = entry.path();
            let name = entry.file_name();
            let name_str = name.to_string_lossy();

            if SKIP_NAMES.iter().any(|s| *s == name_str.as_ref()) {
                continue;
            }

            let meta = entry.metadata().await?;
            if meta.is_dir() {
                Box::pin(save_dir(client, cfg, &path, count)).await?;
            } else if meta.is_file() {
                if meta.len() > MAX_FILE_SIZE {
                    warn!("Skipping large file ({}B): {}", meta.len(), path.display());
                    continue;
                }

                let rel = path
                    .strip_prefix(&cfg.local_dir)
                    .unwrap_or(&path)
                    .to_string_lossy()
                    .replace('\\', "/");
                let key = format!("{}{}", cfg.prefix, rel);

                let body = tokio::fs::read(&path).await?;
                client
                    .put_object()
                    .bucket(&cfg.bucket)
                    .key(&key)
                    .body(ByteStream::from(body))
                    .send()
                    .await?;

                *count += 1;
            }
        }

        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Stub when `agentcore` feature is not enabled
// ---------------------------------------------------------------------------

#[cfg(not(feature = "agentcore"))]
mod s3_ops {
    use super::*;

    pub struct S3Client;

    pub async fn create_client() -> S3Client {
        S3Client
    }

    pub async fn restore(_client: &S3Client, _cfg: &S3SyncConfig) -> anyhow::Result<u64> {
        Ok(0)
    }

    pub async fn save(_client: &S3Client, _cfg: &S3SyncConfig) -> anyhow::Result<u64> {
        Ok(0)
    }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Restore runtime data from S3 on container start.
/// Returns the number of files restored, or 0 if S3 sync is not configured.
pub async fn restore_from_s3(cfg: &S3SyncConfig) -> u64 {
    info!(
        bucket = %cfg.bucket,
        prefix = %cfg.prefix,
        local_dir = %cfg.local_dir.display(),
        "S3 sync: restoring workspace"
    );

    let client = s3_ops::create_client().await;
    match s3_ops::restore(&client, cfg).await {
        Ok(count) => {
            info!("S3 sync: restored {count} files");
            count
        }
        Err(e) => {
            error!("S3 sync: restore failed: {e}");
            0
        }
    }
}

/// Save runtime data to S3.
pub async fn save_to_s3(cfg: &S3SyncConfig) -> u64 {
    let client = s3_ops::create_client().await;
    match s3_ops::save(&client, cfg).await {
        Ok(count) => {
            info!("S3 sync: saved {count} files");
            count
        }
        Err(e) => {
            error!("S3 sync: save failed: {e}");
            0
        }
    }
}

/// Spawn the periodic S3 sync background task.
/// Returns a `Notify` that can be used to trigger an immediate save (e.g. on SIGTERM).
pub fn spawn_periodic_sync(cfg: S3SyncConfig) -> Arc<Notify> {
    let shutdown = Arc::new(Notify::new());
    let shutdown_rx = shutdown.clone();

    tokio::spawn(async move {
        let interval = std::time::Duration::from_secs(cfg.sync_interval_secs);
        loop {
            tokio::select! {
                _ = tokio::time::sleep(interval) => {
                    save_to_s3(&cfg).await;
                }
                _ = shutdown_rx.notified() => {
                    info!("S3 sync: shutdown signal received, performing final save");
                    save_to_s3(&cfg).await;
                    return;
                }
            }
        }
    });

    shutdown
}
