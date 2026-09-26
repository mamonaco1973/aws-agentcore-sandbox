# ==============================================================================
# Code Interpreter — the managed sandbox
# ==============================================================================
# A custom interpreter rather than the built-in aws.codeinterpreter.v1, for one
# reason: network mode. The built-in one has no internet, so `pip install`
# and downloads fail; PUBLIC gives the sandbox outbound internet, matching the
# INTERNET_EGRESS connector the MicroVM version attaches.
#
# No execution role: the sandbox needs no AWS permissions, the same stance as
# the MicroVM version's policy-less sandbox role. Files leave the sandbox
# through the agent (readFiles), never through the sandbox calling AWS.

resource "aws_bedrockagentcore_code_interpreter" "sandbox" {
  name        = "${local.name}_interpreter"
  description = "Per-conversation Python + bash sandbox for the agent"

  network_configuration {
    network_mode = "PUBLIC"
  }
}

# ==============================================================================
# Memory — conversation history (short-term events only)
# ==============================================================================
# Strands' AgentCoreMemorySessionManager writes every message of a
# conversation here as events, tool calls and results included, and restores
# them when the next message starts. No long-term strategies: the MicroVM
# version has no cross-conversation memory, and the comparison stays like for
# like. A strategy resource can be added later as an AgentCore-only extra.

resource "aws_bedrockagentcore_memory" "conversations" {
  name        = "${local.name}_memory"
  description = "Conversation history for the sandbox agent"

  # Days events are kept. Chats older than this lose their history.
  event_expiry_duration = 30
}

# ==============================================================================
# Outputs — consumed by apply.sh and passed into 02-core
# ==============================================================================

output "code_interpreter_id" {
  value = aws_bedrockagentcore_code_interpreter.sandbox.code_interpreter_id
}

output "code_interpreter_arn" {
  value = aws_bedrockagentcore_code_interpreter.sandbox.code_interpreter_arn
}

output "memory_id" {
  value = aws_bedrockagentcore_memory.conversations.id
}

output "memory_arn" {
  value = aws_bedrockagentcore_memory.conversations.arn
}
