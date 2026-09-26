# ================================================================================
# boto3 layer
# The Lambda runtime's bundled SDK may predate the bedrock-agentcore service
# model, so apply.sh vendors a current boto3 into dist/boto3-layer.zip. The API
# needs it to call InvokeAgentRuntime and to stop sessions.
# ================================================================================

resource "aws_lambda_layer_version" "boto3" {
  layer_name          = "agent-boto3-${random_id.bucket_suffix.hex}"
  filename            = "${path.module}/../dist/boto3-layer.zip"
  source_code_hash    = filebase64sha256("${path.module}/../dist/boto3-layer.zip")
  compatible_runtimes = ["python3.13"]
}

# ================================================================================
# API Lambda function
# Handles all synchronous API Gateway requests (conversations, queries, usage)
# and starts each message on the agent. There is no worker Lambda or queue:
# the agent runs in AgentCore Runtime (agent.tf).
# ================================================================================

resource "aws_lambda_function" "api" {
  function_name = "agent-api-${random_id.bucket_suffix.hex}"

  filename         = data.archive_file.lambdas_zip.output_path
  source_code_hash = data.archive_file.lambdas_zip.output_base64sha256

  handler = "handler.lambda_handler"
  runtime = "python3.13"
  layers  = [aws_lambda_layer_version.boto3.arn]

  role = aws_iam_role.lambda_exec.arn

  # Covers a cold Runtime session starting on the first message of a chat;
  # the agent's handler itself returns as soon as the message is running.
  timeout = 20

  environment {
    variables = {
      TABLE_NAME          = aws_dynamodb_table.app_table.name
      BACKEND_BUCKET_NAME = aws_s3_bucket.backend.bucket
      AGENT_RUNTIME_ARN   = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn
      CODE_INTERPRETER_ID = var.code_interpreter_id
    }
  }
}

# ================================================================================
# CloudWatch log group for API Lambda
# ================================================================================

resource "aws_cloudwatch_log_group" "api_logs" {
  name              = "/aws/lambda/${aws_lambda_function.api.function_name}"
  retention_in_days = 7
}
