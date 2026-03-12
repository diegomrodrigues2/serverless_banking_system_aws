# ─────────────────────────────────────────────────────────────────────────────
# versions.tf — Restrições de versão do módulo validation-engine-s3
#
# Este arquivo declara as versões mínimas exigidas pelo módulo.
# Providers são configurados exclusivamente no root module — este módulo
# não contém blocos provider.
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
