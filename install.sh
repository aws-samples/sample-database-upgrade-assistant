#!/bin/bash
set -euo pipefail

#############################################################################
# RDS Upgrade Assistant — One-Command Installer (Lambda + AgentCore)
#
# Prerequisites:
#   - AWS CLI v2 configured with credentials (admin or PowerUser access)
#   - Node.js 18+ and npm
#
# Usage:
#   ./install.sh                          # Deploy the app
#   ./install.sh --profile=myprofile      # Use a specific AWS CLI profile
#############################################################################

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
AWS_PROFILE_OPT=""
for arg in "$@"; do
  case $arg in
    --profile=*)      AWS_PROFILE="${arg#--profile=}" ;;
  esac
done
if [ -n "${AWS_PROFILE:-}" ]; then
  AWS_PROFILE_OPT="--profile $AWS_PROFILE"
fi

CONFIG_FILE="$(dirname "$0")/conf/install.conf"

VPC_ID=""
EXTERNAL_ID=""
TEST_DB_PASS=""
SUBNETS=""
TAG_KEY=""
TAG_VALUE=""
SAVED_ACCOUNT_ID=""
SAVED_AWS_PROFILE=""
if [ -f "$CONFIG_FILE" ]; then
  source "$CONFIG_FILE"
  echo "  Loaded config from $CONFIG_FILE"
  if [ -z "${AWS_PROFILE:-}" ] && [ -n "$SAVED_AWS_PROFILE" ]; then
    AWS_PROFILE="$SAVED_AWS_PROFILE"
    AWS_PROFILE_OPT="--profile $AWS_PROFILE"
  fi
fi

save_config() {
  mkdir -p "$(dirname "$CONFIG_FILE")"
  cat > "$CONFIG_FILE" <<EOF
SAVED_ACCOUNT_ID="$ACCOUNT_ID"
SAVED_AWS_PROFILE="${AWS_PROFILE:-}"
VPC_ID="$VPC_ID"
EXTERNAL_ID="$EXTERNAL_ID"
TEST_DB_PASS="$TEST_DB_PASS"
SUBNETS="$SUBNETS"
TAG_KEY="$TAG_KEY"
TAG_VALUE="$TAG_VALUE"
EOF
  chmod 600 "$CONFIG_FILE"
}

echo "============================================"
echo "  RDS Upgrade Assistant — Installer"
echo "  Architecture: Lambda + AgentCore (no Docker)"
echo "============================================"
echo ""

echo "Checking prerequisites..."
MISSING=""
command -v aws >/dev/null 2>&1 || MISSING="$MISSING aws-cli"
command -v node >/dev/null 2>&1 || MISSING="$MISSING node"
command -v npm >/dev/null 2>&1 || MISSING="$MISSING npm"
command -v zip >/dev/null 2>&1 || MISSING="$MISSING zip"
command -v agentcore >/dev/null 2>&1 || MISSING="$MISSING agentcore-cli(npm install -g @aws/agentcore@latest)"
if [ -n "$MISSING" ]; then
  echo "ERROR: Missing:$MISSING"; exit 1
fi
echo "  All prerequisites met."
echo ""

ACCOUNT_ID=$(aws sts get-caller-identity $AWS_PROFILE_OPT --query Account --output text --region "$REGION")
echo "AWS Account: $ACCOUNT_ID  Region: $REGION"

if [ -n "$SAVED_ACCOUNT_ID" ] && [ "$SAVED_ACCOUNT_ID" != "$ACCOUNT_ID" ]; then
  echo "  Account mismatch — resetting config."
  rm -f "$CONFIG_FILE"; VPC_ID=""; EXTERNAL_ID=""; SUBNETS=""
fi

if [ -z "$VPC_ID" ]; then
  _DEFAULT_VPC=$(aws ec2 describe-vpcs $AWS_PROFILE_OPT --filters "Name=isDefault,Values=true" \
    --query "Vpcs[0].VpcId" --output text --region "$REGION" 2>/dev/null || echo "")
  [ "$_DEFAULT_VPC" = "None" ] && _DEFAULT_VPC=""
  read -p "VPC ID [${_DEFAULT_VPC:-required}]: " VPC_ID
  VPC_ID="${VPC_ID:-$_DEFAULT_VPC}"
  [ -z "$VPC_ID" ] && echo "ERROR: VPC ID required." && exit 1
else
  echo "  VPC: $VPC_ID (from config)"
fi

if [ -z "$EXTERNAL_ID" ]; then
  read -p "ExternalId [upgradeassistant]: " EXTERNAL_ID
  EXTERNAL_ID="${EXTERNAL_ID:-upgradeassistant}"
fi

if [ -z "$SUBNETS" ]; then
  SUBNETS=$(aws ec2 describe-subnets $AWS_PROFILE_OPT \
    --filters "Name=vpc-id,Values=$VPC_ID" \
    --query "Subnets[*].SubnetId" --output text --region "$REGION" | tr '\t' ',')
  echo "  Subnets: $SUBNETS"
fi

if [ -z "$TAG_KEY" ]; then
  echo "  Enter a custom tag key:value pair for all resources (press Enter to default to auto-delete=no)"
  read -p "Custom tag key [auto-delete]: " TAG_KEY
  TAG_KEY="${TAG_KEY:-auto-delete}"
  read -p "Custom tag value [no]: " TAG_VALUE
  TAG_VALUE="${TAG_VALUE:-no}"
else
  echo "  Tag: $TAG_KEY=$TAG_VALUE (from config)"
fi
save_config

get_output() {
  aws cloudformation describe-stacks $AWS_PROFILE_OPT --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text --region "$REGION"
}
deploy_stack() {
  local STACK_NAME="$1"; shift
  echo "  Deploying $STACK_NAME..."
  aws cloudformation deploy $AWS_PROFILE_OPT "$@" --tags "$TAG_KEY"="$TAG_VALUE" --region "$REGION" --no-fail-on-empty-changeset
}

# Check for pre-created IAM roles (split-team deployment)
ROLES_CONF="$(dirname "$0")/conf/roles.conf"
USE_PRECREATED_ROLES=false
CFN_CAPABILITIES="CAPABILITY_NAMED_IAM"
ROLE_PARAMS=""

if [ -f "$ROLES_CONF" ]; then
  echo ""
  echo ">>> Found conf/roles.conf — using pre-created IAM roles (split-team mode)"
  LAMBDA_ROLE_ARN=$(grep "^LAMBDA_ROLE=" "$ROLES_CONF" | cut -d= -f2)
  API_ROLE_ARN=$(grep "^API_ROLE=" "$ROLES_CONF" | cut -d= -f2)
  SFN_ROLE_ARN=$(grep "^SFN_ROLE=" "$ROLES_CONF" | cut -d= -f2)
  if [ -z "$LAMBDA_ROLE_ARN" ] || [ -z "$API_ROLE_ARN" ] || [ -z "$SFN_ROLE_ARN" ]; then
    echo "ERROR: conf/roles.conf must contain LAMBDA_ROLE, API_ROLE, and SFN_ROLE"
    echo "See conf/roles.conf.example for format."
    exit 1
  fi
  USE_PRECREATED_ROLES=true
  CFN_CAPABILITIES="CAPABILITY_IAM"
  echo "  Lambda Role: $LAMBDA_ROLE_ARN"
  echo "  API Role:    $API_ROLE_ARN"
  echo "  SFN Role:    $SFN_ROLE_ARN"
fi

echo ""
echo ">>> Step 1/2: Foundation (DynamoDB, S3, CloudFront, Lambda, SQS)"
if [ "$USE_PRECREATED_ROLES" = true ]; then
  deploy_stack aurora-upgrade-planner-foundation \
    --template-file infra/01-foundation.yaml \
    --stack-name aurora-upgrade-planner-foundation \
    --parameter-overrides ExternalId="$EXTERNAL_ID" VpcId="$VPC_ID" SubnetIds="$SUBNETS" \
      LambdaRoleArn="$LAMBDA_ROLE_ARN" SfnRoleArn="$SFN_ROLE_ARN" \
    --capabilities $CFN_CAPABILITIES
else
  deploy_stack aurora-upgrade-planner-foundation \
    --template-file infra/01-foundation.yaml \
    --stack-name aurora-upgrade-planner-foundation \
    --parameter-overrides ExternalId="$EXTERNAL_ID" VpcId="$VPC_ID" SubnetIds="$SUBNETS" \
    --capabilities $CFN_CAPABILITIES
fi

REPORT_BUCKET=$(get_output aurora-upgrade-planner-foundation ReportBucketName)
FRONTEND_BUCKET=$(get_output aurora-upgrade-planner-foundation FrontendBucketName)
CF_DIST=$(get_output aurora-upgrade-planner-foundation CloudFrontDistributionId)
CF_URL=$(get_output aurora-upgrade-planner-foundation CloudFrontURL)

echo "  CloudFront: $CF_URL"

echo ""
echo ">>> Step 2/2: API Gateway"
if [ "$USE_PRECREATED_ROLES" = true ]; then
  deploy_stack aurora-upgrade-planner-frontend-api \
    --template-file infra/02-frontend-api-stub.yaml \
    --stack-name aurora-upgrade-planner-frontend-api \
    --parameter-overrides FoundationStackName=aurora-upgrade-planner-foundation \
      ApiLambdaRoleArn="$API_ROLE_ARN" ExternalId="$EXTERNAL_ID" \
    --capabilities $CFN_CAPABILITIES
else
  deploy_stack aurora-upgrade-planner-frontend-api \
    --template-file infra/02-frontend-api-stub.yaml \
    --stack-name aurora-upgrade-planner-frontend-api \
    --parameter-overrides FoundationStackName=aurora-upgrade-planner-foundation \
      ExternalId="$EXTERNAL_ID" \
    --capabilities $CFN_CAPABILITIES
fi

API_URL=$(get_output aurora-upgrade-planner-frontend-api ApiGatewayURL)
WS_URL=$(get_output aurora-upgrade-planner-frontend-api WebSocketURL)
API_KEY_ID=$(get_output aurora-upgrade-planner-frontend-api ApiKeyId)
API_KEY=$(aws apigateway get-api-key $AWS_PROFILE_OPT --api-key "$API_KEY_ID" --include-value --query 'value' --output text --region "$REGION" 2>/dev/null || echo "")
echo "  API: $API_URL"
if [ -n "$API_KEY" ]; then
  echo "  API Key: (retrieved successfully)"
fi

echo ""
echo ">>> Packaging Lambda functions (ZIP)..."
BUILD_DIR=$(mktemp -d)
trap "rm -rf $BUILD_DIR" EXIT
cp -r backend/shared "$BUILD_DIR/"
mkdir -p "$BUILD_DIR/agentcore" && cp backend/agentcore/__init__.py backend/agentcore/catalog_extract.py backend/agentcore/code_enrichment.py backend/agentcore/report.py "$BUILD_DIR/agentcore/"
cp -r backend/lambdas "$BUILD_DIR/"

# API handler (single file)
zip -j /tmp/rds-ua-api.zip backend/api/handler.py backend/api/github_auth.py > /dev/null
aws lambda update-function-code $AWS_PROFILE_OPT \
  --function-name aurora-upgrade-planner-api \
  --zip-file fileb:///tmp/rds-ua-api.zip --region "$REGION" > /dev/null
echo "  API Lambda updated."

# Pipeline Lambdas (shared package + DB drivers)
pushd "$BUILD_DIR" > /dev/null
pip install pymysql==1.1.2 psycopg2-binary==2.9.9 -t . --quiet --platform manylinux2014_x86_64 --python-version 3.12 --only-binary=:all: 2>/dev/null || pip3 install pymysql==1.1.2 psycopg2-binary==2.9.9 -t . --quiet --platform manylinux2014_x86_64 --python-version 3.12 --only-binary=:all: 2>/dev/null || echo "  (pip install for DB drivers skipped — query replay may be unavailable)"
zip -r /tmp/rds-ua-lambdas.zip shared/ agentcore/ lambdas/ pymysql* psycopg2* > /dev/null 2>&1
popd > /dev/null

for FUNC in deprecation code-analysis baseline clone poll-status upgrade validation cleanup; do
  echo "  Updating aurora-upgrade-planner-$FUNC..."
  aws lambda update-function-code $AWS_PROFILE_OPT \
    --function-name "aurora-upgrade-planner-$FUNC" \
    --zip-file fileb:///tmp/rds-ua-lambdas.zip --region "$REGION" > /dev/null 2>&1 || true
done
echo "  All Lambdas updated."


#############################################################################
# Deploy AgentCore agents
#############################################################################
echo ">>> Deploying AgentCore agents..."
pushd backend/agentcore/agents/auroraupgradeplanner > /dev/null
# Set the account ID in aws-targets.json (required by AgentCore CDK)
python3 -c "
import json
with open('agentcore/aws-targets.json') as f:
    targets = json.load(f)
targets[0]['account'] = '$ACCOUNT_ID'
with open('agentcore/aws-targets.json', 'w') as f:
    json.dump(targets, f, indent=2)
"
agentcore deploy --yes 2>&1 | tee /tmp/agentcore-deploy.log
popd > /dev/null

# Extract runtime ARNs from AgentCore CloudFormation stack outputs
AGENTCORE_STACK="AgentCore-auroraupgradeplanner-default"
echo "  Reading AgentCore endpoints from stack: $AGENTCORE_STACK"

CATALOG_RUNTIME_ARN=$(aws cloudformation describe-stacks $AWS_PROFILE_OPT --stack-name "$AGENTCORE_STACK" \
  --query "Stacks[0].Outputs[?contains(OutputKey,'CatalogExtractRuntimeArn')].OutputValue" --output text --region "$REGION" 2>/dev/null || echo "")
ENRICHMENT_RUNTIME_ARN=$(aws cloudformation describe-stacks $AWS_PROFILE_OPT --stack-name "$AGENTCORE_STACK" \
  --query "Stacks[0].Outputs[?contains(OutputKey,'CodeEnrichmentRuntimeArn')].OutputValue" --output text --region "$REGION" 2>/dev/null || echo "")
REPORT_RUNTIME_ARN=$(aws cloudformation describe-stacks $AWS_PROFILE_OPT --stack-name "$AGENTCORE_STACK" \
  --query "Stacks[0].Outputs[?contains(OutputKey,'ReportRuntimeArn')].OutputValue" --output text --region "$REGION" 2>/dev/null || echo "")

# Build endpoint URLs from runtime ARNs
# Format: https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{url_encoded_arn}/invocations
_url_encode_arn() {
  python3 -c "import urllib.parse; print('https://bedrock-agentcore.$REGION.amazonaws.com/runtimes/' + urllib.parse.quote('$1', safe='') + '/invocations')"
}

CATALOG_ENDPOINT=""
ENRICHMENT_ENDPOINT=""
REPORT_ENDPOINT=""
if [ -n "$CATALOG_RUNTIME_ARN" ] && [ "$CATALOG_RUNTIME_ARN" != "None" ]; then
  CATALOG_ENDPOINT=$(_url_encode_arn "$CATALOG_RUNTIME_ARN")
fi
if [ -n "$ENRICHMENT_RUNTIME_ARN" ] && [ "$ENRICHMENT_RUNTIME_ARN" != "None" ]; then
  ENRICHMENT_ENDPOINT=$(_url_encode_arn "$ENRICHMENT_RUNTIME_ARN")
fi
if [ -n "$REPORT_RUNTIME_ARN" ] && [ "$REPORT_RUNTIME_ARN" != "None" ]; then
  REPORT_ENDPOINT=$(_url_encode_arn "$REPORT_RUNTIME_ARN")
fi

# Update Lambda env vars with AgentCore endpoints
if [ -n "$CATALOG_ENDPOINT" ]; then
  echo "  Configuring Lambda env vars with AgentCore endpoints..."
  POLLER_URL=$(get_output aurora-upgrade-planner-foundation PollerQueueUrl)
  aws lambda update-function-configuration $AWS_PROFILE_OPT --region "$REGION" \
    --function-name aurora-upgrade-planner-deprecation \
    --environment "Variables={DYNAMODB_TABLE_PREFIX=aurora-upgrade-planner-,S3_REPORT_BUCKET=$REPORT_BUCKET,POLLER_QUEUE_URL=$POLLER_URL,AGENTCORE_CATALOG_EXTRACT_ENDPOINT=$CATALOG_ENDPOINT}" > /dev/null
  aws lambda update-function-configuration $AWS_PROFILE_OPT --region "$REGION" \
    --function-name aurora-upgrade-planner-code-analysis \
    --environment "Variables={DYNAMODB_TABLE_PREFIX=aurora-upgrade-planner-,S3_REPORT_BUCKET=$REPORT_BUCKET,POLLER_QUEUE_URL=$POLLER_URL,AGENTCORE_CODE_ENRICHMENT_ENDPOINT=$ENRICHMENT_ENDPOINT,AGENTCORE_REPORT_ENDPOINT=$REPORT_ENDPOINT}" > /dev/null
  aws lambda update-function-configuration $AWS_PROFILE_OPT --region "$REGION" \
    --function-name aurora-upgrade-planner-validation \
    --environment "Variables={DYNAMODB_TABLE_PREFIX=aurora-upgrade-planner-,S3_REPORT_BUCKET=$REPORT_BUCKET,POLLER_QUEUE_URL=$POLLER_URL,CLEANUP_LAMBDA_NAME=aurora-upgrade-planner-cleanup,AGENTCORE_REPORT_ENDPOINT=$REPORT_ENDPOINT,DB_SECRET_ARN=${DB_SECRET_ARN:-},QUERY_REPLAY_LIMIT=20}" > /dev/null
  echo "  ✓ Deprecation Lambda: AGENTCORE_CATALOG_EXTRACT_ENDPOINT set"
  echo "  ✓ Code Analysis Lambda: AGENTCORE_CODE_ENRICHMENT_ENDPOINT + AGENTCORE_REPORT_ENDPOINT set"
  echo "  ✓ Validation Lambda: AGENTCORE_REPORT_ENDPOINT set"
else
  echo "  WARNING: Could not read AgentCore runtime ARNs from stack $AGENTCORE_STACK."
  echo "  Ensure 'agentcore deploy' succeeded, then re-run ./install.sh"
fi
echo ""
echo ""
echo ">>> Building frontend..."
pushd frontend > /dev/null
npm install --silent
VITE_API_URL="$API_URL" VITE_WS_URL="$WS_URL" VITE_API_KEY="$API_KEY" npm run build
popd > /dev/null

echo ">>> Deploying frontend to S3..."
aws s3 sync frontend/dist/ "s3://$FRONTEND_BUCKET/" --delete $AWS_PROFILE_OPT --region "$REGION"
aws cloudfront create-invalidation $AWS_PROFILE_OPT --distribution-id "$CF_DIST" --paths "/*" --region "$REGION" > /dev/null

echo ""
echo "============================================"
echo "  Installation Complete!"
echo "============================================"
echo "  Portal:  $CF_URL"
echo "  API:     $API_URL"
echo "  Arch:    Lambda + AgentCore (no Docker/ECS)"
echo ""
