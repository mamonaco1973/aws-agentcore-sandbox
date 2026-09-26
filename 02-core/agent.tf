# ================================================================================
# The agent — Strands + Claude on AgentCore Runtime
# ================================================================================
# Deployed as code, not a container: apply.sh zips agent/*.py with ARM64
# wheels (pip --platform, no Docker or buildx needed) into dist/agent.zip. The
# object key carries the zip's hash, so a code change produces a new key and
# Terraform updates the runtime to it.

locals {
  agent_name = "agentcore_sandbox_agent"
  agent_key  = "agent-code/agent-${filesha256("${path.module}/../dist/agent.zip")}.zip"
}

resource "aws_s3_object" "agent_code" {
  bucket = aws_s3_bucket.backend.id
  key    = local.agent_key
  source = "${path.module}/../dist/agent.zip"
}

resource "aws_bedrockagentcore_agent_runtime" "agent" {
  agent_runtime_name = local.agent_name
  description        = "Coding agent with a Code Interpreter sandbox"
  role_arn           = aws_iam_role.agent.arn

  agent_runtime_artifact {
    code_configuration {
      entry_point = ["main.py"]
      runtime     = "PYTHON_3_13"
      code {
        s3 {
          bucket = aws_s3_bucket.backend.id
          prefix = aws_s3_object.agent_code.key
        }
      }
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  lifecycle_configuration {
    # A conversation's Runtime session idles out after 15 minutes; the next
    # message starts a fresh one in seconds. Nothing is lost -- history is in
    # Memory and the sandbox is a separate Code Interpreter session -- so a
    # longer idle window would only pay for an idle microVM.
    idle_runtime_session_timeout = 900
    # The service maximum: one message may run this long in the background.
    max_lifetime = 28800
  }

  environment_variables = {
    TABLE_NAME          = aws_dynamodb_table.app_table.name
    BACKEND_BUCKET_NAME = aws_s3_bucket.backend.bucket
    BEDROCK_MODEL_ID    = var.bedrock_model_id
    CODE_INTERPRETER_ID = var.code_interpreter_id
    MEMORY_ID           = var.memory_id
  }

  depends_on = [aws_iam_role_policy.agent]
}

# ================================================================================
# Agent execution role — what the agent (and so the model) can reach
# ================================================================================
# Logs/X-Ray/metrics/workload-identity statements follow the AgentCore
# direct-code-deploy execution role in the AWS docs. The rest is scoped to
# this deployment: the model, this Code Interpreter, this Memory, this table,
# and the user-data prefix of this bucket.

resource "aws_iam_role" "agent" {
  name = "agentcore-sandbox-agent-${random_id.bucket_suffix.hex}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike = {
          "aws:SourceArn" = "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "agent" {
  role = aws_iam_role.agent.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:DescribeLogStreams", "logs:CreateLogGroup"]
        Resource = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
      },
      {
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
        Resource = "*"
      },
      {
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" } }
      },
      {
        Effect = "Allow"
        Action = ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT"]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:workload-identity-directory/default/workload-identity/${local.agent_name}-*",
        ]
      },
      {
        # Converse via a cross-region inference profile: the profile and the
        # foundation model in every region it may route to.
        Sid    = "InvokeModel"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = [
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}",
          "arn:aws:bedrock:*::foundation-model/*",
        ]
      },
      {
        Sid    = "Sandbox"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:StartCodeInterpreterSession",
          "bedrock-agentcore:InvokeCodeInterpreter",
          "bedrock-agentcore:GetCodeInterpreterSession",
          "bedrock-agentcore:StopCodeInterpreterSession",
        ]
        Resource = var.code_interpreter_arn
      },
      {
        Sid    = "ConversationMemory"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:DeleteEvent",
        ]
        Resource = var.memory_arn
      },
      {
        Sid      = "AppTable"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.app_table.arn
      },
      {
        # Questions in, answers/traces/files out -- the same prefix the API
        # reads. The agent code under agent-code/ is not readable by it.
        Sid      = "UserData"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.backend.arn}/users/*"
      },
    ]
  })
}

output "agent_runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn
}
