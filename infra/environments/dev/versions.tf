terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }

  # Backend remoto S3 com locking via use_lockfile
  # Credenciais nunca são embutidas aqui — use SSO/OIDC na execução
  backend "s3" {
    bucket       = "ledger-terraform-state-dev"
    key          = "double-entry-ledger/dev/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
    encrypt      = true
  }
}
