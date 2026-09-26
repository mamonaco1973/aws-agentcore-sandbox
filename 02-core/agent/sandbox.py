# ================================================================================
# sandbox.py
#
# One AgentCore Code Interpreter session per conversation, started on demand.
#
# The counterpart of aws-microvm-agent-sandbox's sandbox.py + its MicroVM
# image. There, we built the sandbox; here AWS runs it and we start sessions:
#
#   * Python: executeCode keeps its state between calls within a session, the
#     same behaviour as our kernel.py.
#   * Bash: executeCommand does NOT persist -- every call is a fresh shell, so
#     `cd` and `export` are lost (tested: new PID and cwd reset each call). The
#     persistent shell is rebuilt here: BOOTSTRAP starts a long-lived bash
#     process inside the Python session, and run_shell drives it through
#     executeCode. The rules from our shell.sh carry over -- commands are
#     eval'd in that shell with stdin closed, `exit` is shadowed, and a dead
#     shell restarts on the next command with a note saying what was lost.
#
# Session lifetime is a hard TTL from start (8 hours here), with no idle
# suspend: nothing like the MicroVM's suspend/resume exists. When it ends,
# everything in it is gone, and ensure() starts a fresh one.
#
# Record on pk=USER#<id>, sk=CONV#<id>: ci_session_id, ci_started_at
# ================================================================================

import base64
import json
import logging
import os
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()

REGION = os.environ["AWS_REGION"]
CODE_INTERPRETER_ID = os.environ["CODE_INTERPRETER_ID"]

# Hard TTL from start: the service maximum, matching the MicroVM's lifetime.
SESSION_TIMEOUT_SECONDS = 28800

# Longest a single shell command may run before its shell is killed and
# restarted. executeCode itself is capped at 15 minutes per call.
SHELL_TIMEOUT_SECONDS = 600

# Output handed back per call; head and tail kept, like our kernel.
OUTPUT_LIMIT = 64000

table = boto3.resource("dynamodb", region_name=REGION).Table(os.environ["TABLE_NAME"])

# read_timeout above the 15-minute executeCode ceiling, so a long cell ends
# with the service's answer rather than a client-side timeout.
client = boto3.client("bedrock-agentcore", region_name=REGION, config=Config(
    connect_timeout=10, read_timeout=960, retries={"mode": "standard", "max_attempts": 3}))


class SandboxError(Exception):
    """A sandbox call failed in a way the model should be told about."""


# ================================================================================
# The persistent shell, built inside the Python session
# ================================================================================
# Every name starts with _sb_ so the sandbox inventory (which skips names
# starting with "_", and anything in _sb_py_baseline) never reports the
# machinery as the model's own state.
# Commands travel base64-encoded, so quotes, heredocs and newlines in them
# cannot break the framing, and are eval'd with stdin from /dev/null: stdin is
# the command channel, and a `cat` or `read` would otherwise swallow it.

BOOTSTRAP = r'''
import base64 as _sb_b64, os as _sb_os, select as _sb_select
import subprocess as _sb_sp, time as _sb_time, uuid as _sb_uuid

# What the session already held before the model ran anything: the IPython
# kernel's own names (In, Out, get_ipython, exit, ...) and Code Interpreter's
# files in the home directory (node_modules for its JavaScript runtime, log/).
# The sandbox inventory reports only what is new, so these are recorded once.
_sb_py_baseline = set(globals())
with open("/tmp/.sb_baseline", "w") as _sb_f:
    _sb_f.write("\n".join(sorted(_sb_os.listdir(_sb_os.path.expanduser("~")))) + "\n")
# The environment the shell inherits. IPython sets variables (CLICOLOR, PAGER,
# MPLBACKEND, ...) inside this process, so the process's original environment
# is not a usable baseline; this snapshot is.
with open("/tmp/.sb_env_baseline", "w") as _sb_f:
    _sb_f.write("\n".join(sorted(_sb_os.environ)) + "\n")

_SB_GUARD = b"""exit() { echo "[exit ${1:-0} ignored: this shell is the persistent session]" >&2; return "${1:-0}"; }
logout() { exit "$@"; }
"""

def _sb_start():
    global _sb_sh
    _sb_sh = _sb_sp.Popen(["bash", "--noprofile", "--norc"], stdin=_sb_sp.PIPE,
                          stdout=_sb_sp.PIPE, stderr=_sb_sp.STDOUT)
    _sb_sh.stdin.write(_SB_GUARD)
    _sb_sh.stdin.flush()

def _sb_restart(why):
    try:
        _sb_sh.kill()
    except Exception:
        pass
    _sb_start()
    return ("[The bash session " + why + " and was restarted: cd, exports and "
            "shell variables are reset. Files on disk are intact.]\n")

def _sb_run(command, timeout):
    note = "" if _sb_sh.poll() is None else _sb_restart("had exited")
    mark = ("__SB_END_" + _sb_uuid.uuid4().hex + "__").encode()
    enc = _sb_b64.b64encode(command.encode()).decode()
    _sb_sh.stdin.write(('eval "$(printf %s ' + enc + ' | base64 -d)" </dev/null\n'
                        '_sb_rc=$?; printf "\\n%s%s\\n" "' + mark.decode() + '" "$_sb_rc"\n').encode())
    _sb_sh.stdin.flush()
    fd, buf, deadline = _sb_sh.stdout.fileno(), b"", _sb_time.time() + timeout
    while True:
        i = buf.find(b"\n" + mark)
        if i >= 0:
            j = buf.find(b"\n", i + 1 + len(mark))
            if j >= 0:
                rc = int(buf[i + 1 + len(mark):j] or b"1")
                return note + buf[:i].decode(errors="replace"), rc
        left = deadline - _sb_time.time()
        if left <= 0:
            return (note + buf.decode(errors="replace") + "\n" +
                    _sb_restart("timed out after %ds" % timeout)), 124
        ready, _, _ = _sb_select.select([fd], [], [], min(left, 5))
        if ready:
            chunk = _sb_os.read(fd, 65536)
            if not chunk:
                return (note + buf.decode(errors="replace") + "\n" +
                        _sb_restart("exited during this command")), 1
            buf += chunk

_sb_start()
print("sandbox shell ready")
'''


# ================================================================================
# Low-level API
# ================================================================================

def _invoke(session_id, name, **arguments):
    """Call InvokeCodeInterpreter and fold its event stream into one result.

    Returns:
        {"content": [...], "structured": {...}, "is_error": bool}
    """
    try:
        response = client.invoke_code_interpreter(
            codeInterpreterIdentifier=CODE_INTERPRETER_ID, sessionId=session_id,
            name=name, arguments=arguments)
    except ClientError as exc:
        raise SandboxError(f"Code Interpreter {name} failed: "
                           f"{exc.response['Error'].get('Message', exc)}") from None
    out = {"content": [], "structured": {}, "is_error": False}
    for event in response["stream"]:
        result = event.get("result")
        if not result:
            # Exception events arrive on the stream under their own key.
            kind, detail = next(iter(event.items()), ("unknown", ""))
            raise SandboxError(f"Code Interpreter {name}: {kind}: {detail}")
        out["content"] += result.get("content") or []
        out["structured"] = result.get("structuredContent") or out["structured"]
        out["is_error"] = out["is_error"] or bool(result.get("isError"))
    return out


def _clip(text):
    if len(text) <= OUTPUT_LIMIT:
        return text
    half = OUTPUT_LIMIT // 2
    return (text[:half] + f"\n... [{len(text) - OUTPUT_LIMIT} characters truncated] ...\n"
            + text[-half:])


def _text_of(result):
    """stdout and stderr of an executeCode/executeCommand result, in order."""
    s = result["structured"]
    parts = [s.get("stdout") or "", s.get("stderr") or ""]
    text = "".join(p if p.endswith("\n") or not p else p + "\n" for p in parts if p)
    if not text:
        # Some failures report only through content blocks.
        text = "\n".join(c.get("text", "") for c in result["content"] if c.get("type") == "text")
    return text


# ================================================================================
# Sessions
# ================================================================================

def _conv_key(user_id, conv_id):
    return {"pk": f"USER#{user_id}", "sk": f"CONV#{conv_id}"}


def alive(session_id):
    """True while a session can still run code. Does not touch its contents."""
    try:
        status = client.get_code_interpreter_session(
            codeInterpreterIdentifier=CODE_INTERPRETER_ID, sessionId=session_id)["status"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("ResourceNotFoundException", "ValidationException"):
            return False
        raise
    return status == "READY"


def _start():
    session_id = client.start_code_interpreter_session(
        codeInterpreterIdentifier=CODE_INTERPRETER_ID,
        name=f"conv_{uuid.uuid4().hex[:24]}",
        sessionTimeoutSeconds=SESSION_TIMEOUT_SECONDS)["sessionId"]
    booted = _invoke(session_id, "executeCode", code=BOOTSTRAP, language="python")
    if "sandbox shell ready" not in _text_of(booted):
        stop(session_id)
        raise SandboxError("Sandbox shell failed to start: " + _text_of(booted)[:500])
    return session_id


def ensure(user_id, conv_id):
    """Return this conversation's session, starting one if there is none.

    Returns:
        (session, event): session is {"id"}; event is None when an existing
        session was reused, or {"launched", "ms", "replaced"} for the trace.
    """
    key = _conv_key(user_id, conv_id)
    item = table.get_item(Key=key, ConsistentRead=True).get("Item") or {}
    old = item.get("ci_session_id")
    if old and alive(old):
        return {"id": old}, None

    started = time.perf_counter()
    session_id = _start()
    try:
        # Conditional, so two concurrent messages in one conversation cannot
        # each start a session and leave one running unrecorded; and
        # attribute_exists(pk) so a deleted conversation is never resurrected.
        table.update_item(
            Key=key,
            UpdateExpression="SET ci_session_id = :n, ci_started_at = :t",
            ConditionExpression=("attribute_exists(pk) AND "
                                 "(attribute_not_exists(ci_session_id) OR ci_session_id = :o)"),
            ExpressionAttributeValues={":n": session_id, ":t": int(time.time()),
                                       ":o": old or "-"})
    except ClientError as exc:
        stop(session_id)
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        if not table.get_item(Key=key, ConsistentRead=True).get("Item"):
            raise SandboxError("The conversation was deleted") from None
        return ensure(user_id, conv_id)
    return {"id": session_id}, {"launched": session_id, "replaced": bool(old),
                                "ms": round((time.perf_counter() - started) * 1000)}


def stop(session_id):
    """Stop a session. Best effort: one already gone is fine."""
    try:
        client.stop_code_interpreter_session(
            codeInterpreterIdentifier=CODE_INTERPRETER_ID, sessionId=session_id)
    except Exception:
        logger.exception("stop_code_interpreter_session failed for %s", session_id)


# ================================================================================
# Running code
# ================================================================================

def run_python(session_id, code):
    """Run a cell in the session's Python state. Returns (ok, output, ms)."""
    started = time.perf_counter()
    result = _invoke(session_id, "executeCode", code=code, language="python")
    ms = round((time.perf_counter() - started) * 1000)
    exit_code = result["structured"].get("exitCode")
    ok = not result["is_error"] and exit_code in (0, None)
    return ok, _clip(_text_of(result)) or "(no output)", ms


def run_shell(session_id, command):
    """Run a command in the session's persistent bash. Returns (ok, output, ms)."""
    enc = base64.b64encode(command.encode()).decode()
    cell = ("import json as _sb_json\n"
            f"print(_sb_json.dumps(_sb_run(_sb_b64.b64decode('{enc}').decode(), "
            f"{SHELL_TIMEOUT_SECONDS})))")
    started = time.perf_counter()
    result = _invoke(session_id, "executeCode", code=cell, language="python")
    stdout = result["structured"].get("stdout") or ""
    if "NameError" in _text_of(result) and "_sb_" in _text_of(result):
        # The machinery was deleted from the Python namespace (a cell ran
        # `del` or rebound a name). Rebuild it and run the command once more.
        _invoke(session_id, "executeCode", code=BOOTSTRAP, language="python")
        result = _invoke(session_id, "executeCode", code=cell, language="python")
        stdout = result["structured"].get("stdout") or ""
    ms = round((time.perf_counter() - started) * 1000)
    try:
        text, rc = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, _clip(_text_of(result)) or "(no output)", ms
    return rc == 0, _clip(text) or "(no output)", ms


def read_file(session_id, path):
    """Read one file's bytes out of the session. Returns (bytes, mime, name)."""
    result = _invoke(session_id, "readFiles", paths=[path])
    for item in result["content"]:
        resource = item.get("resource") or {}
        if "blob" in resource or "text" in resource:
            body = resource.get("blob")
            if body is None:
                body = resource["text"].encode()
            elif isinstance(body, str):
                body = base64.b64decode(body)
            name = os.path.basename(resource.get("uri") or path) or "file"
            return body, resource.get("mimeType") or "application/octet-stream", name
    raise SandboxError(_text_of(result).strip() or f"Could not read {path}")
