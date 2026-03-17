"""MicroClaw Router Stack — API Gateway + Lambda for webhook ingestion.

Deploys:
  - DynamoDB routing table (ROUTE#{channel}:{id} -> bot config + session_id)
  - Router Lambda (receives webhooks, invokes AgentCore, replies via channel API)
  - API Gateway HTTP API with explicit webhook routes
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
)
from constructs import Construct


class MicroClawRouterStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        default_runtime_arn: str = "",
        default_runtime_endpoint_id: str = "",
        data_bucket_name: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        lambda_timeout = int(self.node.try_get_context("router_lambda_timeout_seconds") or "300")

        # --- DynamoDB Routing Table ---
        # PK: ROUTE#{channel}:{channel_user_id}  SK: CONFIG
        # Stores: bot_id, session_id, display_name, channel config
        self.routing_table = dynamodb.Table(
            self,
            "RoutingTable",
            table_name="microclaw-routing",
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="SK", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
        )

        # --- Log Group ---
        log_group = logs.LogGroup(
            self,
            "RouterLogGroup",
            log_group_name="/microclaw/lambda/router",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Router Lambda ---
        self.router_fn = _lambda.Function(
            self,
            "RouterFn",
            function_name="microclaw-router",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/router"),
            timeout=Duration.seconds(lambda_timeout),
            memory_size=256,
            environment={
                "AGENTCORE_RUNTIME_ARN": default_runtime_arn,
                "AGENTCORE_QUALIFIER": default_runtime_endpoint_id,
                "ROUTING_TABLE_NAME": self.routing_table.table_name,
                "AWS_REGION_NAME": region,
                "LAMBDA_TIMEOUT_SECONDS": str(lambda_timeout),
            },
            log_group=log_group,
        )

        # --- API Gateway HTTP API ---
        integration = apigwv2_integrations.HttpLambdaIntegration(
            "LambdaIntegration",
            handler=self.router_fn,
        )

        self.http_api = apigwv2.HttpApi(
            self,
            "RouterApi",
            api_name="microclaw-router",
            description="MicroClaw webhook ingestion API",
        )

        # Explicit routes for each channel
        for channel in ["slack", "feishu"]:
            self.http_api.add_routes(
                path=f"/webhook/{channel}",
                methods=[apigwv2.HttpMethod.POST],
                integration=integration,
            )

        # Telegram: /webhook/telegram/{bot_id} — each bot sets a unique webhook URL
        self.http_api.add_routes(
            path="/webhook/telegram/{bot_id}",
            methods=[apigwv2.HttpMethod.POST],
            integration=integration,
        )

        self.http_api.add_routes(
            path="/health",
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
        )

        # --- IAM Permissions ---

        # AgentCore invocation — wildcard to support multiple bot runtimes
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/*",
                ],
            )
        )

        # DynamoDB
        self.routing_table.grant_read_write_data(self.router_fn)

        # Secrets Manager (channel bot tokens stored as secrets)
        self.router_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:microclaw/*",
                ],
            )
        )

        # --- Outputs ---
        CfnOutput(
            self,
            "ApiUrl",
            value=self.http_api.url or "",
            description="Webhook URL base (append /webhook/telegram etc.)",
        )
        CfnOutput(
            self,
            "RoutingTableName",
            value=self.routing_table.table_name,
        )
