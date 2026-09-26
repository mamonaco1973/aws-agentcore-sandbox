#!/bin/bash
# ==============================================================================
# validate.sh
# ==============================================================================
# Smoke-tests the deployment, then prints the app URL.
#
#   1. The agent runtime is READY
#   2. A Code Interpreter session, driven by the agent's own sandbox.py:
#      - start it (which bootstraps the persistent bash shell)
#      - render a fractal tree in Python and read the PNG back out
#      - prove the shell persists: cd + export in one call, read in the next
#      - stop it -- always, including on failure
#
# It does not exercise Cognito, the API or the model: sign in and ask for a
# fractal tree for that. Uses the boto3 apply.sh vendors into dist/layer, so
# the host needs no Python AWS SDK of its own.
# ==============================================================================

export AWS_DEFAULT_REGION="us-east-1"
set -euo pipefail
cd "$(dirname "$0")"

CUSTOM_URL=$(terraform -chdir=02-core output -raw custom_domain_url      2>/dev/null || true)
COGNITO_UI=$(terraform -chdir=02-core output -raw cognito_hosted_ui_base 2>/dev/null || true)
RUNTIME_ARN=$(terraform -chdir=02-core output -raw agent_runtime_arn     2>/dev/null || true)
CI_ID=$(terraform -chdir=01-agentcore output -raw code_interpreter_id    2>/dev/null || true)

if [ -z "${CUSTOM_URL}" ] || [ -z "${RUNTIME_ARN}" ] || [ -z "${CI_ID}" ]; then
  echo "ERROR: Could not read Terraform outputs. Run ./apply.sh first."
  exit 1
fi
if [ ! -d dist/layer/python/boto3 ]; then
  echo "ERROR: dist/layer is missing. Run ./apply.sh (it vendors boto3 there)."
  exit 1
fi

# ------------------------------------------------------------------------------
# Runtime status
# ------------------------------------------------------------------------------

RUNTIME_ID="${RUNTIME_ARN##*/}"
STATUS=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "${RUNTIME_ID}" \
  --query status --output text)
if [[ "${STATUS}" != "READY" ]]; then
  echo "ERROR: Agent runtime ${RUNTIME_ID} is ${STATUS}, expected READY."
  exit 1
fi
echo "NOTE: Agent runtime ${RUNTIME_ID} is READY."

# ------------------------------------------------------------------------------
# Sandbox smoke test, through the agent's own sandbox module
# ------------------------------------------------------------------------------

echo "NOTE: Starting a validation Code Interpreter session..."
PYTHONPATH="dist/layer/python:02-core/agent" \
AWS_REGION="${AWS_DEFAULT_REGION}" TABLE_NAME="unused" CODE_INTERPRETER_ID="${CI_ID}" \
python3 - <<'EOF'
import sys
import sandbox

session_id = sandbox._start()
print(f"NOTE: Session {session_id} started (persistent shell bootstrapped).")
try:
    ok, out, ms = sandbox.run_python(session_id, """
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
def branch(ax, x, y, a, n, d):
    if d == 0: return
    x2, y2 = x + n * np.cos(a), y + n * np.sin(a)
    ax.plot([x, x2], [y, y2], color=plt.cm.inferno(d / 10), lw=d * 0.5)
    for s in (-1, 1): branch(ax, x2, y2, a + s * np.radians(25), n * 0.75, d - 1)
fig, ax = plt.subplots(figsize=(6, 6), facecolor="black")
ax.set_facecolor("black"); ax.axis("off")
branch(ax, 0, 0, np.pi / 2, 1.0, 10)
fig.savefig("validate_tree.png", dpi=100, facecolor="black"); plt.close(fig)
print("rendered")""")
    if not ok or "rendered" not in out:
        sys.exit(f"ERROR: Render cell failed: {out}")
    print(f"NOTE: Rendered a fractal tree in {ms} ms.")

    body, mime, name = sandbox.read_file(session_id, "validate_tree.png")
    if not body.startswith(b"\x89PNG"):
        sys.exit(f"ERROR: readFiles did not return a PNG ({mime}).")
    print(f"NOTE: Read back {name}: {len(body)} bytes, {mime}.")

    sandbox.run_shell(session_id, "cd /tmp && export MARKER=validated")
    ok, out, _ = sandbox.run_shell(session_id, 'echo "$MARKER $(pwd)"')
    if out.strip() != "validated /tmp":
        sys.exit(f"ERROR: The persistent shell lost its state (got: {out.strip()!r}).")
    print("NOTE: Persistent shell kept cd + export across calls.")
finally:
    sandbox.stop(session_id)
    print(f"NOTE: Session {session_id} stopped.")
EOF

echo ""
echo "========================================================"
echo "  AgentCore Sandbox — deployment validated"
echo "========================================================"
echo "  App : ${CUSTOM_URL}"
echo "  Try : Build me a fractal tree and get me the results"
echo "========================================================"
echo ""
echo "  Google IDP — Authorized redirect URI:"
echo "  ${COGNITO_UI}/oauth2/idpresponse"
echo ""
