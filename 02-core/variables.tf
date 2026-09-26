# ================================================================================
# Frontend S3 bucket base name
# ================================================================================

variable "frontend_bucket_base_name" {
  description = "Base name for the frontend S3 bucket"
  type        = string
  default     = "agent-app"
}

# ================================================================================
# Backend S3 bucket base name
# ================================================================================

variable "backend_bucket_base_name" {
  description = "Base name for the backend S3 bucket"
  type        = string
  default     = "agent-data"
}

# ================================================================================
# AWS region
# ================================================================================

variable "region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

# ================================================================================
# Model — the Bedrock inference profile the agent (Strands) calls. It
# must support tool use and image input. Set in bedrock-config.sh.
# ================================================================================

variable "bedrock_model_id" {
  description = "Bedrock model or inference-profile id the agent uses"
  type        = string
}

# ================================================================================
# AgentCore resources — created by 01-agentcore and passed in by apply.sh
# ================================================================================

variable "code_interpreter_id" {
  description = "Custom (PUBLIC network) Code Interpreter the agent's sandboxes run on"
  type        = string
}

variable "code_interpreter_arn" {
  description = "ARN of that Code Interpreter, for IAM"
  type        = string
}

variable "memory_id" {
  description = "AgentCore Memory holding conversation history"
  type        = string
}

variable "memory_arn" {
  description = "ARN of that Memory, for IAM"
  type        = string
}

# ================================================================================
# Google OAuth — optional; set to enable Google sign-in via Cognito IdP
# Populated from AWS_AGENTOPS_GOOGLE_CLIENT_ID / AWS_AGENTOPS_GOOGLE_CLIENT_SECRET
# ================================================================================

variable "google_client_id" {
  description = "Google OAuth client ID for Cognito identity provider"
  type        = string
  default     = ""
}

variable "google_client_secret" {
  description = "Google OAuth client secret for Cognito identity provider"
  type        = string
  default     = ""
  sensitive   = true
}

# ================================================================================
# Custom domain — leave empty to serve directly from CloudFront's default domain
# When set, also set route53_zone_id to create ACM + DNS records automatically
# ================================================================================

variable "custom_domain" {
  description = "Custom domain name (e.g. sandbox.example.com). Leave empty to use CloudFront default domain. The parent hosted zone is looked up automatically."
  type        = string
  default     = ""
}
