#!/usr/bin/env bash
#
# Deploy a MicroClaw bot to AgentCore.
#
# This script:
#   1. Deploys the AgentCore Runtime stack for the bot (CDK)
#   2. Uploads the bot's microclaw.config.yaml to S3
#   3. Registers the bot config in DynamoDB (runtime ARN + session)
#   4. Registers channel routes in DynamoDB (optional, per --route flags)
#
# Usage:
#   ./deploy_bot.sh \
#     --bot-id my-bot \
#     --config ./my-bot.config.yaml \
#     --region us-west-2 \
#     --route feishu:cli_a9xxxx \
#     --route telegram:my-bot
#
# Route keys per channel (one app/bot = one routing entry):
#   feishu:   feishu:{app_id}        (e.g. feishu:cli_a9xxxx)
#   slack:    slack:{api_app_id}     (e.g. slack:A01XXXXX)
#   telegram: telegram:{bot_id}      (e.g. telegram:my-bot)
#
# Prerequisites:
#   - AWS CDK CLI installed (npm install -g aws-cdk)
#   - AWS credentials configured
#   - Python 3.x with boto3
#   - Run from the deploy/ directory (or set --cdk-dir)

set -euo pipefail

BOT_ID=""
CONFIG_FILE=""
REGION="us-west-2"
CDK_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TABLE="microclaw-routing"
ROUTES=()
SKIP_CDK=false

usage() {
    echo "Usage: $0 --bot-id BOT_ID --config CONFIG_FILE [OPTIONS]"
    echo ""
    echo "Required:"
    echo "  --bot-id ID            Bot identifier (= bot_username in config)"
    echo "  --config FILE          Path to microclaw.config.yaml for this bot"
    echo ""
    echo "Optional:"
    echo "  --region REGION        AWS region (default: us-west-2)"
    echo "  --table TABLE          DynamoDB table name (default: microclaw-routing)"
    echo "  --route CHANNEL:ID     Register a channel route (repeatable)"
    echo "  --skip-cdk             Skip CDK deploy (only upload config + register)"
    echo "  --cdk-dir DIR          CDK project directory (default: deploy/)"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --bot-id)     BOT_ID="$2"; shift 2 ;;
        --config)     CONFIG_FILE="$2"; shift 2 ;;
        --region)     REGION="$2"; shift 2 ;;
        --table)      TABLE="$2"; shift 2 ;;
        --route)      ROUTES+=("$2"); shift 2 ;;
        --skip-cdk)   SKIP_CDK=true; shift ;;
        --cdk-dir)    CDK_DIR="$2"; shift 2 ;;
        *)            echo "Unknown option: $1"; usage ;;
    esac
done

[[ -z "$BOT_ID" ]] && { echo "Error: --bot-id is required"; usage; }
[[ -z "$CONFIG_FILE" ]] && { echo "Error: --config is required"; usage; }
[[ ! -f "$CONFIG_FILE" ]] && { echo "Error: config file not found: $CONFIG_FILE"; exit 1; }

echo "=== Deploying MicroClaw bot: $BOT_ID ==="
echo "  Region: $REGION"
echo "  Config: $CONFIG_FILE"
echo ""

# Step 1: CDK deploy
if [[ "$SKIP_CDK" == "false" ]]; then
    echo "--- Step 1: CDK deploy AgentCore Runtime ---"
    cd "$CDK_DIR"
    cdk deploy "MicroClawBot-${BOT_ID}" \
        -c bot_id="$BOT_ID" \
        -c region="$REGION" \
        --require-approval never \
        --outputs-file "/tmp/cdk-outputs-${BOT_ID}.json"
    echo ""
else
    echo "--- Step 1: Skipped (--skip-cdk) ---"
    echo ""
fi

# Extract outputs
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="microclaw-data-${ACCOUNT}-${REGION}"

# Try to get runtime ARN from CDK outputs
CDK_OUTPUTS="/tmp/cdk-outputs-${BOT_ID}.json"
if [[ -f "$CDK_OUTPUTS" ]]; then
    STACK_KEY="MicroClawBot-${BOT_ID}"
    RUNTIME_ID=$(python3 -c "
import json
with open('$CDK_OUTPUTS') as f:
    o = json.load(f)
print(o.get('$STACK_KEY', {}).get('RuntimeId', ''))
" 2>/dev/null || echo "")
    ENDPOINT_ID=$(python3 -c "
import json
with open('$CDK_OUTPUTS') as f:
    o = json.load(f)
print(o.get('$STACK_KEY', {}).get('RuntimeEndpointId', ''))
" 2>/dev/null || echo "")
else
    RUNTIME_ID=""
    ENDPOINT_ID=""
fi

RUNTIME_ARN="arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/${RUNTIME_ID}"

if [[ -z "$RUNTIME_ID" || -z "$ENDPOINT_ID" ]]; then
    echo "Warning: Could not extract Runtime/Endpoint IDs from CDK outputs."
    echo "  You may need to register the bot manually with register_bot.py"
fi

# Step 2: Upload config to S3
echo "--- Step 2: Upload config to S3 ---"
S3_KEY="${BOT_ID}/microclaw.config.yaml"
aws s3 cp "$CONFIG_FILE" "s3://${S3_BUCKET}/${S3_KEY}" --region "$REGION"
echo "  Uploaded: s3://${S3_BUCKET}/${S3_KEY}"
echo ""

# Step 3: Register bot config in DynamoDB
echo "--- Step 3: Register bot config ---"
if [[ -n "$RUNTIME_ID" && -n "$ENDPOINT_ID" ]]; then
    python3 "$(dirname "$0")/register_bot.py" bot \
        --table "$TABLE" \
        --region "$REGION" \
        --bot-id "$BOT_ID" \
        --runtime-arn "$RUNTIME_ARN" \
        --qualifier "$ENDPOINT_ID"
else
    echo "  Skipped (no runtime info available)"
fi
echo ""

# Step 4: Register channel routes
if [[ ${#ROUTES[@]} -gt 0 ]]; then
    echo "--- Step 4: Register channel routes ---"
    for route in "${ROUTES[@]}"; do
        CHANNEL="${route%%:*}"
        CHANNEL_ID="${route#*:}"
        python3 "$(dirname "$0")/register_bot.py" route \
            --table "$TABLE" \
            --region "$REGION" \
            --channel "$CHANNEL" \
            --channel-id "$CHANNEL_ID" \
            --bot-id "$BOT_ID"
    done
    echo ""
fi

echo "=== Deploy complete: $BOT_ID ==="
echo ""
echo "Next steps:"
echo "  1. Build & push Docker image:  docker build -f Dockerfile.agentcore -t microclaw ."
echo "  2. Push to ECR:  docker tag microclaw:latest ${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/microclaw:v1"
echo "  3. Add routes:  python register_bot.py route --channel feishu --channel-id {app_id} --bot-id ${BOT_ID}"
echo "  4. Set Telegram webhook:  https://{api-url}/webhook/telegram/${BOT_ID}"
