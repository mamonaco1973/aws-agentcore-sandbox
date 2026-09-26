# ================================================================================
# query.py
#
# One user message, start to finish, running inside AgentCore Runtime.
#
#   1. Read question.txt from S3; mark the query processing
#   2. Build a Strands Agent: Claude on Bedrock (prompt caching on), three
#      tools backed by AgentCore Code Interpreter, and AgentCore Memory as the
#      session manager -- which restores the conversation so far
#   3. Tidy the restored history (context.clean_restored), prepend what the
#      sandbox holds (context.state_block), and run the agent
#   4. Write answer.txt + trace.json to S3, stage shown files as artifacts,
#      take a fresh sandbox inventory, mark the query complete and add the
#      tokens to the user's usage
#
# This is the port of aws-microvm-agent-sandbox's worker.py. What went away:
# the hand-written Converse loop (Strands runs it), history save/replay
# (AgentCore Memory), cache-point placement (Strands CacheConfig), get_result
# and job polling (a Runtime async task has hours, so tools just wait), and
# the SQS/Lambda 15-minute ceiling. The trace, files and S3/DynamoDB records
# are identical, so the web app is unchanged.
# ================================================================================

import json
import logging
import os
import re
import struct
import time
from datetime import datetime, timezone

import boto3
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import BeforeToolCallEvent, HookProvider, MessageAddedEvent
from strands.models import BedrockModel
from strands.models.bedrock import CacheConfig

import context
import sandbox

logger = logging.getLogger(__name__)

REGION = os.environ["AWS_REGION"]
MODEL_ID = os.environ["BEDROCK_MODEL_ID"]
MEMORY_ID = os.environ["MEMORY_ID"]
BACKEND_BUCKET = os.environ["BACKEND_BUCKET_NAME"]

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(os.environ["TABLE_NAME"])
s3 = boto3.client("s3", region_name=REGION)

# Tool calls per message. A fractal takes three or four; the cap stops a
# model that keeps "fixing" the same error from burning the token budget.
MAX_TOOL_CALLS = 30

# Messages the model sees from the restored conversation. Tool calls count, so
# this is several earlier questions, not forty.
HISTORY_WINDOW_MESSAGES = 40

# Converse image limits: 3.75 MB and 8000 px a side.
IMAGE_BYTES_LIMIT = 3_750_000
IMAGE_PX_LIMIT = 8000
IMAGE_FORMATS = {"image/png": "png", "image/jpeg": "jpeg",
                 "image/gif": "gif", "image/webp": "webp"}

SYSTEM_PROMPT = """You are a coding agent with a private sandbox: an Amazon Bedrock \
AgentCore Code Interpreter session that belongs to this conversation.

How the sandbox works:
- It has TWO persistent sessions that share one filesystem (the default \
working directory is the sandbox user's home):
  - run_code: a Python session. Variables, functions and imports survive \
between calls and between messages in this conversation.
  - run_shell: a bash session. cd, exports, variables and functions survive \
between calls the same way.
- Use Python for computation, data and plots. Use bash for files, downloads \
and builds. A file written in one is visible in the other.
- numpy, pandas, matplotlib (use the Agg backend), seaborn, plotly and pillow \
are installed. Install other Python packages from run_shell with \
`pip install -q pkg`, then import them from run_code. The sandbox has \
internet access.
- You are NOT root and there is no sudo: dnf, yum and system packages cannot \
be installed. git is not installed -- to get a repository, download its \
tarball with curl (e.g. https://github.com/OWNER/REPO/archive/refs/heads/main.tar.gz) \
and extract it with tar, or pip install a pure-Python tool.
- The bash session IS the session. Never put `set -e` at the top of a \
run_shell command -- one failing command would end the shell and reset its \
state. For fail-fast, use a subshell: `( set -e; ...; )`. Commands cannot \
prompt (stdin is closed), so pass -y to anything that would ask.
- The sandbox starts automatically the first time you run code, and lives \
at most 8 hours. Never ask the user to start it.
- A failing command returns its error and the sessions survive. Read the \
error, fix it, run it again.

Showing results:
- Save figures to files with plt.savefig(...) and plt.close(); never \
plt.show(). Then call show_file with the path. The image is attached to your \
answer for the user, and returned to you so you can check it.
- Review every image you show before answering. Does it look like what was \
asked for? Is anything floating, clipped, cropped, overlapping, or in large \
empty space? Is all text rendered (no missing-glyph boxes)? If anything is \
off, fix the code, save to the SAME path, and call show_file again -- that \
replaces the earlier attachment, so the user only sees the final version.
- Fix real defects, not taste: one correction pass is usually enough. \
Anything cut off at the frame edge or hidden behind the title or labels IS a \
defect -- fix it. Do not re-render repeatedly for small spacing tweaks; each \
render costs the user time.
- Treat warnings in cell output as bugs to fix. The fonts have no emoji: \
keep emoji out of plot titles and labels.
- Prefer a clean, faithful rendering of what was asked over decoration.
- Never paste file contents, base64, links, or markdown image syntax into \
your answer -- the user already sees every file you showed.

Your final answer: say briefly what you built and the key parameters, then \
include the final code in a ```python block unless it is very long."""


# ================================================================================
# S3 / DynamoDB helpers — the same records worker.py wrote
# ================================================================================

def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_s3_text(key):
    return s3.get_object(Bucket=BACKEND_BUCKET, Key=key)["Body"].read().decode("utf-8")


def _write_s3_text(key, text):
    s3.put_object(Bucket=BACKEND_BUCKET, Key=key, Body=text.encode("utf-8"),
                  ContentType="text/plain; charset=utf-8")


def _write_s3_json(key, obj):
    s3.put_object(Bucket=BACKEND_BUCKET, Key=key,
                  Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                  ContentType="application/json; charset=utf-8")


def _s3_prefix(user_id, conv_id, query_id):
    return f"users/USER#{user_id}/conversations/CONV#{conv_id}/QUERY#{query_id}"


def _query_key(user_id, conv_id, query_id):
    return {"pk": f"USER#{user_id}", "sk": f"QUERY#{conv_id}#{query_id}"}


def _update_status(user_id, conv_id, query_id, status, trace_key):
    table.update_item(
        Key=_query_key(user_id, conv_id, query_id),
        UpdateExpression="SET #s = :s, trace_s3_key = :tr, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":tr": trace_key, ":u": utc_now()})


def _finalize(user_id, conv_id, query_id, answer_key, tokens_used, artifacts):
    table.update_item(
        Key=_query_key(user_id, conv_id, query_id),
        UpdateExpression=("SET #s = :s, answer_s3_key = :a, tokens_used = :t, "
                          "artifacts = :f, updated_at = :u"),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "complete", ":a": answer_key, ":t": tokens_used,
                                   ":f": artifacts, ":u": utc_now()})


def _fail(user_id, conv_id, query_id, reason):
    table.update_item(
        Key=_query_key(user_id, conv_id, query_id),
        UpdateExpression="SET #s = :s, status_message = :m, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "failed", ":m": str(reason)[:500], ":u": utc_now()})


def budget_tokens(usage):
    """Tokens charged to the user's budget, weighted like the bill.

    A cache read costs about a tenth of a normal input token and a cache write
    about a quarter more -- the same weighting as the MicroVM version.
    """
    return (int(usage.get("inputTokens") or 0) + int(usage.get("outputTokens") or 0)
            + round(int(usage.get("cacheWriteInputTokens") or 0) * 1.25)
            + round(int(usage.get("cacheReadInputTokens") or 0) * 0.1))


def accumulate_tokens(user_id, total):
    if total <= 0:
        return
    try:
        table.update_item(Key={"pk": f"USER#{user_id}", "sk": "USER#USAGE"},
                          UpdateExpression="ADD tokens_used :n",
                          ExpressionAttributeValues={":n": int(total)})
    except Exception:
        logger.exception("Failed to update token usage for user_id=%s", user_id)


# ================================================================================
# File type detection (unchanged from worker.py)
# ================================================================================

MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)


def _sniff(body, declared):
    """Decide a file's type from its content, falling back to its name."""
    for prefix, mime in MAGIC:
        if body.startswith(prefix):
            return mime
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if b"\x00" not in body[:8192]:
        try:
            body[:8192].decode("utf-8")
            if declared.startswith("text/") or declared in (
                    "application/json", "application/xml", "image/svg+xml"):
                return declared
            return "text/plain"
        except UnicodeDecodeError:
            pass
    return declared


def _png_too_large(body):
    if not body.startswith(b"\x89PNG") or len(body) < 24:
        return False
    width, height = struct.unpack(">II", body[16:24])
    return width > IMAGE_PX_LIMIT or height > IMAGE_PX_LIMIT


# ================================================================================
# One message
# ================================================================================

class Run:
    """State for one message: the sandbox session, trace and artifacts."""

    def __init__(self, user_id, conv_id, query_id):
        self.user_id = user_id
        self.conv_id = conv_id
        self.prefix = _s3_prefix(user_id, conv_id, query_id)
        self.trace_key = f"{self.prefix}/trace.json"
        self.session_id = None
        self.steps = []
        self.artifacts = []

    def step(self, **step):
        """Record a trace step and persist the trace for the polling browser."""
        self.steps.append(step)
        try:
            _write_s3_json(self.trace_key, self.steps)
        except Exception:
            logger.exception("Progress write failed (non-fatal)")

    def sandbox(self):
        """The conversation's Code Interpreter session, started on first use.

        Returns:
            (session_id, note) where note tells the model when a fresh session
            replaced an expired one.
        """
        if self.session_id is not None:
            return self.session_id, ""
        session, started = sandbox.ensure(self.user_id, self.conv_id)
        note = ""
        if started:
            self.step(type="sandbox",
                      text=f"Started Code Interpreter session {started['launched']} "
                           f"({started['ms'] / 1000:.1f}s)")
            if started["replaced"]:
                note = ("[The previous sandbox had expired, so this is a fresh "
                        "one: variables and files from earlier messages are gone.]\n")
        else:
            self.step(type="sandbox", text=f"Reusing Code Interpreter session {session['id']}")
        self.session_id = session["id"]
        return self.session_id, note

    # --------------------------------------------------------------------------
    # Tools. Closures over this Run, so each message has its own trace and
    # artifacts. A dict with "status" and "content" is passed through by
    # Strands as the tool result, which is how show_file returns an image.
    # --------------------------------------------------------------------------

    def tools(self):
        run = self

        def outcome(ok, output, ms, note):
            run.step(type="tool_result", text=output[:2000], ok=ok, ms=ms)
            return {"status": "success" if ok else "error",
                    "content": [{"text": note + output}]}

        @tool
        def run_code(code: str) -> dict:
            """Run Python in the persistent sandbox session and return its output.

            Variables, functions and imports persist across calls and across
            messages in this conversation. Shares its files with run_shell.

            Args:
                code: Python source to execute.
            """
            if not code.strip():
                return {"status": "error", "content": [{"text": "No code given."}]}
            session_id, note = run.sandbox()
            return outcome(*sandbox.run_python(session_id, code), note)

        @tool
        def run_shell(command: str) -> dict:
            """Run bash in the persistent sandbox shell and return its output.

            cd, exports and variables persist across calls. Shares files with
            run_code. Use for pip installs, downloads and file management.
            Never start with `set -e`. You are not root: no dnf, yum or sudo.

            Args:
                command: Bash to execute.
            """
            if not command.strip():
                return {"status": "error", "content": [{"text": "No command given."}]}
            session_id, note = run.sandbox()
            return outcome(*sandbox.run_shell(session_id, command), note)

        @tool
        def show_file(path: str) -> dict:
            """Attach a file from the sandbox to your answer so the user sees it.

            Images render inline for the user and are also returned to you so
            you can check the result; other files become downloads. Showing the
            same path again replaces its earlier attachment.

            Args:
                path: Path in the sandbox, e.g. fractal_tree.png
            """
            session_id, note = run.sandbox()
            body, declared, name = sandbox.read_file(session_id, path)
            return run.attach(path, body, declared, name, note)

        return [run_code, run_shell, show_file]

    def attach(self, path, body, declared, name, note):
        """Stage a file as an artifact and build show_file's tool result."""
        mime = _sniff(body, declared)
        # Re-showing a path replaces its attachment: after a fix-and-rerender
        # the user should see the corrected image, not both versions.
        artifact = next((a for a in self.artifacts if a["path"] == path), None)
        replaced = artifact is not None
        if not replaced:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80] or "file"
            artifact = {"key": f"{self.prefix}/files/{len(self.artifacts) + 1}-{safe}",
                        "path": path}
            self.artifacts.append(artifact)
        artifact.update(name=name, mime=mime, size=len(body))
        s3.put_object(Bucket=BACKEND_BUCKET, Key=artifact["key"], Body=body, ContentType=mime)
        self.step(type="file", name=name, mime=mime, size=len(body), replaced=replaced)

        fmt = IMAGE_FORMATS.get(mime)
        if fmt and len(body) <= IMAGE_BYTES_LIMIT and not _png_too_large(body):
            return {"status": "success", "content": [
                {"text": f"{note}Shown to the user: {name} ({len(body):,} bytes). "
                         "Here it is so you can check it."},
                {"image": {"format": fmt, "source": {"bytes": body}}}]}
        if mime.startswith("text/") or mime in ("application/json", "image/svg+xml"):
            text = body.decode("utf-8", errors="replace")
            return {"status": "success", "content": [
                {"text": f"{note}Attached {name} for the user. Contents:\n{text[:20000]}"}]}
        return {"status": "success", "content": [
            {"text": f"{note}Attached {name} ({mime}, {len(body):,} bytes) for the user "
                     "to download."}]}


class TraceHooks(HookProvider):
    """Turn Strands agent events into the web app's trace steps.

    The MicroVM version recorded these inline in its own loop; with Strands
    running the loop, hooks are how we see each step.
    """

    def __init__(self, run):
        self.run = run
        self.tool_calls = 0

    def register_hooks(self, registry, **_):
        registry.add_callback(MessageAddedEvent, self.on_message)
        registry.add_callback(BeforeToolCallEvent, self.on_tool_call)

    def on_message(self, event):
        # Text the model writes alongside a tool call is its reasoning.
        message = event.message
        if message.get("role") != "assistant":
            return
        blocks = message.get("content") or []
        if any("toolUse" in b for b in blocks):
            text = "\n".join(b["text"] for b in blocks if "text" in b).strip()
            if text:
                self.run.step(type="reasoning", text=text)

    def on_tool_call(self, event):
        self.tool_calls += 1
        if self.tool_calls > MAX_TOOL_CALLS:
            event.cancel_tool = (f"Tool-call limit ({MAX_TOOL_CALLS}) reached for this "
                                 "message. Answer with what you have.")
            return
        use = event.tool_use
        self.run.step(type="tool_call", tool=use["name"], input=use.get("input") or {})


def process_query(user_id, conv_id, query_id):
    """Run one message end to end and persist answer, trace and files."""
    run = Run(user_id, conv_id, query_id)
    _update_status(user_id, conv_id, query_id, "processing", run.trace_key)

    try:
        question = _read_s3_text(f"{run.prefix}/question.txt").strip()
    except Exception as exc:
        logger.exception("Failed to read question from S3")
        _fail(user_id, conv_id, query_id, f"Could not read question: {exc}")
        return
    if not question:
        _fail(user_id, conv_id, query_id, "Question is empty")
        return

    t0 = time.time()
    try:
        # AgentCore Memory: one Memory session per conversation, one actor per
        # user. Constructing the Agent restores every earlier message.
        session_manager = AgentCoreMemorySessionManager(
            AgentCoreMemoryConfig(memory_id=MEMORY_ID, session_id=conv_id, actor_id=user_id),
            region_name=REGION)
        agent = Agent(
            model=BedrockModel(model_id=MODEL_ID, region_name=REGION, max_tokens=8192,
                               cache_config=CacheConfig(strategy="auto")),
            system_prompt=SYSTEM_PROMPT,
            tools=run.tools(),
            session_manager=session_manager,
            conversation_manager=SlidingWindowConversationManager(
                window_size=HISTORY_WINDOW_MESSAGES),
            hooks=[TraceHooks(run)],
            callback_handler=None,
        )

        replayed = context.clean_restored(agent.messages)
        state_text, state_summary = context.state_block(user_id, conv_id)
        if replayed or state_text:
            parts = [f"{replayed} earlier message(s) restored from AgentCore Memory"] if replayed else []
            if state_summary:
                parts.append(state_summary)
            run.step(type="context", text="Context: " + "; ".join(parts))

        prompt = ([{"text": state_text}] if state_text else []) + [{"text": question}]
        result = agent(prompt)
    except Exception as exc:
        logger.exception("Agent failed")
        _fail(user_id, conv_id, query_id, f"Agent failed: {exc}")
        return

    blocks = (result.message or {}).get("content") or []
    answer = "\n".join(b["text"] for b in blocks if "text" in b).strip()
    if result.stop_reason == "max_tokens":
        answer += "\n\n_(The answer hit the output limit and was cut short.)_"
    answer = answer or "(the model returned no answer)"

    # Inventory while the session is warm from this message's work. Only when
    # the sandbox was used: otherwise nothing in it can have changed.
    if run.session_id is not None:
        inventory, summary = context.capture_inventory(run.session_id)
        if inventory:
            context.save_inventory(user_id, conv_id, run.session_id, inventory, summary)

    usage = dict(result.metrics.accumulated_usage or {})
    tokens = budget_tokens(usage)
    run.steps.append({"type": "answer"})
    logger.info("Query complete. conv=%s query=%s steps=%d files=%d elapsed=%.1fs usage=%s budget=%d",
                conv_id, query_id, len(run.steps), len(run.artifacts), time.time() - t0,
                json.dumps(usage), tokens)

    answer_key = f"{run.prefix}/answer.txt"
    try:
        _write_s3_text(answer_key, answer)
        _write_s3_json(run.trace_key, run.steps)
    except Exception as exc:
        logger.exception("Failed to write answer/trace to S3")
        _fail(user_id, conv_id, query_id, f"Failed to store result: {exc}")
        return
    _finalize(user_id, conv_id, query_id, answer_key, tokens, run.artifacts)
    accumulate_tokens(user_id, tokens)
