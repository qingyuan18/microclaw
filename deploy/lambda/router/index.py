"""MicroClaw Router Lambda — Webhook ingestion for Telegram, Slack, Feishu.

Receives webhook events via API Gateway, looks up bot routing in DynamoDB,
invokes the per-bot AgentCore Runtime session, and sends responses back.

MicroClaw uses a per-bot model: one AgentCore Runtime per bot instance,
with a fixed session_id that persists across container restarts.

DynamoDB routing table schema:
  PK: ROUTE#{channel}:{channel_user_id}   SK: CONFIG
  Attributes: bot_id, session_id, bot_token, display_name, ...

Path routing:
  POST /webhook/telegram  — Telegram Bot API webhook
  POST /webhook/slack     — Slack Events API webhook
  POST /webhook/feishu    — Feishu/Lark webhook
  GET  /health            — Health check
"""

import json
import logging
import os
import time
import uuid
from urllib import request as urllib_request

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration ---
AGENTCORE_RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
AGENTCORE_QUALIFIER = os.environ["AGENTCORE_QUALIFIER"]
ROUTING_TABLE_NAME = os.environ["ROUTING_TABLE_NAME"]
AWS_REGION = os.environ.get("AWS_REGION_NAME", os.environ.get("AWS_REGION", "us-west-2"))
LAMBDA_TIMEOUT_SECONDS = int(os.environ.get("LAMBDA_TIMEOUT_SECONDS", "300"))

# --- AWS Clients ---
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
routing_table = dynamodb.Table(ROUTING_TABLE_NAME)
agentcore_client = boto3.client(
    "bedrock-agentcore",
    region_name=AWS_REGION,
    config=Config(
        read_timeout=LAMBDA_TIMEOUT_SECONDS - 5,
        retries={"max_attempts": 0},
    ),
)
secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)

# --- Secrets cache (warm across Lambda invocations) ---
_secrets_cache = {}


def get_secret(secret_id):
    """Fetch a secret value with simple in-memory caching."""
    if secret_id in _secrets_cache:
        return _secrets_cache[secret_id]
    try:
        resp = secrets_client.get_secret_value(SecretId=secret_id)
        val = resp["SecretString"]
        _secrets_cache[secret_id] = val
        return val
    except ClientError as e:
        logger.error("Failed to fetch secret %s: %s", secret_id, e)
        return None


# ---------------------------------------------------------------------------
# DynamoDB routing
# ---------------------------------------------------------------------------

def get_route(channel, channel_id):
    """Look up routing config for a channel user/chat."""
    pk = f"ROUTE#{channel}:{channel_id}"
    try:
        resp = routing_table.get_item(Key={"PK": pk, "SK": "CONFIG"})
        return resp.get("Item")
    except ClientError as e:
        logger.error("DynamoDB route lookup failed: %s", e)
        return None


def get_or_create_session(bot_id):
    """Get or create a session_id for a bot. Session IDs must be >= 33 chars."""
    pk = f"BOT#{bot_id}"
    try:
        resp = routing_table.get_item(Key={"PK": pk, "SK": "SESSION"})
        if "Item" in resp:
            routing_table.update_item(
                Key={"PK": pk, "SK": "SESSION"},
                UpdateExpression="SET lastActivity = :now",
                ExpressionAttributeValues={
                    ":now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                },
            )
            return resp["Item"]["sessionId"]
    except ClientError as e:
        logger.error("DynamoDB session lookup failed: %s", e)

    # Create new session (>= 33 chars required by AgentCore)
    session_id = f"ses_{bot_id}_{uuid.uuid4().hex[:16]}"
    if len(session_id) < 33:
        session_id += "_" + uuid.uuid4().hex[:33 - len(session_id)]
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        routing_table.put_item(
            Item={
                "PK": pk,
                "SK": "SESSION",
                "sessionId": session_id,
                "botId": bot_id,
                "createdAt": now_iso,
                "lastActivity": now_iso,
            }
        )
    except ClientError as e:
        logger.error("Failed to create session: %s", e)

    logger.info("New session for bot %s: %s", bot_id, session_id)
    return session_id


# ---------------------------------------------------------------------------
# AgentCore invocation
# ---------------------------------------------------------------------------

def invoke_agent_runtime(session_id, actor_id, channel, message,
                         sender_name=None, external_chat_id=None):
    """Invoke the AgentCore Runtime container's /invocations endpoint."""
    payload = {
        "action": "chat",
        "userId": actor_id,
        "actorId": actor_id,
        "channel": channel,
        "message": message,
    }
    if sender_name:
        payload["senderName"] = sender_name
    if external_chat_id:
        payload["externalChatId"] = external_chat_id

    try:
        logger.info(
            "Invoking AgentCore: session=%s actor=%s channel=%s",
            session_id, actor_id, channel,
        )
        resp = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            qualifier=AGENTCORE_QUALIFIER,
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode(),
            contentType="application/json",
            accept="application/json",
        )

        body = resp.get("response")
        if body:
            if hasattr(body, "read"):
                body_text = body.read(500_000).decode("utf-8", errors="replace")
            else:
                body_text = str(body)[:500_000]
            try:
                return json.loads(body_text)
            except json.JSONDecodeError:
                return {"response": body_text}

        logger.warning("AgentCore returned no response body")
        return {"response": "", "error": "no response"}

    except Exception as e:
        logger.error("AgentCore invocation failed: %s", e)
        return {"response": "", "error": str(e)}


# ---------------------------------------------------------------------------
# Channel handlers
# ---------------------------------------------------------------------------

def send_telegram_message(bot_token, chat_id, text):
    """Send a message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib_request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error("Telegram send failed: %s", e)


def handle_telegram(body):
    """Handle a Telegram webhook event."""
    update = json.loads(body) if isinstance(body, str) else body
    message = update.get("message", {})
    text = message.get("text", "")
    if not text:
        return {"statusCode": 200, "body": "ok"}

    chat_id = str(message["chat"]["id"])
    user_id = str(message["from"]["id"])
    sender_name = message["from"].get("first_name", user_id)

    # Look up route by chat_id (group) or user_id (DM)
    route = get_route("telegram", chat_id) or get_route("telegram", user_id)
    if not route:
        logger.warning("No route for telegram:%s", chat_id)
        return {"statusCode": 200, "body": "no route"}

    bot_token = route.get("botToken") or get_secret(route.get("botTokenSecretId", ""))
    if not bot_token:
        logger.error("No bot token for route telegram:%s", chat_id)
        return {"statusCode": 200, "body": "no token"}

    session_id = get_or_create_session(route["botId"])
    result = invoke_agent_runtime(
        session_id=session_id,
        actor_id=f"telegram_{chat_id}",
        channel="telegram",
        message=text,
        sender_name=sender_name,
        external_chat_id=chat_id,
    )

    response_text = result.get("response", "")
    if response_text:
        send_telegram_message(bot_token, chat_id, response_text)

    return {"statusCode": 200, "body": "ok"}


def handle_feishu(body, headers=None):
    """Handle a Feishu/Lark webhook event."""
    event = json.loads(body) if isinstance(body, str) else body

    # Feishu URL verification challenge
    if "challenge" in event:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"challenge": event["challenge"]}),
        }

    # Extract message from Feishu event
    header = event.get("header", {})
    evt = event.get("event", {})
    message = evt.get("message", {})
    sender = evt.get("sender", {})

    msg_type = message.get("message_type", "")
    chat_id = message.get("chat_id", "")
    user_id = sender.get("sender_id", {}).get("open_id", "")
    sender_name = sender.get("sender_id", {}).get("name", user_id)

    if msg_type != "text":
        return {"statusCode": 200, "body": "unsupported type"}

    content = json.loads(message.get("content", "{}"))
    text = content.get("text", "").strip()
    if not text:
        return {"statusCode": 200, "body": "empty"}

    route = get_route("feishu", chat_id)
    if not route:
        logger.warning("No route for feishu:%s", chat_id)
        return {"statusCode": 200, "body": "no route"}

    session_id = get_or_create_session(route["botId"])
    result = invoke_agent_runtime(
        session_id=session_id,
        actor_id=f"feishu_{chat_id}",
        channel="feishu",
        message=text,
        sender_name=sender_name,
        external_chat_id=chat_id,
    )

    # Response is sent by MicroClaw via Feishu WebSocket/API inside the container.
    # The Lambda does NOT need to send the reply — MicroClaw handles it.
    return {"statusCode": 200, "body": "ok"}


def handle_slack(body, headers=None):
    """Handle a Slack webhook event."""
    event = json.loads(body) if isinstance(body, str) else body

    # Slack URL verification
    if event.get("type") == "url_verification":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"challenge": event["challenge"]}),
        }

    slack_event = event.get("event", {})
    if slack_event.get("type") != "message" or slack_event.get("bot_id"):
        return {"statusCode": 200, "body": "ignored"}

    text = slack_event.get("text", "")
    channel_id = slack_event.get("channel", "")
    user_id = slack_event.get("user", "")

    if not text or not channel_id:
        return {"statusCode": 200, "body": "empty"}

    route = get_route("slack", channel_id)
    if not route:
        logger.warning("No route for slack:%s", channel_id)
        return {"statusCode": 200, "body": "no route"}

    bot_token = route.get("botToken") or get_secret(route.get("botTokenSecretId", ""))

    session_id = get_or_create_session(route["botId"])
    result = invoke_agent_runtime(
        session_id=session_id,
        actor_id=f"slack_{channel_id}",
        channel="slack",
        message=text,
        sender_name=user_id,
        external_chat_id=channel_id,
    )

    response_text = result.get("response", "")
    if response_text and bot_token:
        send_slack_message(bot_token, channel_id, response_text)

    return {"statusCode": 200, "body": "ok"}


def send_slack_message(bot_token, channel, text):
    """Send a message via Slack Web API."""
    url = "https://slack.com/api/chat.postMessage"
    data = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib_request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bot_token}",
        },
    )
    try:
        urllib_request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error("Slack send failed: %s", e)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handler(event, context):
    """API Gateway HTTP API Lambda handler."""
    path = event.get("rawPath", event.get("path", ""))
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    body = event.get("body", "")
    headers = event.get("headers", {})

    logger.info("Request: %s %s", method, path)

    if path == "/health":
        return {"statusCode": 200, "body": json.dumps({"status": "healthy"})}

    if method != "POST":
        return {"statusCode": 405, "body": "Method not allowed"}

    # Base64-encoded body from API Gateway
    if event.get("isBase64Encoded") and body:
        import base64
        body = base64.b64decode(body).decode("utf-8")

    try:
        if path == "/webhook/telegram":
            return handle_telegram(body)
        elif path == "/webhook/feishu":
            return handle_feishu(body, headers)
        elif path == "/webhook/slack":
            return handle_slack(body, headers)
        else:
            return {"statusCode": 404, "body": "Not found"}
    except Exception as e:
        logger.exception("Handler error: %s", e)
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
