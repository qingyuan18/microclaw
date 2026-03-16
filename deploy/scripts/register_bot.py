#!/usr/bin/env python3
"""Register a bot route in the MicroClaw DynamoDB routing table.

Usage:
  python register_bot.py \
    --table microclaw-routing \
    --channel telegram \
    --channel-id 123456789 \
    --bot-id my-bot \
    --bot-token "123:ABC..."

  python register_bot.py \
    --table microclaw-routing \
    --channel feishu \
    --channel-id oc_xxxxx \
    --bot-id my-bot \
    --bot-token-secret microclaw/feishu-token

This creates the routing entry:
  PK: ROUTE#telegram:123456789  SK: CONFIG
  { botId, botToken/botTokenSecretId, ... }
"""

import argparse
import time
import boto3


def main():
    parser = argparse.ArgumentParser(description="Register a MicroClaw bot route")
    parser.add_argument("--table", default="microclaw-routing", help="DynamoDB table name")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--channel", required=True, choices=["telegram", "slack", "feishu"])
    parser.add_argument("--channel-id", required=True, help="Channel chat/user ID")
    parser.add_argument("--bot-id", required=True, help="Bot identifier")
    parser.add_argument("--bot-token", default="", help="Bot token (inline)")
    parser.add_argument("--bot-token-secret", default="", help="Secrets Manager ID for bot token")
    parser.add_argument("--display-name", default="")
    args = parser.parse_args()

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


if __name__ == "__main__":
    main()
