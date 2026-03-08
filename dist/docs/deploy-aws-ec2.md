# MicroClaw AWS EC2 部署指南

本文档介绍如何在 AWS EC2 实例上从零部署 MicroClaw 聊天机器人。

---

## 目录

1. [前置要求](#1-前置要求)
2. [创建 EC2 实例](#2-创建-ec2-实例)
3. [配置安全组](#3-配置安全组)
4. [连接实例并安装依赖](#4-连接实例并安装依赖)
5. [构建 MicroClaw](#5-构建-microclaw)
6. [配置 MicroClaw](#6-配置-microclaw)
7. [注册为 systemd 服务](#7-注册为-systemd-服务)
8. [配置 Nginx 反向代理与 HTTPS](#8-配置-nginx-反向代理与-https)
9. [验证部署](#9-验证部署)
10. [运维与日志](#10-运维与日志)
11. [更新与热重载](#11-更新与热重载)
12. [备份与恢复](#12-备份与恢复)
13. [常见问题](#13-常见问题)

---

## 1. 前置要求

- 一个 AWS 账户
- 一个 LLM API Key（Anthropic / OpenAI / 其他兼容提供商）
- 至少一个聊天渠道的凭据（Telegram Bot Token / Discord Bot Token / Slack / Feishu / 或仅使用内置 Web UI）
- （可选）一个域名，用于 HTTPS 访问 Web UI

### 推荐实例规格

| 用途 | 实例类型 | vCPU | 内存 | 磁盘 | 预估月费 |
|------|----------|------|------|------|----------|
| 测试/个人 | t3.small | 2 | 2 GB | 20 GB gp3 | ~$15 |
| 生产/多渠道 | t3.medium | 2 | 4 GB | 30 GB gp3 | ~$30 |
| 编译优化（首次构建更快） | t3.large | 2 | 8 GB | 30 GB gp3 | ~$60 |

> **提示**：首次 `cargo build --release` 编译约需 5-10 分钟（t3.medium），增量编译约 30-90 秒。如果只需运行不需在机器上编译，可在本地交叉编译后上传二进制。

---

## 2. 创建 EC2 实例

### 通过 AWS Console

1. 打开 [EC2 控制台](https://console.aws.amazon.com/ec2/)
2. 点击 **Launch Instance**
3. 配置：
   - **Name**: `microclaw`
   - **AMI**: Ubuntu 24.04 LTS (HVM, SSD Volume Type)
   - **Instance type**: `t3.small` 或以上
   - **Key pair**: 选择已有密钥或创建新密钥（用于 SSH）
   - **Storage**: 20 GB gp3（或更多）
   - **Network**: 默认 VPC 即可
4. 点击 **Launch Instance**

### 通过 AWS CLI

```bash
aws ec2 run-instances \
  --image-id ami-0c7217cdde317cfec \
  --instance-type t3.small \
  --key-name your-key-pair \
  --security-group-ids sg-xxxxxxxx \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=microclaw}]' \
  --count 1
```

> AMI ID 因区域而异，请在目标区域查找最新的 Ubuntu 24.04 AMI。

---

## 3. 配置安全组

为实例的安全组添加以下入站规则：

| 类型 | 端口 | 来源 | 用途 |
|------|------|------|------|
| SSH | 22 | 你的 IP | SSH 管理 |
| HTTP | 80 | 0.0.0.0/0 | Web UI（Nginx 反代） |
| HTTPS | 443 | 0.0.0.0/0 | Web UI（HTTPS） |

> **注意**：MicroClaw 的 Web UI 默认监听 `127.0.0.1:10961`，无需对外开放 10961 端口。通过 Nginx 反向代理暴露 80/443 即可。
>
> 如果只使用 Telegram/Discord/Slack/Feishu 而不需要 Web UI，则无需开放 80/443。这些渠道仅需出站 HTTPS 连接。

---

## 4. 连接实例并安装依赖

```bash
ssh -i your-key.pem ubuntu@<EC2-PUBLIC-IP>
```

### 4.1 系统更新与基础工具

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y build-essential pkg-config git curl
```

### 4.2 安装 Rust

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
rustc --version   # 确认安装成功
```

### 4.3 安装 Node.js（用于构建 Web UI）

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
node --version    # v20.x
npm --version     # v10.x
```

### 4.4 安装 Nginx（可选，用于反向代理）

```bash
sudo apt install -y nginx
```

---

## 5. 构建 MicroClaw

### 5.1 获取源码

```bash
cd ~
git clone https://github.com/gavrielc/microclaw.git
cd microclaw
```

### 5.2 构建 Web UI

Web UI 通过 `include_dir!` 宏嵌入到 Rust 二进制中，必须**先于** Rust 编译：

```bash
npm --prefix web ci
npm --prefix web run build
```

### 5.3 编译 Rust 二进制

```bash
cargo build --release
```

编译完成后，二进制位于 `target/release/microclaw`（约 37 MB）。

验证：

```bash
./target/release/microclaw version
```

### 5.4 可选功能

```bash
# 启用语义记忆（embedding 向量搜索）
cargo build --release --features sqlite-vec

# 如果系统 OpenSSL 有兼容问题，使用 vendored 版本
cargo build --release --features openssl-vendored
```

---

## 6. 配置 MicroClaw

### 6.1 方式一：交互式向导（推荐新手）

```bash
./target/release/microclaw setup
```

向导会引导你输入 API Key 和渠道凭据，自动生成 `microclaw.config.yaml`。

### 6.2 方式二：手动编辑配置文件

```bash
cp microclaw.config.example.yaml microclaw.config.yaml
```

编辑 `microclaw.config.yaml`：

```yaml
# ── LLM 配置 ──
llm_provider: "anthropic"
api_key: "sk-ant-..."          # 你的 API Key
model: "claude-sonnet-4-20250514"

# ── 通用配置 ──
data_dir: "./microclaw.data"
working_dir: "./tmp"
timezone: "Asia/Shanghai"       # 你的时区
max_tokens: 8192
max_tool_iterations: 100

# ── Web UI ──
web_enabled: true
web_host: "127.0.0.1"          # 通过 Nginx 反代，无需对外绑定
web_port: 10961
web_auth_token: "your-secret-token"   # 建议设置，保护 Web 接口

# ── 渠道配置（按需启用）──

# Telegram
# telegram_bot_token: "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
# bot_username: "my_bot"

# Discord
# discord_bot_token: "MTIzNDU2Nzg..."

# Slack (Socket Mode)
# channels:
#   slack:
#     bot_token: "xoxb-..."
#     app_token: "xapp-..."

# Feishu / Lark
# channels:
#   feishu:
#     app_id: "cli_xxx"
#     app_secret: "xxx"
#     connection_mode: "websocket"
#     domain: "feishu"
```

### 6.3 环境诊断

```bash
./target/release/microclaw doctor
```

该命令会检查配置文件、API 连通性、渠道凭据等，帮你发现问题。

---

## 7. 注册为 systemd 服务

项目提供了 `microclaw.service.example`，包含重启策略、资源限制和安全加固，可直接使用。

### 7.1 安装服务文件

```bash
sudo cp microclaw.service.example /etc/systemd/system/microclaw.service
```

如果你的项目路径不是 `/home/ubuntu/microclaw`，需要修改服务文件中的路径：

```bash
sudo sed -i 's|/home/ubuntu/microclaw|/your/actual/path|g' /etc/systemd/system/microclaw.service
sudo sed -i 's|/home/ubuntu|/your/home|g' /etc/systemd/system/microclaw.service
```

`microclaw.service.example` 完整内容：

```ini
[Unit]
Description=MicroClaw AI Bot
Documentation=https://github.com/gavrielc/microclaw
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/microclaw
ExecStart=/home/ubuntu/microclaw/target/release/microclaw start

# ── Restart policy ──
Restart=always
RestartSec=3
# Give up after 5 consecutive failures within 60s
StartLimitIntervalSec=60
StartLimitBurst=5

# ── Logging ──
StandardOutput=append:/var/log/microclaw.log
StandardError=append:/var/log/microclaw.log

# ── Environment ──
Environment=RUST_LOG=info
Environment=HOME=/home/ubuntu
# Uncomment to use a custom config path:
# Environment=MICROCLAW_CONFIG=/etc/microclaw/config.yaml

# ── Graceful shutdown ──
# MicroClaw listens for SIGTERM/SIGHUP and drains for 2s
KillSignal=SIGTERM
TimeoutStopSec=10

# ── Resource limits ──
LimitNOFILE=65536
LimitNPROC=4096

# ── Security hardening ──
NoNewPrivileges=true
ProtectHome=read-only
ProtectSystem=strict
# Allow writes to data dir, working dir, and log
ReadWritePaths=/home/ubuntu/microclaw/microclaw.data
ReadWritePaths=/home/ubuntu/microclaw/tmp
ReadWritePaths=/var/log/microclaw.log
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

**关键配置说明**：

| 配置项 | 作用 |
|--------|------|
| `Restart=always` + `RestartSec=3` | 崩溃后 3 秒自动重启 |
| `StartLimitBurst=5` | 60 秒内连续失败 5 次则停止重试，防止无限循环 |
| `LimitNOFILE=65536` | 提高文件描述符上限，支持高并发连接 |
| `NoNewPrivileges=true` | 禁止进程提升权限 |
| `ProtectSystem=strict` | 只读挂载系统目录，仅 `ReadWritePaths` 可写 |
| `ProtectHome=read-only` | 只读挂载 home 目录，仅指定路径可写 |
| `PrivateTmp=true` | 隔离 /tmp，防止与其他服务冲突 |

### 7.2 创建日志文件并设置权限

```bash
sudo touch /var/log/microclaw.log
sudo chown ubuntu:ubuntu /var/log/microclaw.log
```

### 7.3 启动服务

```bash
sudo systemctl daemon-reload
sudo systemctl enable microclaw
sudo systemctl start microclaw
sudo systemctl status microclaw
```

### 7.4 配置 sudoers（可选，用于热重载）

如果需要让 microclaw 进程通过 bash 工具自行重启：

```bash
sudo tee /etc/sudoers.d/microclaw > /dev/null <<'EOF'
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart microclaw
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl stop microclaw
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl start microclaw
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl status microclaw
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl is-active microclaw
EOF
sudo chmod 440 /etc/sudoers.d/microclaw
```

---

## 8. 配置 Nginx 反向代理与 HTTPS

### 8.1 Nginx 反代配置

```bash
sudo tee /etc/nginx/sites-available/microclaw > /dev/null <<'EOF'
server {
    listen 80;
    server_name your-domain.com;   # 替换为你的域名或 EC2 公网 IP

    location / {
        proxy_pass http://127.0.0.1:10961;
        proxy_http_version 1.1;

        # SSE 和 WebSocket 支持
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
        proxy_cache off;

        # 传递真实客户端信息
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE 长连接超时
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/microclaw /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### 8.2 启用 HTTPS（Let's Encrypt）

如果有域名：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.com
```

Certbot 会自动修改 Nginx 配置并设置证书自动续期。

如果仅通过 IP 访问，可跳过此步骤（HTTP 直连）。

---

## 9. 验证部署

### 9.1 检查服务状态

```bash
sudo systemctl status microclaw
```

### 9.2 查看日志

```bash
tail -50 /var/log/microclaw.log
```

### 9.3 测试 Web UI

从浏览器访问：

- 有域名：`https://your-domain.com`
- 无域名：`http://<EC2-PUBLIC-IP>`

如果配置了 `web_auth_token`，Web UI 会要求输入 token。

### 9.4 测试 API 端点

```bash
# 本机测试（不走 Nginx）
curl http://127.0.0.1:10961/api/sessions

# 通过 Nginx 测试
curl http://localhost/api/sessions
```

### 9.5 测试聊天渠道

- **Telegram**：在 Telegram 中找到你的 bot，发送一条消息
- **Discord**：在已邀请 bot 的服务器中 @mention bot
- **Feishu**：在飞书中 @bot 发消息
- **Web UI**：在浏览器中打开 Web 界面，输入消息测试

---

## 10. 运维与日志

### 常用命令

```bash
# 查看服务状态
sudo systemctl status microclaw

# 查看实时日志
tail -f /var/log/microclaw.log

# 重启服务
sudo systemctl restart microclaw

# 停止服务
sudo systemctl stop microclaw

# 查看日志最后 100 行
tail -100 /var/log/microclaw.log
```

### 日志轮转

避免日志文件无限增长：

```bash
sudo tee /etc/logrotate.d/microclaw > /dev/null <<'EOF'
/var/log/microclaw.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    copytruncate
}
EOF
```

### 磁盘监控

```bash
# 查看数据目录大小
du -sh ~/microclaw/microclaw.data/

# 查看 SQLite 数据库大小
ls -lh ~/microclaw/microclaw.data/runtime/microclaw.db
```

---

## 11. 更新与热重载

### 11.1 标准更新流程

```bash
cd ~/microclaw
git pull

# 如果 web/ 有变化，重新构建 Web UI
npm --prefix web ci
npm --prefix web run build

# 重新编译
cargo build --release

# 重启服务
sudo systemctl restart microclaw
```

### 11.2 使用 hot-reload 脚本

项目自带热重载脚本，支持多种模式：

```bash
# 仅编译 + 重启
scripts/hot-reload.sh

# 仅重启（不重新编译）
scripts/hot-reload.sh --restart-only

# 仅编译（不重启）
scripts/hot-reload.sh --build-only
```

脚本会自动备份当前二进制到 `target/release/microclaw.bak`，编译失败时自动恢复。

---

## 12. 备份与恢复

### 12.1 需要备份的内容

| 路径 | 内容 | 重要性 |
|------|------|--------|
| `microclaw.config.yaml` | 配置文件 | 必备 |
| `microclaw.data/` | 数据库、记忆、会话 | 必备 |
| `SOUL.md` | 人格文件 | 推荐 |

### 12.2 自动备份脚本

```bash
#!/bin/bash
# backup-microclaw.sh
BACKUP_DIR="/home/ubuntu/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP_DIR"

tar czf "$BACKUP_DIR/microclaw-$TIMESTAMP.tar.gz" \
  -C /home/ubuntu/microclaw \
  microclaw.config.yaml \
  microclaw.data/ \
  SOUL.md 2>/dev/null

# 保留最近 7 天的备份
find "$BACKUP_DIR" -name "microclaw-*.tar.gz" -mtime +7 -delete

echo "Backup created: $BACKUP_DIR/microclaw-$TIMESTAMP.tar.gz"
```

配合 cron 每天自动备份：

```bash
crontab -e
# 添加：每天凌晨 3 点备份
0 3 * * * /home/ubuntu/backup-microclaw.sh
```

### 12.3 恢复

```bash
cd ~/microclaw
tar xzf /home/ubuntu/backups/microclaw-YYYYMMDD_HHMMSS.tar.gz
sudo systemctl restart microclaw
```

---

## 13. 常见问题

### Q: 编译时内存不足 (OOM)

**A**: `t3.micro`（1 GB 内存）编译 Rust 可能 OOM。解决方案：

```bash
# 创建 2GB swap
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

或使用更大的实例类型（t3.small 2GB+）。

### Q: Web UI 打开空白

**A**: 可能没有先构建 Web UI 就编译了 Rust。重新构建：

```bash
npm --prefix web ci
npm --prefix web run build
cargo build --release
sudo systemctl restart microclaw
```

### Q: Telegram bot 不回复

**A**: 检查以下几点：

1. 日志中是否有连接错误：`tail -50 /var/log/microclaw.log`
2. API Key 是否正确
3. EC2 安全组是否允许出站 HTTPS（443）
4. 在群组中需要 @mention bot 才会回复

### Q: 如何更换 LLM 提供商

**A**: 编辑 `microclaw.config.yaml`，修改 `llm_provider`、`api_key`、`model`，然后重启：

```bash
sudo systemctl restart microclaw
```

支持的提供商：`anthropic`、`openai`、`openrouter`、`ollama`、`google`、`deepseek`、`bedrock`、`azure`、`mistral`、`xai`、`together` 等。

### Q: 如何使用自定义域名

**A**: 将域名 A 记录指向 EC2 公网 IP，然后按 [第 8 节](#8-配置-nginx-反向代理与-https) 配置 Nginx 和 Let's Encrypt。

### Q: SQLite 数据库损坏怎么办

**A**: MicroClaw 使用 WAL 模式，损坏概率极低。如果发生：

```bash
sudo systemctl stop microclaw
cd ~/microclaw/microclaw.data/runtime/
sqlite3 microclaw.db ".recover" | sqlite3 microclaw-recovered.db
mv microclaw.db microclaw.db.corrupted
mv microclaw-recovered.db microclaw.db
sudo systemctl start microclaw
```

### Q: 如何限制 Web UI 访问

**A**: 在配置中设置 `web_auth_token`：

```yaml
web_auth_token: "a-strong-random-token"
```

访问 Web UI 时需要输入此 token。配合 Nginx + HTTPS 使用效果更佳。
