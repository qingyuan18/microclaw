#!/usr/bin/env python3
"""MicroClaw AgentCore CDK App."""

import aws_cdk as cdk
from stacks.agentcore_stack import MicroClawAgentCoreStack
from stacks.router_stack import MicroClawRouterStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account") or None,
    region=app.node.try_get_context("region") or "us-west-2",
)

agentcore = MicroClawAgentCoreStack(app, "MicroClawAgentCore", env=env)

MicroClawRouterStack(
    app,
    "MicroClawRouter",
    env=env,
    runtime_arn=agentcore.runtime_arn,
    runtime_endpoint_id=agentcore.runtime_endpoint_id,
    data_bucket_name=agentcore.data_bucket_name,
)

app.synth()
