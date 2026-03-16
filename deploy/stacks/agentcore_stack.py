"""MicroClaw AgentCore Stack — Hosts MicroClaw on AgentCore Runtime.

Deploys:
  - ECR repository for the MicroClaw container image
  - S3 bucket for runtime data persistence (SQLite DB, memory, skills)
  - Security group (HTTPS/WSS egress only)
  - IAM execution role
  - AgentCore Runtime + Endpoint
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_bedrockagentcore as agentcore,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_s3 as s3,
)
from constructs import Construct


class MicroClawAgentCoreStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account

        # --- Context parameters ---
        bot_id = self.node.try_get_context("bot_id") or "microclaw-bot"
        image_version = str(self.node.try_get_context("image_version") or "1")
        vpc_id = self.node.try_get_context("vpc_id") or ""
        subnet_ids = self.node.try_get_context("subnet_ids") or []

        # --- VPC lookup ---
        if vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "Vpc", vpc_id=vpc_id)
        else:
            vpc = ec2.Vpc.from_lookup(self, "Vpc", is_default=True)

        if not subnet_ids:
            subnet_ids = [s.subnet_id for s in vpc.private_subnets] or \
                         [s.subnet_id for s in vpc.public_subnets]

        # --- ECR Repository ---
        self.ecr_repo = ecr.Repository(
            self,
            "MicroClawRepo",
            repository_name="microclaw",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
        )

        # --- S3 Bucket for runtime data persistence ---
        self.data_bucket = s3.Bucket(
            self,
            "DataBucket",
            bucket_name=f"microclaw-data-{account}-{region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-old-data",
                    expiration=Duration.days(365),
                ),
            ],
        )

        # --- Security Group ---
        self.agent_sg = ec2.SecurityGroup(
            self,
            "AgentSG",
            vpc=vpc,
            description="MicroClaw AgentCore container — HTTPS/WSS egress only",
            allow_all_outbound=False,
        )
        self.agent_sg.add_egress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(443),
            description="HTTPS/WSS egress (APIs, Feishu WebSocket, S3)",
        )

        # --- Execution Role ---
        self.execution_role = iam.Role(
            self,
            "ExecutionRole",
            role_name=f"microclaw-agentcore-{bot_id}",
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
                iam.ServicePrincipal("bedrock.amazonaws.com"),
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            ),
        )

        # S3 data bucket access
        self.data_bucket.grant_read_write(self.execution_role)

        # ECR pull
        self.ecr_repo.grant_pull(self.execution_role)

        # CloudWatch Logs
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:/microclaw/*",
                    f"arn:aws:logs:{region}:{account}:log-group:/microclaw/*:*",
                ],
            )
        )

        # --- AgentCore Runtime ---
        self.runtime = agentcore.CfnRuntime(
            self,
            "Runtime",
            agent_runtime_name=f"microclaw_{bot_id}",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=f"{account}.dkr.ecr.{region}.amazonaws.com/microclaw:v{image_version}"
                )
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="VPC",
                network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
                    subnets=subnet_ids,
                    security_groups=[self.agent_sg.security_group_id],
                ),
            ),
            role_arn=self.execution_role.role_arn,
            environment_variables={
                "MICROCLAW_AGENTCORE": "true",
                "MICROCLAW_DATA_DIR": "/app/data",
                "S3_DATA_BUCKET": self.data_bucket.bucket_name,
                "BOT_ID": bot_id,
                "S3_SYNC_INTERVAL_SECS": str(
                    self.node.try_get_context("s3_sync_interval_secs") or "300"
                ),
                "IMAGE_VERSION": image_version,
            },
            description=f"MicroClaw bot: {bot_id}",
            lifecycle_configuration=agentcore.CfnRuntime.LifecycleConfigurationProperty(
                idle_runtime_session_timeout=int(
                    self.node.try_get_context("session_idle_timeout") or "1800"
                ),
                max_lifetime=int(
                    self.node.try_get_context("session_max_lifetime") or "28800"
                ),
            ),
        )

        # --- Runtime Endpoint ---
        self.endpoint = agentcore.CfnRuntimeEndpoint(
            self,
            "RuntimeEndpoint",
            agent_runtime_id=self.runtime.attr_agent_runtime_id,
            name=f"microclaw_{bot_id}_live",
            description=f"MicroClaw {bot_id} production endpoint",
            agent_runtime_version=self.runtime.attr_agent_runtime_version,
        )
        self.endpoint.add_dependency(self.runtime)

        # --- Exports ---
        self.runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{self.runtime.attr_agent_runtime_id}"
        self.runtime_endpoint_id = self.endpoint.attr_id
        self.data_bucket_name = self.data_bucket.bucket_name

        CfnOutput(self, "RuntimeId", value=self.runtime.attr_agent_runtime_id)
        CfnOutput(self, "RuntimeEndpointId", value=self.endpoint.attr_id)
        CfnOutput(self, "DataBucketName", value=self.data_bucket.bucket_name)
        CfnOutput(self, "EcrRepoUri", value=self.ecr_repo.repository_uri)
