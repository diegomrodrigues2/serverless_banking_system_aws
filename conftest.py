"""
Configuração raiz do pytest para o projeto Double-Entry Ledger.

Este arquivo registra os markers customizados utilizados em toda a suíte de testes:
- unit: testes unitários do domínio e camadas de aplicação
- property: testes baseados em propriedades usando Hypothesis
- integration: testes de integração com DynamoDB Local via Finch

Perfis Hypothesis:
- "default": perfil padrão para desenvolvimento local — rápido (50 exemplos, sem deadline).
- "ci": perfil para CI — mais exemplos (200) para cobertura mais ampla.

Para usar o perfil CI: HYPOTHESIS_PROFILE=ci pytest
"""
import pytest
from hypothesis import HealthCheck, Phase, settings

# ---------------------------------------------------------------------------
# Perfis Hypothesis
# ---------------------------------------------------------------------------

# Perfil padrão: rápido para desenvolvimento local.
# - max_examples=50: suficiente para detectar a maioria dos bugs sem demorar.
# - deadline=None: desativa o limite de tempo por exemplo (evita flakiness em
#   máquinas lentas ou com carga variável).
# - suppress_health_check: desativa checks de saúde que causam falsos positivos
#   em testes com setup custoso (too_slow, filter_too_much).
settings.register_profile(
    "default",
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

# Perfil CI: mais exemplos para cobertura mais ampla em pipelines de integração.
settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

# Ativa o perfil padrão. Pode ser sobrescrito via variável de ambiente:
#   HYPOTHESIS_PROFILE=ci pytest
settings.load_profile("default")


def pytest_configure(config: pytest.Config) -> None:
    """Registra markers customizados para evitar warnings de marker desconhecido."""
    config.addinivalue_line("markers", "unit: testes unitários do domínio e camadas de aplicação")
    config.addinivalue_line(
        "markers", "property: testes baseados em propriedades usando Hypothesis"
    )
    config.addinivalue_line(
        "markers",
        "integration: testes de integração com DynamoDB Local via Finch",
    )
    config.addinivalue_line(
        "markers",
        "integration_local: testes de integração local (moto, DynamoDB Local, tempdir)",
    )
    config.addinivalue_line(
        "markers",
        "integration_aws_dev: testes de integração contra AWS dev (requer credenciais e recursos reais)",
    )
    config.addinivalue_line(
        "markers",
        "slow: testes lentos que podem ser excluídos em execuções rápidas",
    )
