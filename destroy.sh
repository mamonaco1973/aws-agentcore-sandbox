#!/bin/bash
# ================================================================================
# File: destroy.sh
# ================================================================================
#
# Purpose:
#   Tears down everything apply.sh deployed, in reverse order.
#
#   Code Interpreter sessions are not Terraform resources -- the agent starts
#   them on demand -- so any still running are stopped first; otherwise they
#   would keep their 8-hour TTL (and may block deleting the interpreter).
#   Runtime sessions need no cleanup: deleting the runtime ends them.
#
# ================================================================================

export AWS_DEFAULT_REGION="us-east-1"

# Terraform needs the model ID during destroy to resolve variable references.
source "$(dirname "$0")/bedrock-config.sh"

set -euo pipefail
cd "$(dirname "$0")"

CI_ID="" CI_ARN="unused" MEMORY_ID="unused" MEMORY_ARN="unused"
if [[ -f 01-agentcore/terraform.tfstate ]]; then
  CI_ID=$(terraform -chdir=01-agentcore output -raw code_interpreter_id 2>/dev/null || true)
  CI_ARN=$(terraform -chdir=01-agentcore output -raw code_interpreter_arn 2>/dev/null || echo unused)
  MEMORY_ID=$(terraform -chdir=01-agentcore output -raw memory_id 2>/dev/null || echo unused)
  MEMORY_ARN=$(terraform -chdir=01-agentcore output -raw memory_arn 2>/dev/null || echo unused)
fi

# ================================================================================
# STOP CODE INTERPRETER SESSIONS
# ================================================================================

if [[ -n "${CI_ID}" ]]; then
  echo "NOTE: Stopping Code Interpreter sessions on ${CI_ID}..."
  SESSIONS=$(aws bedrock-agentcore list-code-interpreter-sessions \
    --code-interpreter-identifier "${CI_ID}" --status READY \
    --query "items[].sessionId" --output text 2>/dev/null || true)
  for session in ${SESSIONS}; do
    [[ "${session}" == "None" ]] && continue
    echo "NOTE: Stopping ${session}..."
    aws bedrock-agentcore stop-code-interpreter-session \
      --code-interpreter-identifier "${CI_ID}" --session-id "${session}" >/dev/null || true
  done
fi

# ================================================================================
# RESTORE BUILD ARTIFACTS THE CONFIGURATION REFERENCES
# ================================================================================
# 02-core hashes dist/agent.zip and dist/boto3-layer.zip. Only existence
# matters for a destroy, so empty placeholders are enough.
# ================================================================================

mkdir -p dist
if [[ ! -f dist/agent.zip ]]; then
  mkdir -p dist/agent && touch dist/agent/placeholder
  (cd dist/agent && zip -q -X -r ../agent.zip .)
fi
if [[ ! -f dist/boto3-layer.zip ]]; then
  mkdir -p dist/layer/python
  (cd dist/layer && zip -q -X -r ../boto3-layer.zip python)
fi

# ================================================================================
# DESTROY TERRAFORM RESOURCES IN REVERSE ORDER
# ================================================================================
# 03-webapp holds no Terraform state: its files live in the frontend bucket,
# which 02-core destroys with force_destroy.
# ================================================================================

if [[ -f 02-core/terraform.tfstate ]]; then
  echo "NOTE: Destroying 02-core..."
  terraform -chdir=02-core init -input=false
  terraform -chdir=02-core destroy -auto-approve -input=false \
    -var="bedrock_model_id=${BEDROCK_MODEL_ID}" \
    -var="code_interpreter_id=${CI_ID:-unused}" \
    -var="code_interpreter_arn=${CI_ARN}" \
    -var="memory_id=${MEMORY_ID}" \
    -var="memory_arn=${MEMORY_ARN}" \
    -var="google_client_id=${AWS_AGENTOPS_GOOGLE_CLIENT_ID:-}" \
    -var="google_client_secret=${AWS_AGENTOPS_GOOGLE_CLIENT_SECRET:-}" \
    -var="custom_domain=${AWS_AGENTOPS_CUSTOM_DOMAIN:-}"
fi

if [[ -f 01-agentcore/terraform.tfstate ]]; then
  echo "NOTE: Destroying 01-agentcore..."
  terraform -chdir=01-agentcore init -input=false
  terraform -chdir=01-agentcore destroy -auto-approve -input=false \
    -var="region=${AWS_DEFAULT_REGION}"
fi

echo "NOTE: Infrastructure teardown complete."
