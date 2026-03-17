#!/usr/bin/env python3
"""Register a bot route in the MicroClaw DynamoDB routing table.

Usage:
  # Register a channel route (maps channel:group → bot_id):
  python register_bot.py route \
    --channel feishu \
    --channel-id oc_xxxxx \
    --bot-id my-bot

  # Register bot config (maps bot_id → runtime ARN + session):
  python register_bot.py bot \
    --bot-id my-bot \
    --runtime-arn arn:aws:bedrock-agentcore:us-west-2:123456:runtime/abc \
    --qualifier endpoint-id-xyz

DynamoDB schema:
  ROUTE#feishu:{app_id} / CONFIG       → { botId, ... }
  ROUTE#slack:{app_id} / CONFIG        → { botId, botToken, ... }
  ROUTE#telegram:{bot_id} / CONFIG     → { botId, botToken, ... }
  BOT#my-bot / SESSION                 → { sessionId, runtimeArn, qualifier, ... }

Routing keys per channel:
  - Feishu:   app_id (one Feishu app = one bot, serves all groups)
  - Slack:    api_app_id (one Slack app = one bot)
  - Telegram: bot_id from webhook URL path /webhook/telegram/{bot_id}
"""

import argparse
import time
import uuid
import boto3


def cmd_route(args):
    """Register a channel route: channel:channel_id → bot_id."""
    dynamodb = boto3.resource("dynamodb", region_name=args.region)
    table = dynamodb.Table(args.table)

    pk = f"ROUTE#{args.channel}:{args.channel_id}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    item = {
        "PK": pk,
        "SK": "CONFIG",
        "botId": args.bot_id,
        "channel": args.channel,
        "channelId": args.channel_id,
        "displayName": args.display_name or args.bot_id,
        "createdAt": now_iso,
    }

    if args.bot_token:
        item["botToken"] = args.bot_token
    if args.bot_token_secret:
        item["botTokenSecretId"] = args.bot_token_secret

    table.put_item(Item=item)
    print(f"Registered route: {pk}")
    print(f"  botId: {args.bot_id}")
    print(f"  channel: {args.channel}")


def cmd_bot(args):
    """Register bot config: bot_id → runtimeArn + qualifier + sessionId."""
    dynamodb = boto3.resource("dynamodb", region_name=args.region)
    table = dynamodb.Table(args.table)

    pk = f"BOT#{args.bot_id}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Check if session already exists
    resp = table.get_item(Key={"PK": pk, "SK": "SESSION"})
    if "Item" in resp:
        # Update runtimeArn/qualifier, keep existing sessionId
        table.update_item(
            Key={"PK": pk, "SK": "SESSION"},
            UpdateExpression="SET runtimeArn = :arn, qualifier = :q, lastActivity = :now",
            ExpressionAttributeValues={
                ":arn": args.runtime_arn,
                ":q": args.qualifier,
                ":now": now_iso,
            },
        )
        print(f"Updated bot config: {pk}")
        print(f"  sessionId: {resp['Item']['sessionId']} (unchanged)")
    else:
        # Create new session
        session_id = f"ses_{args.bot_id}_{uuid.uuid4().hex[:16]}"
        if len(session_id) < 33:
            session_id += "_" + uuid.uuid4().hex[: 33 - len(session_id)]

        table.put_item(
            Item={
                "PK": pk,
                "SK": "SESSION",
                "sessionId": session_id,
                "botId": args.bot_id,
                "runtimeArn": args.runtime_arn,
                "qualifier": args.qualifier,
                "createdAt": now_iso,
                "lastActivity": now_iso,
            }
        )
        print(f"Created bot config: {pk}")
        print(f"  sessionId: {session_id}")

    print(f"  runtimeArn: {args.runtime_arn}")
    print(f"  qualifier: {args.qualifier}")


def main():
    parser = argparse.ArgumentParser(description="MicroClaw DynamoDB registration tool")
    parser.add_argument("--table", default="microclaw-routing", help="DynamoDB table name")
    parser.add_argument("--region", default="us-west-2")

    sub = parser.add_subparsers(dest="command", required=True)

    # route subcommand
    p_route = sub.add_parser("route", help="Register a channel route")
    p_route.add_argument("--channel", required=True, choices=["telegram", "slack", "feishu"])
    p_route.add_argument(
        "--channel-id", required=True,
        help="Routing key: Feishu app_id, Slack api_app_id, or Telegram bot_id",
    )
    p_route.add_argument("--bot-id", required=True, help="Bot identifier (= bot_username)")
    p_route.add_argument("--bot-token", default="", help="Bot token (inline, for Telegram/Slack)")
    p_route.add_argument("--bot-token-secret", default="", help="Secrets Manager ID for bot token")
    p_route.add_argument("--display-name", default="")

    # bot subcommand
    p_bot = sub.add_parser("bot", help="Register bot config (runtime ARN + session)")
    p_bot.add_argument("--bot-id", required=True, help="Bot identifier (= bot_username)")
    p_bot.add_argument("--runtime-arn", required=True, help="AgentCore Runtime ARN")
    p_bot.add_argument("--qualifier", required=True, help="AgentCore Runtime Endpoint ID")

    args = parser.parse_args()
    if args.command == "route":
        cmd_route(args)
    elif args.command == "bot":
        cmd_bot(args)


if __name__ == "__main__":
    main()
