variable "project_id" {
  type = string
}
variable "region" {
  type    = string
  default = "europe-west1"
}
variable "image" {
  type        = string
  description = "Container image URL produced by Cloud Build"
}
variable "model_provider" {
  type    = string
  default = "mock"
}
variable "model_endpoint" {
  type    = string
  default = ""
}
variable "model_api_key_secret_version" {
  type        = string
  default     = null
  nullable    = true
  description = "Existing chat-eval-model-api-key Secret Manager version number to expose as MODEL_API_KEY; the secret value must never be supplied to Terraform"

  validation {
    condition = (
      var.model_api_key_secret_version == null ||
      can(regex("^[1-9][0-9]*$", var.model_api_key_secret_version))
    )
    error_message = "model_api_key_secret_version must be null or a numeric Secret Manager version."
  }
}
variable "deletion_protection" {
  type    = bool
  default = true
}
