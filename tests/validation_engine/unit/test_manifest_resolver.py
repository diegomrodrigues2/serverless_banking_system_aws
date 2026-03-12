"""
Testes unitários para ManifestResolver.

Cobre:
- Resolução bem-sucedida de escopo existente
- Múltiplos escopos no mesmo payload
- Validação estrutural do payload (version, scopes)
- Campos obrigatórios ausentes no escopo
- Escopo não encontrado no manifesto
- Gerenciamento de sessão AppConfig (inicialização, rotação de token)
- Comportamento com payload vazio (sem mudanças)
- Falhas de I/O com o AppConfig
- Invalidação de sessão

Requisitos cobertos: 4.3, 4.4, 5.1, 5.3
"""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, call

import botocore.exceptions
import pytest

from validation_engine.domain.errors import InvalidPolicyBundle, PolicyBundleUnavailable
from validation_engine.domain.models import PolicyActivationManifest
from validation_engine.infrastructure.manifest_resolver import ManifestResolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_error(code: str, operation: str = "StartConfigurationSession") -> botocore.exceptions.ClientError:
    """Cria um ClientError boto3 com o código especificado."""
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": f"Simulated {code}"}},
        operation,
    )


def _make_appconfig_response(payload: dict | None, next_token: str = "token_002") -> dict:
    """
    Cria uma resposta simulada do AppConfig GetLatestConfiguration.

    Args:
        payload: dicionário a serializar como payload. None = payload vazio (sem mudanças).
        next_token: próximo token de sessão.
    """
    content = json.dumps(payload).encode("utf-8") if payload is not None else b""
    return {
        "Configuration": BytesIO(content),
        "NextPollConfigurationToken": next_token,
        "ContentType": "application/json",
    }


def _make_valid_payload(scopes: dict | None = None) -> dict:
    """Cria um payload de manifesto válido com os escopos fornecidos."""
    return {
        "version": "1",
        "scopes": scopes or {
            "tenantA:TRANSFER:PIX:*:prod": {
                "activation_id": "act_20260311_abc123",
                "artifact_hash": "sha256:bundle_hash_001",
                "snapshot_version": "snap_001",
                "context_schema_version": "1.0",
                "evaluator_version": "1.2.0",
                "activated_at": "2026-03-11T10:00:00+00:00",
                "activated_by": "ci-pipeline",
            }
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def appconfig_data_mock() -> MagicMock:
    """Cliente AppConfigData mockado."""
    return MagicMock()


@pytest.fixture
def resolver(appconfig_data_mock) -> ManifestResolver:
    """ManifestResolver configurado com cliente mockado."""
    return ManifestResolver(
        appconfig_data_client=appconfig_data_mock,
        application_id="ledger-validation-engine-dev",
        environment_id="dev",
        configuration_profile_id="policy-activation-manifests",
    )


# ---------------------------------------------------------------------------
# Testes de inicialização de sessão
# ---------------------------------------------------------------------------


class TestManifestResolverSessionManagement:
    """Testes de gerenciamento de sessão AppConfig."""

    def test_starts_session_on_first_resolve(self, resolver, appconfig_data_mock):
        """Deve iniciar sessão AppConfig na primeira chamada a resolve()."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(
            _make_valid_payload()
        )

        resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        appconfig_data_mock.start_configuration_session.assert_called_once_with(
            ApplicationIdentifier="ledger-validation-engine-dev",
            EnvironmentIdentifier="dev",
            ConfigurationProfileIdentifier="policy-activation-manifests",
        )

    def test_reuses_session_on_subsequent_resolves(self, resolver, appconfig_data_mock):
        """Deve reutilizar o token de sessão em chamadas subsequentes."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(
            _make_valid_payload()
        )

        resolver.resolve("tenantA:TRANSFER:PIX:*:prod")
        resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        # Sessão deve ser iniciada apenas uma vez
        assert appconfig_data_mock.start_configuration_session.call_count == 1

    def test_invalidate_session_clears_token_and_cache(self, resolver, appconfig_data_mock):
        """Deve limpar token e cache ao invalidar sessão."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(
            _make_valid_payload()
        )

        resolver.resolve("tenantA:TRANSFER:PIX:*:prod")
        resolver.invalidate_session()

        # Após invalidação, próxima chamada deve iniciar nova sessão
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_new"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(
            _make_valid_payload()
        )

        resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert appconfig_data_mock.start_configuration_session.call_count == 2

    def test_raises_bundle_unavailable_on_session_start_failure(self, resolver, appconfig_data_mock):
        """Deve levantar PolicyBundleUnavailable se a sessão não puder ser iniciada."""
        appconfig_data_mock.start_configuration_session.side_effect = _make_client_error(
            "AccessDeniedException"
        )

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert "Falha ao iniciar sessão AppConfig" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Testes de resolução bem-sucedida
# ---------------------------------------------------------------------------


class TestManifestResolverSuccessfulResolution:
    """Testes de resolução bem-sucedida de manifestos."""

    def test_returns_policy_activation_manifest(self, resolver, appconfig_data_mock):
        """Deve retornar PolicyActivationManifest tipado para escopo existente."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(
            _make_valid_payload()
        )

        result = resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert isinstance(result, PolicyActivationManifest)

    def test_manifest_has_correct_activation_id(self, resolver, appconfig_data_mock):
        """Deve retornar manifesto com activation_id correto."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(
            _make_valid_payload()
        )

        result = resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert result.activation_id == "act_20260311_abc123"

    def test_manifest_has_correct_artifact_hash(self, resolver, appconfig_data_mock):
        """Deve retornar manifesto com artifact_hash correto."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(
            _make_valid_payload()
        )

        result = resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert result.artifact_hash == "sha256:bundle_hash_001"

    def test_manifest_has_correct_policy_scope_id(self, resolver, appconfig_data_mock):
        """Deve retornar manifesto com policy_scope_id igual ao solicitado."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(
            _make_valid_payload()
        )

        result = resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert result.policy_scope_id == "tenantA:TRANSFER:PIX:*:prod"

    def test_manifest_has_all_required_fields(self, resolver, appconfig_data_mock):
        """Deve retornar manifesto com todos os campos obrigatórios preenchidos."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(
            _make_valid_payload()
        )

        result = resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert result.activation_id
        assert result.policy_scope_id
        assert result.artifact_hash
        assert result.snapshot_version
        assert result.context_schema_version
        assert result.evaluator_version
        assert result.activated_at
        assert result.activated_by


# ---------------------------------------------------------------------------
# Testes de múltiplos escopos
# ---------------------------------------------------------------------------


class TestManifestResolverMultipleScopes:
    """Testes de resolução com múltiplos escopos no payload."""

    def test_resolves_correct_scope_from_multiple(self, resolver, appconfig_data_mock):
        """Deve resolver o escopo correto quando há múltiplos escopos no payload."""
        payload = _make_valid_payload(scopes={
            "tenantA:TRANSFER:PIX:*:prod": {
                "activation_id": "act_tenantA",
                "artifact_hash": "sha256:hash_tenantA",
                "snapshot_version": "snap_tenantA",
                "context_schema_version": "1.0",
                "evaluator_version": "1.2.0",
            },
            "tenantB:PAYMENT:TED:*:prod": {
                "activation_id": "act_tenantB",
                "artifact_hash": "sha256:hash_tenantB",
                "snapshot_version": "snap_tenantB",
                "context_schema_version": "1.0",
                "evaluator_version": "1.2.0",
            },
        })
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(payload)

        result = resolver.resolve("tenantB:PAYMENT:TED:*:prod")

        assert result.activation_id == "act_tenantB"
        assert result.artifact_hash == "sha256:hash_tenantB"

    def test_raises_bundle_unavailable_for_missing_scope(self, resolver, appconfig_data_mock):
        """Deve levantar PolicyBundleUnavailable para escopo não presente no payload."""
        payload = _make_valid_payload(scopes={
            "tenantA:TRANSFER:PIX:*:prod": {
                "activation_id": "act_tenantA",
                "artifact_hash": "sha256:hash_tenantA",
                "snapshot_version": "snap_tenantA",
                "context_schema_version": "1.0",
                "evaluator_version": "1.2.0",
            }
        })
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(payload)

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            resolver.resolve("tenantC:TRANSFER:*:*:prod")

        assert "tenantC:TRANSFER:*:*:prod" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Testes de validação estrutural do payload
# ---------------------------------------------------------------------------


class TestManifestResolverPayloadValidation:
    """Testes de validação estrutural do payload AppConfig."""

    def test_raises_invalid_bundle_for_invalid_json(self, resolver, appconfig_data_mock):
        """Deve levantar InvalidPolicyBundle para payload que não é JSON válido."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        # Retornar payload inválido (não é JSON)
        appconfig_data_mock.get_latest_configuration.return_value = {
            "Configuration": BytesIO(b"not valid json {{{"),
            "NextPollConfigurationToken": "token_002",
        }

        with pytest.raises(InvalidPolicyBundle) as exc_info:
            resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert "não é JSON válido" in str(exc_info.value)

    def test_raises_invalid_bundle_for_wrong_version(self, resolver, appconfig_data_mock):
        """Deve levantar InvalidPolicyBundle para payload com version != '1'."""
        payload = {"version": "2", "scopes": {}}
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(payload)

        with pytest.raises(InvalidPolicyBundle) as exc_info:
            resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert "Versão do manifesto AppConfig inválida" in str(exc_info.value)

    def test_raises_invalid_bundle_for_missing_scopes_field(self, resolver, appconfig_data_mock):
        """Deve levantar InvalidPolicyBundle para payload sem campo 'scopes'."""
        payload = {"version": "1"}
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(payload)

        with pytest.raises(InvalidPolicyBundle) as exc_info:
            resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert "'scopes'" in str(exc_info.value)

    def test_raises_invalid_bundle_for_scopes_not_dict(self, resolver, appconfig_data_mock):
        """Deve levantar InvalidPolicyBundle quando 'scopes' não é um dicionário."""
        payload = {"version": "1", "scopes": ["not", "a", "dict"]}
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(payload)

        with pytest.raises(InvalidPolicyBundle) as exc_info:
            resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert "'scopes'" in str(exc_info.value)

    @pytest.mark.parametrize("missing_field", [
        "activation_id",
        "artifact_hash",
        "snapshot_version",
        "context_schema_version",
        "evaluator_version",
    ])
    def test_raises_invalid_bundle_for_missing_scope_field(
        self, resolver, appconfig_data_mock, missing_field
    ):
        """Deve levantar InvalidPolicyBundle para cada campo obrigatório ausente no escopo."""
        scope_data = {
            "activation_id": "act_001",
            "artifact_hash": "sha256:hash_001",
            "snapshot_version": "snap_001",
            "context_schema_version": "1.0",
            "evaluator_version": "1.2.0",
        }
        # Remover o campo obrigatório para testar a validação
        del scope_data[missing_field]

        payload = _make_valid_payload(scopes={"tenantA:TRANSFER:PIX:*:prod": scope_data})
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(payload)

        with pytest.raises(InvalidPolicyBundle) as exc_info:
            resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert missing_field in str(exc_info.value)


# ---------------------------------------------------------------------------
# Testes de comportamento com payload vazio (sem mudanças)
# ---------------------------------------------------------------------------


class TestManifestResolverEmptyPayload:
    """Testes de comportamento quando AppConfig retorna payload vazio."""

    def test_uses_cached_payload_when_appconfig_returns_empty(self, resolver, appconfig_data_mock):
        """Deve usar payload em cache quando AppConfig retorna vazio (sem mudanças)."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        # Primeira chamada: payload completo
        # Segunda chamada: payload vazio (sem mudanças)
        appconfig_data_mock.get_latest_configuration.side_effect = [
            _make_appconfig_response(_make_valid_payload(), next_token="token_002"),
            _make_appconfig_response(None, next_token="token_003"),
        ]

        result1 = resolver.resolve("tenantA:TRANSFER:PIX:*:prod")
        result2 = resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        # Ambas as resoluções devem retornar o mesmo manifesto
        assert result1.activation_id == result2.activation_id

    def test_raises_bundle_unavailable_when_first_response_is_empty(
        self, resolver, appconfig_data_mock
    ):
        """Deve levantar PolicyBundleUnavailable quando a primeira resposta é vazia."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        # Primeira chamada retorna payload vazio — AppConfig sem configuração publicada
        appconfig_data_mock.get_latest_configuration.return_value = _make_appconfig_response(None)

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert "não retornou manifesto" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Testes de falhas de I/O
# ---------------------------------------------------------------------------


class TestManifestResolverIOFailures:
    """Testes de falhas de I/O com o AppConfig."""

    def test_raises_bundle_unavailable_on_get_configuration_failure(
        self, resolver, appconfig_data_mock
    ):
        """Deve levantar PolicyBundleUnavailable em falha de GetLatestConfiguration."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.side_effect = _make_client_error(
            "InternalServerException", "GetLatestConfiguration"
        )

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        assert "Falha ao obter configuração do AppConfig" in str(exc_info.value)

    def test_invalidates_session_on_bad_request_exception(self, resolver, appconfig_data_mock):
        """Deve invalidar sessão quando AppConfig retorna BadRequestException."""
        appconfig_data_mock.start_configuration_session.return_value = {
            "InitialConfigurationToken": "token_001"
        }
        appconfig_data_mock.get_latest_configuration.side_effect = _make_client_error(
            "BadRequestException", "GetLatestConfiguration"
        )

        with pytest.raises(PolicyBundleUnavailable):
            resolver.resolve("tenantA:TRANSFER:PIX:*:prod")

        # Sessão deve ter sido invalidada — próxima chamada deve iniciar nova sessão
        assert resolver._session_token is None
        assert resolver._last_payload is None
