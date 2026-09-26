#!/bin/bash
# ================================================================================
# apply.sh
# Full deployment of the AgentCore sandbox agent.
#
# Workflow:
#   1. Validate tooling, AWS credentials, AgentCore CLI support, model access
#   2. Package the agent (ARM64 wheels + agent/*.py) and a boto3 layer
#   3. 01-agentcore: Code Interpreter (PUBLIC) + Memory
#   4. 02-core:      agent on AgentCore Runtime, API, Cognito, DynamoDB, S3,
#                    CloudFront
#   5. 03-webapp:    generate config.js and upload the SPA
#   6. validate.sh:  smoke-test a sandbox session and print the app URL
# ================================================================================

export AWS_DEFAULT_REGION="us-east-1"

# Exports BEDROCK_MODEL_ID. Sourced before strict mode so simple assignments
# don't trip the unbound-variable check.
source "$(dirname "$0")/bedrock-config.sh"

set -euo pipefail
cd "$(dirname "$0")"

# The agent's runtime in 02-core/agent.tf and the API Lambda's runtime in
# lambdas.tf; wheels are selected for these.
AGENT_PYTHON="3.13"
LAMBDA_PYTHON="3.13"

# ================================================================================
# Environment pre-check
# ================================================================================

echo "NOTE: Running environment validation..."
./check_env.sh || { echo "ERROR: Environment validation failed. Exiting."; exit 1; }

# ================================================================================
# Packaging
# ================================================================================
# AgentCore Runtime runs ARM64. Rather than build a container (which would
# need Docker buildx or CodeBuild), the agent ships as a zip: pip fetches
# ARM64 wheels for the Runtime's Python regardless of this host's platform.
# --only-binary makes a package without an ARM64 wheel fail here, loudly,
# instead of at runtime.
# ================================================================================

rm -rf dist && mkdir -p dist

echo "NOTE: Packaging the agent for AgentCore Runtime (ARM64)..."
python3 -m pip install --quiet --disable-pip-version-check --no-compile \
  --only-binary=:all: --implementation cp --python-version "${AGENT_PYTHON}" \
  --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 \
  --target dist/agent -r 02-core/agent/requirements.txt
cp 02-core/agent/*.py dist/agent/
(cd dist/agent && zip -q -X -r ../agent.zip . -x '*/__pycache__/*')
echo "NOTE: dist/agent.zip is $(du -h dist/agent.zip | cut -f1)"

# The API Lambda calls InvokeAgentRuntime; the runtime's bundled boto3 may
# predate that service, so a current one is vendored as a layer.
echo "NOTE: Vendoring boto3 into the API Lambda layer..."
python3 -m pip install --quiet --disable-pip-version-check --no-compile \
  --only-binary=:all: --python-version "${LAMBDA_PYTHON}" \
  --ignore-requires-python --no-warn-conflicts \
  --target dist/layer/python "boto3>=1.43.0"
if [[ ! -d dist/layer/python/botocore/data/bedrock-agentcore ]]; then
  echo "ERROR: Vendored boto3 has no bedrock-agentcore service model."
  exit 1
fi
(cd dist/layer && zip -q -X -r ../boto3-layer.zip python -x '*/__pycache__/*')

# ================================================================================
# 01-agentcore — Code Interpreter + Memory
# ================================================================================

echo "NOTE: Creating the Code Interpreter and Memory (Memory takes a few minutes)..."
terraform -chdir=01-agentcore init -input=false
terraform -chdir=01-agentcore apply -auto-approve -input=false \
  -var="region=${AWS_DEFAULT_REGION}"

CI_ID=$(terraform -chdir=01-agentcore output -raw code_interpreter_id)
CI_ARN=$(terraform -chdir=01-agentcore output -raw code_interpreter_arn)
MEMORY_ID=$(terraform -chdir=01-agentcore output -raw memory_id)
MEMORY_ARN=$(terraform -chdir=01-agentcore output -raw memory_arn)

# ================================================================================
# 02-core — agent runtime and backend
# ================================================================================

echo "NOTE: Deploying the agent runtime and backend..."
terraform -chdir=02-core init -input=false
terraform -chdir=02-core apply -auto-approve -input=false \
  -var="bedrock_model_id=${BEDROCK_MODEL_ID}" \
  -var="code_interpreter_id=${CI_ID}" \
  -var="code_interpreter_arn=${CI_ARN}" \
  -var="memory_id=${MEMORY_ID}" \
  -var="memory_arn=${MEMORY_ARN}" \
  -var="google_client_id=${AWS_AGENTOPS_GOOGLE_CLIENT_ID:-}" \
  -var="google_client_secret=${AWS_AGENTOPS_GOOGLE_CLIENT_SECRET:-}" \
  -var="custom_domain=${AWS_AGENTOPS_CUSTOM_DOMAIN:-}"

export API_BASE_URL=$(terraform -chdir=02-core output -raw api_endpoint)
export COGNITO_DOMAIN=$(terraform -chdir=02-core output -raw cognito_hosted_ui_base)
export COGNITO_CLIENT_ID=$(terraform -chdir=02-core output -raw cognito_user_pool_client_id)
BUCKET_NAME=$(terraform -chdir=02-core output -raw frontend_bucket_name)
CF_DISTRIBUTION_ID=$(terraform -chdir=02-core output -raw cloudfront_distribution_id)

# ================================================================================
# 03-webapp — frontend
# ================================================================================

echo "NOTE: Deploying web application..."
envsubst < 03-webapp/js/config.js.tmpl > 03-webapp/js/config.js
aws s3 cp 03-webapp "s3://${BUCKET_NAME}" --recursive --exclude "*.tmpl"
aws cloudfront create-invalidation \
  --distribution-id "${CF_DISTRIBUTION_ID}" \
  --paths "/*" > /dev/null

# ================================================================================
# Post-deploy validation
# ================================================================================

echo "NOTE: Running post-deployment validation..."
./validate.sh
