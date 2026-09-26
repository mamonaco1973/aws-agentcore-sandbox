# ================================================================================
# runtime.py
#
# The API Lambda's side of AgentCore: start a message on the agent, and clean
# up a conversation's sessions when it is deleted.
#
# Replaces the MicroVM version's SQS queue + worker. start_query() calls
# InvokeAgentRuntime, whose handler (agent/main.py) returns as soon as the
# message is running in the background, so this request stays short and the
# browser keeps polling DynamoDB/S3 exactly as before.
#
# Every message in a conversation uses the same runtimeSessionId, so it lands
# on the same Runtime session (and microVM) while that session is alive. The
# Code Interpreter session is separate: its id lives on the CONV# item.
# ================================================================================

import json
import logging
import os

import boto3
from botocore.config import Config

logger = logging.getLogger()

RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
CODE_INTERPRETER_ID = os.environ["CODE_INTERPRETER_ID"]

# Short: the handler only accepts the message. Allow for a cold Runtime
# session starting, but stay inside the API Lambda's own timeout.
client = boto3.client("bedrock-agentcore", config=Config(
    connect_timeout=5, read_timeout=12, retries={"mode": "standard", "max_attempts": 2}))


def session_id(conv_id):
    """The Runtime session for a conversation. Must be 33-256 characters."""
    return f"conversation-{conv_id}"


def start_query(user_id, conv_id, query_id):
    """Hand one message to the agent. Returns the handler's JSON reply.

    Raises:
        RuntimeError: The agent refused the message.
        botocore errors: The Runtime could not be reached.
    """
    response = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=session_id(conv_id),
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({"user_id": user_id, "conv_id": conv_id,
                            "query_id": query_id}).encode())
    reply = json.loads(response["response"].read() or b"{}")
    if reply.get("status") != "accepted":
        raise RuntimeError(f"Agent did not accept the message: {reply}")
    return reply


def release(conv_id, conv_item):
    """Stop a conversation's Code Interpreter and Runtime sessions.

    Best effort, and not awaited: both services finish stopping on their own,
    and a session already gone is fine.
    """
    ci_session = (conv_item or {}).get("ci_session_id")
    if ci_session:
        try:
            client.stop_code_interpreter_session(
                codeInterpreterIdentifier=CODE_INTERPRETER_ID, sessionId=ci_session)
        except Exception:
            logger.info("Code Interpreter session %s not stopped (may be gone)", ci_session)
    try:
        client.stop_runtime_session(agentRuntimeArn=RUNTIME_ARN,
                                    runtimeSessionId=session_id(conv_id))
    except Exception:
        logger.info("Runtime session for %s not stopped (may be gone)", conv_id)
