"""
Configuração e fixtures compartilhadas para a suíte de testes do Validation Engine.

Fornece:
- Perfis Hypothesis específicos para o Validation Engine (local e CI)
- Fixtures de ambiente (local e AWS dev)
- Fixtures de geração de ASTs, contextos canônicos, bundles e snapshots

Perfis Hypothesis:
- "validation_local": desenvolvimento local — 100 exemplos, sem deadline
- "validation_ci": CI — 50 exemplos, sem deriving, sem deadline

Para usar o perfil CI: HYPOTHESIS_PROFILE=validation_ci pytest
"""
from __future__ import annotations

import os
from typing import Any, Mapping

import pytest
from hypothesis import HealthCheck, settings

# ---------------------------------------------------------------------------
# Perfis Hypothesis específicos para o Validation Engine
# ---------------------------------------------------------------------------

# Perfil local: mais exemplos para cobertura ampla durante desenvolvimento.
# max_examples=100 é suficiente para detectar a maioria dos bugs de property.
settings.register_profile(
    "validation_local",
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

# Perfil CI: menos exemplos para execução mais rápida em pipelines.
# deriving=False desativa a geração de estratégias derivadas automaticamente,
# tornando os testes mais previsíveis e rápidos em CI.
settings.register_profile(
    "validation_ci",
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

# Carrega o perfil de acordo com a variável de ambiente HYPOTHESIS_PROFILE.
# Se não definida, usa "validation_local" como padrão para desenvolvimento.
# Em CI, defina: HYPOTHESIS_PROFILE=validation_ci
_hypothesis_profile = os.environ.get("HYPOTHESIS_PROFILE", "validation_local")
if _hypothesis_profile in ("validation_local", "validation_ci"):
    settings.load_profile(_hypothesis_profile)


# ---------------------------------------------------------------------------
# Fixtures de ambiente
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def local_env() -> dict[str, str]:
    """
    Configuração de ambiente para testes locais.

    Retorna um dicionário com variáveis de ambiente simuladas para
    execução local sem dependências de AWS reais.

    Returns:
        Dicionário com configurações do ambiente local de teste.
    """
    return {
        "ENVIRONMENT": "local",
        "AWS_REGION": "us-east-1",
        # Credenciais fictícias para moto/localstack — não são validadas
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "VALIDATION_ENGINE_BUNDLE_BUCKET": "validation-engine-test-local",
        "VALIDATION_ENGINE_SNAPSHOT_BUCKET": "validation-engine-test-local",
        "VALIDATION_ENGINE_BUNDLE_PREFIX": "bundles/",
        "VALIDATION_ENGINE_SNAPSHOT_PREFIX": "snapshots/",
    }


@pytest.fixture(scope="session")
def aws_dev_env() -> dict[str, str]:
    """
    Configuração de ambiente para testes de integração AWS dev.

    Lê variáveis de ambiente reais. Testes que dependem desta fixture
    devem ser marcados com @pytest.mark.integration_aws_dev e serão
    ignorados se as variáveis não estiverem definidas.

    Returns:
        Dicionário com configurações do ambiente AWS dev.
    """
    return {
        "ENVIRONMENT": "dev",
        "AWS_REGION": os.environ.get("AWS_REGION", "us-east-1"),
        "VALIDATION_ENGINE_TEST_BUCKET": os.environ.get(
            "VALIDATION_ENGINE_TEST_BUCKET", ""
        ),
        "VALIDATION_ENGINE_TEST_PREFIX": os.environ.get(
            "VALIDATION_ENGINE_TEST_PREFIX", ""
        ),
        "VALIDATION_ENGINE_TEST_APPCONFIG_APP": os.environ.get(
            "VALIDATION_ENGINE_TEST_APPCONFIG_APP", ""
        ),
        "VALIDATION_ENGINE_TEST_DYNAMODB_TABLE": os.environ.get(
            "VALIDATION_ENGINE_TEST_DYNAMODB_TABLE", ""
        ),
    }


# ---------------------------------------------------------------------------
# Fixtures de geração de ASTs
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_ast_node() -> dict[str, Any]:
    """
    Nó AST de exemplo representando uma comparação simples.

    Representa a condição: facts.posting_count > 0

    Returns:
        Dicionário com a estrutura de um ComparisonNode serializado.
    """
    return {
        "type": "ComparisonNode",
        "left": {
            "type": "FieldAccessNode",
            "path": ["facts", "posting_count"],
        },
        "operator": ">",
        "right": {
            "type": "LiteralNode",
            "value": 0,
        },
    }


@pytest.fixture
def sample_rule_ast() -> dict[str, Any]:
    """
    AST completo de uma rule de exemplo.

    Representa uma policy que nega transações com posting_count == 0.

    Returns:
        Dicionário com a estrutura de um RuleAST serializado.
    """
    return {
        "rules": [
            {
                "type": "PolicyRuleNode",
                "name": "deny_empty_postings",
                "priority": 100,
                "effect": "DENY",
                "message": "Transaction must have at least one posting",
                "condition": {
                    "type": "ComparisonNode",
                    "left": {
                        "type": "FieldAccessNode",
                        "path": ["facts", "posting_count"],
                    },
                    "operator": "==",
                    "right": {
                        "type": "LiteralNode",
                        "value": 0,
                    },
                },
            }
        ],
        "composition_mode": "DENY_OVERRIDES",
    }


# ---------------------------------------------------------------------------
# Fixtures de contextos canônicos
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_canonical_context() -> dict[str, Any]:
    """
    Contexto canônico de avaliação de exemplo.

    Representa uma transação de transferência PIX simples com dois postings
    balanceados em BRL.

    Returns:
        Dicionário com a estrutura de um CanonicalValidationContext serializado.
    """
    return {
        "tenant_id": "tenant_test_001",
        "external_id": "ext_test_001",
        "operation_type": "TRANSFER",
        "product_code": "PIX",
        "channel": "MOBILE",
        "postings": [
            {
                "account_id": "acc_debit_001",
                "amount": 10000,
                "currency": "BRL",
                "direction": "DEBIT",
                "account_type": "CHECKING",
            },
            {
                "account_id": "acc_credit_001",
                "amount": 10000,
                "currency": "BRL",
                "direction": "CREDIT",
                "account_type": "CHECKING",
            },
        ],
        "policy_context": {
            "daily_limit_minor": 500000,
            "channel_risk_score": 1,
        },
        "facts": {
            "posting_count": 2,
            "distinct_account_count": 2,
            "currencies": ["BRL"],
            "total_debits_by_currency": {"BRL": 10000},
            "total_credits_by_currency": {"BRL": 10000},
            "max_posting_amount": 10000,
            "has_platform_account": False,
        },
        "context_schema_version": "1.0",
    }


# ---------------------------------------------------------------------------
# Fixtures de bundles e snapshots
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_rule_bundle() -> dict[str, Any]:
    """
    RuleBundle de exemplo para testes.

    Bundle mínimo com uma rule de deny para transações acima do limite diário.

    Returns:
        Dicionário com a estrutura de um RuleBundle serializado.
    """
    return {
        "policy_set_id": "bundle_test_001",
        "artifact_hash": "sha256:abc123def456",
        "ast": {
            "rules": [
                {
                    "type": "PolicyRuleNode",
                    "name": "deny_over_daily_limit",
                    "priority": 100,
                    "effect": "DENY",
                    "message": "Transaction exceeds daily debit limit",
                    "condition": {
                        "type": "ComparisonNode",
                        "left": {
                            "type": "AggregateNode",
                            "function": "SUM",
                            "collection": {"type": "CollectionRefNode", "name": "postings"},
                            "where": {
                                "type": "ComparisonNode",
                                "left": {
                                    "type": "FieldAccessNode",
                                    "path": ["direction"],
                                },
                                "operator": "==",
                                "right": {"type": "LiteralNode", "value": "DEBIT"},
                            },
                            "select": {
                                "type": "FieldAccessNode",
                                "path": ["amount"],
                            },
                        },
                        "operator": ">",
                        "right": {
                            "type": "RefAccessNode",
                            "path": ["daily_limit_minor"],
                        },
                    },
                }
            ],
            "composition_mode": "DENY_OVERRIDES",
        },
        "execution_plan": {},
        "compatibility": {
            "dsl_version": "1.0",
            "context_schema_version": "1.0",
            "snapshot_schema_version": "1.0",
            "evaluator_min_version": "1.0.0",
        },
        "composition_mode": "DENY_OVERRIDES",
        "metadata": {
            "author": "test",
            "description": "Test bundle",
            "compiled_at": "2024-01-01T00:00:00Z",
            "source_hash": "sha256:source123",
        },
    }


@pytest.fixture
def sample_reference_snapshot() -> dict[str, Any]:
    """
    ReferenceSnapshot de exemplo para testes.

    Snapshot com limites diários e lista de contas bloqueadas para testes.

    Returns:
        Dicionário com a estrutura de um ReferenceSnapshot serializado.
    """
    return {
        "snapshot_version": "snap_test_001",
        "snapshot_schema_version": "1.0",
        "created_at": "2024-01-01T00:00:00Z",
        "data": {
            "daily_limit_minor": 500000,
            "blocked_accounts": [],
            "allowed_currencies": ["BRL", "USD"],
        },
    }
