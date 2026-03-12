# ─────────────────────────────────────────────────────────────────────────────
# versions.tf — Restrições de versão do módulo cloudwatch-validation-alarms
#
# Providers são configurados exclusivamente no root module.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}
