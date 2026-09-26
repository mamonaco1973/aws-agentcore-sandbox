# ================================================================================
# Provider constraints
#
# Pin a floor rather than an exact version so `terraform init` still tracks
# patch/minor updates. 6.46 is the floor for aws_bedrockagentcore_agent_runtime
# with code_configuration (direct code deploy, 6.22+) and the current schema.
# ================================================================================

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.46"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }
}
