# ================================================================================
# context.py
#
# What the model knows when a new message starts, beyond the question.
#
#   1. The sandbox. The Code Interpreter session holds real state -- files,
#      Python names, the shell's cwd -- but the model only knows what it is
#      told. At the end of every message that used the sandbox,
#      capture_inventory() runs two read-only cells while the session is
#      warm; the summary is stored on the CONV# item and state_block() hands it
#      to the model at the start of the next message. (Ported unchanged in
#      spirit from aws-microvm-agent-sandbox's memory.py.)
#
#   2. The conversation. AgentCore Memory, through Strands'
#      AgentCoreMemorySessionManager, stores every message and restores the
#      whole conversation into the agent. That replaces the MicroVM version's
#      messages.json + replay code. What remains ours is hygiene on the
#      restored copy (clean_restored): old <sandbox_state> blocks are stale and
#      dropped, old images become placeholders, and long outputs are clipped --
#      the same rules the MicroVM version applied when saving.
#
# CONV# attributes written here: sandbox_inventory, sandbox_inventory_session,
# sandbox_inventory_summary
# ================================================================================

import json
import logging
import os

import boto3

import sandbox

logger = logging.getLogger()

table = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"]).Table(
    os.environ["TABLE_NAME"])

INVENTORY_LIMIT = 3_000
RESTORED_RESULT_LIMIT = 4_000
STATE_OPEN = "<sandbox_state>"

# ================================================================================
# Sandbox inventory
# ================================================================================

# Runs in the Python session. Everything it defines starts with an underscore
# and is deleted afterwards, so taking the inventory never shows up in it.
# String values are described by length only: a variable may hold a secret.
PY_INVENTORY = r'''
def __sandbox_inventory():
    import inspect, json, types
    # Ranked so what the model can reuse comes first: its own functions and
    # classes, then data, then imports, then loop leftovers (plain scalars).
    ranked = {0: [], 1: [], 2: [], 3: []}
    # Names the session had before the model ran anything (see
    # sandbox.BOOTSTRAP) belong to Code Interpreter's IPython kernel.
    baseline = globals().get("_sb_py_baseline", set())
    for name, value in list(globals().items()):
        if name.startswith("_") or name in baseline:
            continue
        try:
            home = getattr(inspect.getmodule(value), "__name__", "__main__")
            if isinstance(value, types.ModuleType):
                ranked[2].append(f"import {value.__name__}" +
                                 ("" if value.__name__ == name else f" as {name}"))
            elif (inspect.isclass(value) or inspect.isfunction(value)) and home != "__main__":
                ranked[2].append(f"from {home} import {name}")
            elif inspect.isclass(value):
                ranked[0].append(f"class {name}")
            elif inspect.isfunction(value):
                ranked[0].append(f"def {name}{inspect.signature(value)}")
            else:
                kind = type(value).__name__
                shape = getattr(value, "shape", None)
                if shape == ():          # numpy scalar: its value says more
                    value, shape = value.item(), None
                if shape is not None and not callable(shape):
                    ranked[1].append(f"{name}: {kind} shape={tuple(shape)}")
                elif isinstance(value, (bool, int, float, complex)):
                    ranked[3].append(f"{name} = {value!r}"[:60])
                elif isinstance(value, (str, bytes, list, tuple, dict, set)):
                    ranked[1].append(f"{name}: {kind} len={len(value)}")
                else:
                    ranked[1].append(f"{name}: {kind}")
        except Exception:
            ranked[1].append(f"{name}: ?")
    rows = ranked[0] + ranked[1] + ranked[2]
    if ranked[3]:
        rows.append("scalars: " + ", ".join(ranked[3][:30]))
    print(json.dumps(rows[:80]))
try:
    __sandbox_inventory()
finally:
    del __sandbox_inventory
'''

# Runs in the persistent bash. Pipelines run in subshells, so nothing
# lingers. Exports are compared with the environment the shell inherited
# (/tmp/.sb_env_baseline, see sandbox.BOOTSTRAP): only names the shell added
# are reported, and only names -- values may be credentials. Files are listed
# from $HOME, the sandbox's default working directory, minus the entries it
# already held at session start (/tmp/.sb_baseline, see sandbox.BOOTSTRAP).
SH_INVENTORY = r'''
printf 'cwd\t%s\n' "$PWD"
declare -F | while read -r _ _ __inv_f; do
  case "$__inv_f" in exit|logout) ;; *) printf 'function\t%s\n' "$__inv_f" ;; esac
done
compgen -e | while read -r __inv_v; do
  case "$__inv_v" in PWD|OLDPWD|SHLVL|_) continue ;; esac
  grep -qx -- "$__inv_v" /tmp/.sb_env_baseline || printf 'export\t%s\n' "$__inv_v"
done
find "$HOME" -mindepth 1 -maxdepth 2 \( -name '.*' -o -name .git \) -prune -o \
  -printf 'file\t%P\t%s\t%y\n' 2>/dev/null \
  | awk -F'\t' 'NR==FNR { base[$0] = 1; next } { split($2, p, "/"); if (!(p[1] in base)) print }' \
      /tmp/.sb_baseline - | sort | head -n 40
'''


def _size(n):
    n = int(n)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def capture_inventory(session_id):
    """Describe what the sandbox holds right now, for the next message.

    Best effort: a failing cell is simply left out.

    Returns:
        (text, summary) -- the block for the model and a short trace line --
        or (None, None) if nothing could be captured.
    """
    py_rows = sh_out = None
    try:
        ok, out, _ = sandbox.run_python(session_id, PY_INVENTORY)
        if ok:
            py_rows = json.loads(out.strip().splitlines()[-1])
    except Exception:
        logger.exception("Python inventory failed (non-fatal)")
    try:
        ok, out, _ = sandbox.run_shell(session_id, SH_INVENTORY)
        if ok:
            sh_out = out
    except Exception:
        logger.exception("Shell inventory failed (non-fatal)")
    if py_rows is None and sh_out is None:
        return None, None

    files, cwd, functions, exports = [], None, [], []
    for line in (sh_out or "").splitlines():
        parts = line.split("\t")
        if parts[0] == "cwd" and len(parts) > 1:
            cwd = parts[1]
        elif parts[0] == "function" and len(parts) > 1:
            functions.append(parts[1])
        elif parts[0] == "export" and len(parts) > 1:
            exports.append(parts[1])
        elif parts[0] == "file" and len(parts) > 3:
            files.append(parts[1] + "/" if parts[3] == "d" else f"{parts[1]}  ({_size(parts[2])})")

    lines = [f"Captured at the end of your previous message, in Code Interpreter "
             f"session {session_id}."]
    lines.append(f"Files in the working directory ({len(files)}{'+' if len(files) >= 40 else ''}):")
    lines += [f"  {f}" for f in files] or ["  (none)"]
    if py_rows is not None:
        lines.append("Python session (run_code):")
        lines += [f"  {r}" for r in py_rows] or ["  (nothing defined)"]
    if sh_out is not None:
        lines.append("Bash session (run_shell):")
        lines.append(f"  cwd: {cwd or '(default)'}")
        lines.append(f"  exported: {', '.join(exports) if exports else '(none)'}")
        lines.append(f"  functions: {', '.join(functions) if functions else '(none)'}")
    text = "\n".join(lines)
    if len(text) > INVENTORY_LIMIT:
        text = text[:INVENTORY_LIMIT] + "\n  ... (truncated)"
    summary = (f"{len(files)} file(s), {len(py_rows or [])} Python name(s), "
               f"bash cwd {cwd or '(default)'}")
    return text, summary


def save_inventory(user_id, conv_id, session_id, text, summary):
    """Record the inventory on the conversation, tagged with its session."""
    try:
        table.update_item(
            Key={"pk": f"USER#{user_id}", "sk": f"CONV#{conv_id}"},
            UpdateExpression=("SET sandbox_inventory = :t, sandbox_inventory_session = :s, "
                              "sandbox_inventory_summary = :m"),
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":t": text, ":s": session_id, ":m": summary})
    except Exception:
        logger.exception("Could not save sandbox inventory (non-fatal)")


def state_block(user_id, conv_id):
    """Tell the model what the conversation's sandbox holds, before it acts.

    Returns:
        (text, summary): text is a <sandbox_state> block to put in front of
        the question, or None when this conversation never had a sandbox.
    """
    item = table.get_item(Key={"pk": f"USER#{user_id}", "sk": f"CONV#{conv_id}"},
                          ConsistentRead=True).get("Item") or {}
    session_id = item.get("ci_session_id")
    if not session_id:
        return None, None

    try:
        gone = not sandbox.alive(session_id)
    except Exception:
        logger.exception("GetCodeInterpreterSession failed; assuming it is alive")
        gone = False
    if gone:
        return (f"{STATE_OPEN}\nThe sandbox used earlier in this conversation no "
                "longer exists (Code Interpreter sessions end 8 hours after they "
                "start). Its files, variables and shell state are gone. A fresh "
                "sandbox starts when you next run code; do not assume anything from "
                "earlier messages is still defined.\n</sandbox_state>",
                "earlier sandbox expired")

    if item.get("sandbox_inventory") and item.get("sandbox_inventory_session") == session_id:
        return (f"{STATE_OPEN}\n{item['sandbox_inventory']}\n\nAll of this still "
                "exists in the conversation's sandbox. Reuse it -- call existing "
                "functions, read existing files -- instead of recreating it.\n"
                "</sandbox_state>",
                f"sandbox state: {item.get('sandbox_inventory_summary') or 'captured'}")

    return (f"{STATE_OPEN}\nThis conversation has a sandbox (session {session_id}) from "
            "earlier messages, but its contents were not captured. Inspect it "
            "before assuming what is defined.\n</sandbox_state>",
            "sandbox state unknown")


# ================================================================================
# Hygiene for the restored conversation
# ================================================================================

def clean_restored(messages):
    """Tidy the conversation AgentCore Memory restored, in place.

    Memory keeps every message exactly, images and all. Replaying that as-is
    would resend every earlier image (thousands of tokens each) and every old
    <sandbox_state> block, now stale. The stored events are not changed --
    only the copy the model is about to see.

    Returns:
        How many earlier questions the restored history covers.
    """
    questions = 0
    for message in messages:
        content = []
        for block in message.get("content") or []:
            if "text" in block and block["text"].startswith(STATE_OPEN):
                continue
            if "toolResult" in block:
                kept = []
                for part in block["toolResult"].get("content") or []:
                    if "image" in part:
                        kept.append({"text": "[image shown to the user and to you at "
                                             "the time; not replayed]"})
                    elif "text" in part and len(part["text"]) > RESTORED_RESULT_LIMIT:
                        kept.append({"text": part["text"][:RESTORED_RESULT_LIMIT]
                                     + "\n... (clipped in history)"})
                    else:
                        kept.append(part)
                block["toolResult"]["content"] = kept or [{"text": "(no output)"}]
            content.append(block)
        if message.get("role") == "user" and any("text" in b for b in content):
            questions += 1
        message["content"] = content or [{"text": "(context omitted)"}]
    return questions
