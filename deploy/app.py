#!/usr/bin/env python3
"""MicroClaw AgentCore CDK App.

Usage:
  # Deploy shared router stack (once):
    cdk deploy MicroClawRouter

  # Deploy a bot runtime (per bot):
    cdk deploy MicroClawBot-mybot -c bot_id=mybot

  # Deploy both router + one bot:
    cdk deploy --all -c bot_id=mybot
"""

import aws_cdk as cdk
from stacks.agentcore_stack import MicroClawAgentCoreStack
from stacks.router_stack import MicroClawRouterStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account") or None,
    region=app.node.try_get_context("region") or "us-west-2",
)

# Shared router stack (API Gateway + Lambda + DynamoDB) — deploy once.
MicroClawRouterStack(app, "MicroClawRouter", env=env)

# Per-bot AgentCore Runtime stack — deploy per bot_id.
bot_id = app.node.try_get_context("bot_id")
if bot_id:
    MicroClawAgentCoreStack(app, f"MicroClawBot-{bot_id}", env=env)

app.synth()
