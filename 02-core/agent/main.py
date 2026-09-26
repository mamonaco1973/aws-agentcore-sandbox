# ================================================================================
# main.py — AgentCore Runtime entrypoint
#
# The API Lambda calls InvokeAgentRuntime with {user_id, conv_id, query_id}
# once per message, using runtimeSessionId "conversation-<conv_id>", so every
# message in a conversation lands on the same Runtime session.
#
# The handler returns at once and the message runs on a background thread.
# That replaces the MicroVM version's SQS queue + worker Lambda: the browser
# already polls DynamoDB/S3 for results, so nothing needs to wait on this call.
# add_async_task marks the session busy (/ping answers HealthyBusy), so the
# Runtime does not reap it as idle while an agent is still working; a single
# message may now run for hours instead of the Lambda's 15 minutes.
# ================================================================================

import logging
import threading

from bedrock_agentcore.runtime import BedrockAgentCoreApp

import query

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload):
    """Accept one message and process it in the background."""
    user_id = str(payload.get("user_id", "")).strip()
    conv_id = str(payload.get("conv_id", "")).strip()
    query_id = str(payload.get("query_id", "")).strip()
    if not user_id or not conv_id or not query_id:
        return {"status": "rejected", "error": "user_id, conv_id and query_id are required"}

    task_id = app.add_async_task("query", {"conv_id": conv_id, "query_id": query_id})

    def work():
        try:
            query.process_query(user_id, conv_id, query_id)
        except Exception:
            logger.exception("Unhandled error in query %s", query_id)
        finally:
            app.complete_async_task(task_id)

    threading.Thread(target=work, name=f"query-{query_id}", daemon=True).start()
    return {"status": "accepted", "query_id": query_id}


if __name__ == "__main__":
    app.run()
