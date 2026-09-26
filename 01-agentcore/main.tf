# ==============================================================================
# Provider Configuration
# ==============================================================================
# The managed counterpart of aws-microvm-agent-sandbox's 01-sandbox phase.
# There, this phase built a MicroVM image from a Dockerfile. Here it creates
# two AgentCore resources and nothing is built: AWS runs the sandbox.

terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.18 added aws_bedrockagentcore_memory; 6.46 is the floor 02-core
      # needs, kept equal here so both phases pin the same provider.
      version = ">= 6.46"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "aws-agentcore-sandbox", ManagedBy = "Terraform" }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

locals {
  # AgentCore resource names allow letters, digits and underscores only.
  name = "agentcore_sandbox"
}
