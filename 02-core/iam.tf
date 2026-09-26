# ================================================================================
# API Lambda execution role
# (The agent runs as its own role, in agent.tf.)
# ================================================================================

resource "aws_iam_role" "lambda_exec" {
  name = "agent-app-lambda-${random_id.bucket_suffix.hex}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# ================================================================================
# CloudWatch logging
# ================================================================================

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ================================================================================
# DynamoDB access
# ================================================================================

resource "aws_iam_policy" "lambda_dynamodb" {
  name = "agent-app-dynamodb-${random_id.bucket_suffix.hex}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        # Scan used to count registered users for the USER_CAP check
        "dynamodb:Scan"
      ]
      Resource = aws_dynamodb_table.app_table.arn
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_dynamodb_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_dynamodb.arn
}

# ================================================================================
# S3 access — user data only: question/answer/trace payloads and the files the
# model showed from its sandbox. ListBucket lets a conversation delete sweep
# its whole prefix.
# ================================================================================

resource "aws_iam_policy" "lambda_s3" {
  name = "agent-s3-${random_id.bucket_suffix.hex}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "BackendBucketList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.backend.arn
      },
      {
        Sid      = "UserDataAccess"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.backend.arn}/users/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_s3_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_s3.arn
}

# ================================================================================
# AgentCore access — the API starts messages on the agent and, on delete,
# stops the conversation's Runtime and Code Interpreter sessions. It never
# calls the model or the sandbox itself: that is the agent's job.
# ================================================================================

resource "aws_iam_policy" "lambda_agentcore" {
  name = "agent-agentcore-${random_id.bucket_suffix.hex}"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokeAgent"
        Effect = "Allow"
        Action = ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"]
        # The runtime and its endpoints (the DEFAULT endpoint is what an
        # unqualified InvokeAgentRuntime resolves to).
        Resource = [
          aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn,
          "${aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn}/*",
        ]
      },
      {
        Sid      = "StopSandbox"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:StopCodeInterpreterSession"]
        Resource = var.code_interpreter_arn
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_agentcore_attach" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_agentcore.arn
}
