# CLAUDE.md

Guidance for working in **aws-agentcore-sandbox** (product name: **AgentCore Sandbox**).
Read this before changing `02-core/agent/`; several things here look like
obvious improvements and are not.

## What This App Is

The AgentCore twin of `../aws-microvm-agent-sandbox`: the same chat app, model,
tools, prompts and demo ("Build me a fractal tree and get me the results"),
with the sandbox, hosting and memory swapped for Amazon Bedrock AgentCore:

- sandbox → **Code Interpreter** (custom, PUBLIC network), one session per conversation
- worker Lambda + SQS → **Runtime** (direct code deploy, background task per message)
- `messages.json` replay → **Memory** via Strands' `AgentCoreMemorySessionManager`
- hand-written Converse loop → **Strands Agents**

Keep the two projects comparable: the web app, trace steps, S3/DynamoDB
records, prompts and tools should stay in step. A change made to one
project's shared parts usually belongs in the other too.

## Architecture

    01-agentcore/        Terraform: Code Interpreter + Memory
    02-core/             Terraform: runtime (agent.tf), API, Cognito, DynamoDB, S3, CloudFront
      agent/             Runs on AgentCore Runtime: main.py, query.py, sandbox.py, context.py
      code/              API Lambda: handler, conversations, users, runtime.py
    03-webapp/           SPA, uploaded by apply.sh

### Request flow

1. `POST /conversations/{id}/queries`: the API Lambda writes `question.txt`,
   creates the `QUERY#` record, and calls `InvokeAgentRuntime`
   (`runtime.start_query`) with `runtimeSessionId = conversation-<conv_id>`.
2. `main.py` registers an async task (the session reports `HealthyBusy`, so
   the Runtime does not reap it), starts `query.process_query` on a thread,
   and returns `accepted` in milliseconds.
3. `process_query` builds the Strands agent. Constructing it restores the
   conversation from Memory. It runs `context.clean_restored`, prepends
   `context.state_block`, and runs. `TraceHooks` turn Strands events into
   trace steps, written to S3 after every step.
4. At the end it takes the sandbox inventory, then writes the answer, trace,
   artifacts and token usage, the same records as the MicroVM worker.

## Rules That Are Load-Bearing

- **The persistent shell lives inside the Python session.** Code
  Interpreter's `executeCommand` is a fresh shell every call (confirmed by
  test: new PID, cwd reset, exports lost). `sandbox.BOOTSTRAP` starts bash
  from Python; `run_shell` drives it through `executeCode`. Inside it:
  - commands travel base64-encoded and run via `eval … </dev/null` (stdin is
    the command channel);
  - output is framed by a random end marker, read from the raw file
    descriptor with `select`;
  - `exit` is shadowed by a function;
  - a dead or timed-out shell restarts with a note to the model.
  Never switch `run_shell` to `executeCommand` "for simplicity".
- **All bootstrap names start with `_sb_`** and `_sb_py_baseline` records the
  IPython kernel's own names (`In`, `Out`, `get_ipython`, …). The Python
  inventory skips both.
- **The inventory reports only what's new.** Code Interpreter's home
  directory already holds its own files (`node_modules`, `log/`), and IPython
  sets environment variables in-process. At session start, BOOTSTRAP writes
  `/tmp/.sb_baseline` (the home directory's entries) and
  `/tmp/.sb_env_baseline` (the environment). The inventory compares against
  those.
- **Clean the restored history, never the stored events.**
  `clean_restored` strips old `<sandbox_state>` blocks, turns old images into
  placeholders and clips long outputs, working on `agent.messages` in memory.
  Memory keeps everything, and the session manager only appends new messages.
- **The model never needs to poll.** A Runtime background task has hours, so
  tools block until done. There is no `get_result` tool; don't reintroduce
  job polling.
- **Code Interpreter and Runtime sessions have separate lifecycles.** The
  Runtime session idles out after 15 minutes; the Code Interpreter session
  lives 8 hours. The Code Interpreter session ID therefore lives on the
  `CONV#` item (`ci_session_id`), never in Runtime memory.
- **The PUBLIC interpreter is needed for internet access.** The built-in
  `aws.codeinterpreter.v1` has no internet, so `pip install` fails there.
- **No root in the sandbox.** Commands run as `genesis1ptools` without
  sudo: no `dnf`, no git. The system prompt says so and suggests `curl`
  tarballs and `pip`. Don't promise the model otherwise.
- **The agent ships as ARM64 wheels in a zip.** `apply.sh` runs
  `pip --platform manylinux2014_aarch64 … --only-binary=:all:`. Runtime is
  ARM64-only, and this keeps Docker out of the build.
- **The token budget is applied at read time** (`users.token_limit()`, 1M).
  `budget_tokens` weights cache reads at 0.1× and writes at 1.25×.

## Gotchas Found While Building

- `InvokeAgentRuntime` needs `runtimeSessionId` of 33–256 characters
  (`conversation-<uuid>` is 49).
- Terraform's `code_configuration.code.s3.prefix` takes the zip's object
  key. The key carries the zip's hash so that code changes update the runtime.
- The Runtime execution role follows the AWS direct-deploy example (logs,
  X-Ray, metrics, workload identity), plus scoped model, Code Interpreter,
  Memory, DynamoDB and `users/*` S3 access. The caller of
  `CreateAgentRuntime` (Terraform) reads the code zip; the role doesn't.
- The Strands Code Interpreter tool in `strands-agents-tools` returns
  `readFiles` output as text, so the model could never see an image. That's
  why the tools here are custom.
- AgentCore Memory resources take ~2.5 minutes to become ACTIVE.
- Code Interpreter's Python is IPython: a trailing expression echoes its
  value, and tracebacks use IPython's format.

## Testing Without Deploying

The agent runs locally against real AgentCore services:
- create a temporary PUBLIC Code Interpreter and Memory with boto3;
- run `query.process_query`, or `main.py` itself, in a `python:3.13-slim`
  container with the pinned requirements;
- point `AWS_ENDPOINT_URL_DYNAMODB` and `AWS_ENDPOINT_URL_S3` at a moto server.

Driving `main.py` over HTTP (`/ping`, `/invocations`) exercises the Runtime
contract, including `HealthyBusy` while a message runs. Delete the temporary
resources afterwards.

## Model

`bedrock-config.sh` sets `BEDROCK_MODEL_ID` (default
`us.anthropic.claude-sonnet-4-6`). On the author's account Sonnet 5 is listed
but refused by Converse; `check_env.sh` probes with a real call.

## Code Commenting Standards

See the workspace-root `.claude/CLAUDE.md`: comment the *why*, not the *what*.
