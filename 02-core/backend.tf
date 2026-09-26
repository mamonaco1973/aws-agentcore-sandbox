# ================================================================================
# Terraform Remote State — S3 backend
# State is stored in a pre-existing build bucket, not managed by this stack.
# ================================================================================

# terraform {
#   backend "s3" {
#     bucket = "your-terraform-state-bucket"
#     key    = "terraform/state/aws-agentcore-sandbox/terraform.tfstate"
#     region = "us-east-1"
#   }
# }
