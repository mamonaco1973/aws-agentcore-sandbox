# --------------------------------------------------------------------------------
# DATA: archive_file.lambdas_zip
#
# Packages the API Lambda's source from code/. boto3 is not in here -- it
# comes from the layer in lambdas.tf. The agent (agent/) is packaged
# separately by apply.sh, with ARM64 wheels, for AgentCore Runtime.
# --------------------------------------------------------------------------------
data "archive_file" "lambdas_zip" {
  type        = "zip"
  source_dir  = "${path.module}/code"
  output_path = "${path.module}/lambdas.zip"
}
