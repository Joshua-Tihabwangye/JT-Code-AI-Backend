variable "environment" {
  type = string
}

variable "namespace" {
  type = string
}

variable "secrets" {
  description = "Contents of jt-code-secrets; must contain every key in secret_keys_file."
  type        = map(string)
  sensitive   = true
}

variable "kafka_role_credentials" {
  description = "Least-privilege Kafka logins: keys \"consumer\" (read topics, own groups) and \"admin\" (create/configure topics)."
  type = map(object({
    username = string
    password = string
  }))
  sensitive = true
}

variable "secret_keys_file" {
  type = string
}

variable "registry_username" {
  type = string
}

variable "registry_token" {
  description = "GHCR token with read:packages."
  type        = string
  sensitive   = true
}
