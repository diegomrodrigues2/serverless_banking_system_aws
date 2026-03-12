# Provider configurado explicitamente no root module
# Autenticação via SSO/OIDC — nunca credenciais estáticas
provider "aws" {
  region = var.aws_region

  # Em CI/CD: usar OIDC ou instance profile
  # Em local: usar `aws sso login` + profile configurado
  # Nunca usar access_key / secret_key aqui

  default_tags {
    tags = {
      Environment = "dev"
      Project     = "double-entry-ledger"
      ManagedBy   = "terraform"
    }
  }
}
