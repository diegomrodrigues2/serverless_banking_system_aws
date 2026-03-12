"""
Testes de integração local para ManifestResolver e PolicyPublisher.

Usa mocks do appconfigdata client para simular o AppConfig localmente
sem dependências de AWS reais. Moto 5.x não suporta create_environment,
create_deployment_strategy nem start_deployment para AppConfig, então
mockamos o appconfigdata client diretamente.

Cenários cobertos:
- Publicar manifesto e resolver escopo publicado
- Múltiplos escopos no mesmo payload
- Resolver escopo após atualização (novo activation_id)
- Escopo ausente levanta PolicyBundleUnavailable
- Payload inválido levanta InvalidPolicyBundle
- Campos obrigatórios ausentes levanta InvalidPolicyBundle

Uso:
    pytest tests/validation_engine/integration/local/test_manifest_resolution.py -v
    pytest -m integration_local -v

Requisitos cobertos: 4.3, 5.1, 5.3
"""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from validation_engine.domain.errors import InvalidPolicyBundle, PolicyBundleUnavailable
from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
    PolicyActivationManifest,
    ReferenceSnapshot,
    RuleBundle,
)
from validation_engine.domain.policy_ast import CompositionMode, RuleAST
from validation_engine.infrastructure.manifest_resolver import ManifestResolver

# ---------------------------------------------------------------------------
# Constantes de configuração local
# ---------------------------------------------------------------------------

AWS_REGION = "us-east-1"
APP_ID = "app-integration-test"
ENV_ID = "env-integration-test"
PROFILE_ID = "profile-integration-test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bundle(snapshot_schema_version: str = "1.0") -> RuleBundle:
    """Cria um RuleBundle de teste."""
    return RuleBundle(
        policy_set_id="bundle_integration_test",
        artifact_hash="sha256:integration_bundle_hash_001",
        ast=RuleAST(rules=(), composition_mode=CompositionMode.DENY_OVERRIDES),
        execution_plan={},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version="1.0",
            snapshot_schema_version=snapshot_schema_version,
            evaluator_min_version="1.2.0",
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author="integration-test",
            description="Integration test bundle",
            compiled_at="2026-03-11T10:00:00Z",
            source_hash="sha256:source_hash_integration",
        ),
    )


def _make_snapshot() -> ReferenceSnapshot:
    """Cria um ReferenceSnapshot de teste."""
    return ReferenceSnapshot(
        snapshot_version="snap_integration_001",
        snapshot_schema_version="1.0",
        created_at="2026-03-11T10:00:00Z",
        data={"daily_limit_minor": 500000, "blocked_accounts": ()},
    )


def _build_manifest_payload(scopes: dict) -> str:
    """Constrói payload JSON de manifesto para publicação no AppConfig."""
    return json.dumps({"version": "1", "scopes": scopes}, ensure_ascii=False, sort_keys=True)


def _scope_entry(
    activation_id: str = "act_integration_001",
    artifact_hash: str = "sha256:integration_bundle_hash_001",
    snapshot_version: str = "snap_integration_001",
) -> dict:
    """Cria uma entrada de escopo para o payload do manifesto."""
    return {
        "activation_id": activation_id,
        "artifact_hash": artifact_hash,
        "snapshot_version": snapshot_version,
        "context_schema_version": "1.0",
        "evaluator_version": "1.2.0",
        "activated_at": "2026-03-11T10:00:00+00:00",
        "activated_by": "integration-test",
    }


def _make_mock_appconfigdata_client(payload: str) -> MagicMock:
    """
    Cria um mock do appconfigdata client que retorna o payload fornecido.

    Simula o fluxo do AppConfig:
    1. start_configuration_session → retorna InitialConfigurationToken
    2. get_latest_configuration → retorna o payload como streaming body

    O mock suporta múltiplas chamadas a get_latest_configuration:
    - Primeira chamada: retorna o payload completo
    - Chamadas subsequentes: retorna payload vazio (sem mudanças)
    """
    client = MagicMock()

    # start_configuration_session retorna um token inicial
    client.start_configuration_session.return_value = {
        "InitialConfigurationToken": "initial-token-001",
    }

    # get_latest_configuration retorna o payload como streaming body
    payload_bytes = payload.encode("utf-8") if payload else b""
    response = {
        "NextPollConfigurationToken": "next-token-001",
        "Configuration": BytesIO(payload_bytes),
    }
    client.get_latest_configuration.return_value = response

    return client


def _make_updatable_mock_client() -> MagicMock:
    """
    Cria um mock do appconfigdata client que suporta atualização de payload.

    Retorna o client e uma função set_payload para atualizar o payload
    retornado por get_latest_configuration.
    """
    client = MagicMock()
    client.start_configuration_session.return_value = {
        "InitialConfigurationToken": "initial-token-001",
    }

    # Estado mutável para permitir atualização do payload
    state = {"payload": b"", "call_count": 0}

    def _get_latest_config(**kwargs):
        state["call_count"] += 1
        return {
            "NextPollConfigurationToken": f"next-token-{state['call_count']:03d}",
            "Configuration": BytesIO(state["payload"]),
        }

    client.get_latest_configuration.side_effect = _get_latest_config

    def set_payload(payload: str) -> None:
        state["payload"] = payload.encode("utf-8")

    return client, set_payload


def _make_resolver_from_mock(client: MagicMock) -> ManifestResolver:
    """Cria um ManifestResolver configurado com o mock client."""
    return ManifestResolver(
        appconfig_data_client=client,
        application_id=APP_ID,
        environment_id=ENV_ID,
        configuration_profile_id=PROFILE_ID,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def moto_appconfig_setup():
    """
    Configura AppConfig mockado para testes de integração local.

    Usa mocks diretos do appconfigdata client em vez de moto, pois
    moto 5.x não suporta create_environment, create_deployment_strategy
    nem start_deployment para AppConfig.

    Yields:
        Dicionário com mock client e IDs para uso nos testes.
    """
    client, set_payload = _make_updatable_mock_client()
    yield {
        "appconfig_data_client": client,
        "set_payload": set_payload,
        "application_id": APP_ID,
        "environment_id": ENV_ID,
        "configuration_profile_id": PROFILE_ID,
    }


def _publish_manifest(setup: dict, payload: str) -> None:
    """
    Publica um manifesto no mock do AppConfig.

    Atualiza o payload retornado pelo mock client para simular
    uma nova publicação no AppConfig.

    Args:
        setup:   dicionário com mock client e set_payload.
        payload: string JSON do payload do manifesto.
    """
    setup["set_payload"](payload)


def _make_resolver(setup: dict) -> ManifestResolver:
    """Cria um ManifestResolver configurado com o setup mockado."""
    return ManifestResolver(
        appconfig_data_client=setup["appconfig_data_client"],
        application_id=setup["application_id"],
        environment_id=setup["environment_id"],
        configuration_profile_id=setup["configuration_profile_id"],
    )


# ---------------------------------------------------------------------------
# Testes de resolução com escopo único
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestManifestResolutionSingleScope:
    """Testes de resolução com escopo único."""

    def test_resolve_published_manifest(self, moto_appconfig_setup):
        """Deve resolver manifesto publicado e retornar PolicyActivationManifest."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        payload = _build_manifest_payload({scope_id: _scope_entry()})
        _publish_manifest(moto_appconfig_setup, payload)

        resolver = _make_resolver(moto_appconfig_setup)
        result = resolver.resolve(scope_id)

        assert isinstance(result, PolicyActivationManifest)
        assert result.policy_scope_id == scope_id

    def test_resolve_returns_correct_activation_id(self, moto_appconfig_setup):
        """Deve retornar activation_id correto do manifesto publicado."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        payload = _build_manifest_payload({
            scope_id: _scope_entry(activation_id="act_specific_001")
        })
        _publish_manifest(moto_appconfig_setup, payload)

        resolver = _make_resolver(moto_appconfig_setup)
        result = resolver.resolve(scope_id)

        assert result.activation_id == "act_specific_001"

    def test_resolve_returns_correct_artifact_hash(self, moto_appconfig_setup):
        """Deve retornar artifact_hash correto do manifesto publicado."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        payload = _build_manifest_payload({
            scope_id: _scope_entry(artifact_hash="sha256:specific_hash_001")
        })
        _publish_manifest(moto_appconfig_setup, payload)

        resolver = _make_resolver(moto_appconfig_setup)
        result = resolver.resolve(scope_id)

        assert result.artifact_hash == "sha256:specific_hash_001"

    def test_resolve_returns_correct_snapshot_version(self, moto_appconfig_setup):
        """Deve retornar snapshot_version correto do manifesto publicado."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        payload = _build_manifest_payload({
            scope_id: _scope_entry(snapshot_version="snap_specific_001")
        })
        _publish_manifest(moto_appconfig_setup, payload)

        resolver = _make_resolver(moto_appconfig_setup)
        result = resolver.resolve(scope_id)

        assert result.snapshot_version == "snap_specific_001"


# ---------------------------------------------------------------------------
# Testes de resolução com múltiplos escopos
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestManifestResolutionMultipleScopes:
    """Testes de resolução com múltiplos escopos no mesmo payload."""

    def test_resolve_correct_scope_from_multiple(self, moto_appconfig_setup):
        """Deve resolver o escopo correto quando há múltiplos escopos no payload."""
        payload = _build_manifest_payload({
            "tenantA:TRANSFER:PIX:*:prod": _scope_entry(
                activation_id="act_tenantA",
                artifact_hash="sha256:hash_tenantA",
            ),
            "tenantB:PAYMENT:TED:*:prod": _scope_entry(
                activation_id="act_tenantB",
                artifact_hash="sha256:hash_tenantB",
            ),
            "tenantC:REVERSAL:*:*:prod": _scope_entry(
                activation_id="act_tenantC",
                artifact_hash="sha256:hash_tenantC",
            ),
        })
        _publish_manifest(moto_appconfig_setup, payload)

        resolver = _make_resolver(moto_appconfig_setup)

        result_a = resolver.resolve("tenantA:TRANSFER:PIX:*:prod")
        result_b = resolver.resolve("tenantB:PAYMENT:TED:*:prod")
        result_c = resolver.resolve("tenantC:REVERSAL:*:*:prod")

        assert result_a.activation_id == "act_tenantA"
        assert result_b.activation_id == "act_tenantB"
        assert result_c.activation_id == "act_tenantC"

    def test_resolving_different_scopes_returns_different_manifests(self, moto_appconfig_setup):
        """Escopos diferentes devem retornar manifestos diferentes."""
        payload = _build_manifest_payload({
            "tenantA:TRANSFER:PIX:*:prod": _scope_entry(
                activation_id="act_tenantA",
                artifact_hash="sha256:hash_tenantA",
            ),
            "tenantB:PAYMENT:TED:*:prod": _scope_entry(
                activation_id="act_tenantB",
                artifact_hash="sha256:hash_tenantB",
            ),
        })
        _publish_manifest(moto_appconfig_setup, payload)

        resolver = _make_resolver(moto_appconfig_setup)

        result_a = resolver.resolve("tenantA:TRANSFER:PIX:*:prod")
        result_b = resolver.resolve("tenantB:PAYMENT:TED:*:prod")

        assert result_a.artifact_hash != result_b.artifact_hash
        assert result_a.activation_id != result_b.activation_id

    def test_missing_scope_raises_bundle_unavailable(self, moto_appconfig_setup):
        """Deve levantar PolicyBundleUnavailable para escopo não presente no payload."""
        payload = _build_manifest_payload({
            "tenantA:TRANSFER:PIX:*:prod": _scope_entry(),
        })
        _publish_manifest(moto_appconfig_setup, payload)

        resolver = _make_resolver(moto_appconfig_setup)

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            resolver.resolve("tenantZ:UNKNOWN:*:*:prod")

        assert "tenantZ:UNKNOWN:*:*:prod" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Testes de atualização de manifesto
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestManifestResolutionUpdate:
    """Testes de resolução após atualização do manifesto."""

    def test_resolve_returns_updated_manifest_after_republish(self, moto_appconfig_setup):
        """Deve retornar manifesto atualizado após nova publicação."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"

        # Publicar versão inicial
        payload_v1 = _build_manifest_payload({
            scope_id: _scope_entry(
                activation_id="act_v1",
                artifact_hash="sha256:hash_v1",
            )
        })
        _publish_manifest(moto_appconfig_setup, payload_v1)

        resolver = _make_resolver(moto_appconfig_setup)
        result_v1 = resolver.resolve(scope_id)
        assert result_v1.activation_id == "act_v1"

        # Publicar versão atualizada
        payload_v2 = _build_manifest_payload({
            scope_id: _scope_entry(
                activation_id="act_v2",
                artifact_hash="sha256:hash_v2",
            )
        })
        _publish_manifest(moto_appconfig_setup, payload_v2)

        # Invalidar sessão para forçar re-leitura
        resolver.invalidate_session()
        result_v2 = resolver.resolve(scope_id)

        assert result_v2.activation_id == "act_v2"
        assert result_v2.artifact_hash == "sha256:hash_v2"


# ---------------------------------------------------------------------------
# Testes de validação estrutural
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestManifestResolutionValidation:
    """Testes de validação estrutural do payload."""

    def test_raises_invalid_bundle_for_missing_required_field(self, moto_appconfig_setup):
        """Deve levantar InvalidPolicyBundle para campo obrigatório ausente no escopo."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        # Escopo sem artifact_hash (campo obrigatório)
        payload = _build_manifest_payload({
            scope_id: {
                "activation_id": "act_001",
                # artifact_hash ausente
                "snapshot_version": "snap_001",
                "context_schema_version": "1.0",
                "evaluator_version": "1.2.0",
            }
        })
        _publish_manifest(moto_appconfig_setup, payload)

        resolver = _make_resolver(moto_appconfig_setup)

        with pytest.raises(InvalidPolicyBundle) as exc_info:
            resolver.resolve(scope_id)

        assert "artifact_hash" in str(exc_info.value)
