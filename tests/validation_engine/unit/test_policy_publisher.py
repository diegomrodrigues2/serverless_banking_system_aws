"""
Testes unitários para PolicyPublisher.

Cobre:
- Geração de PolicyActivationManifest com campos corretos
- Validação de compatibilidade bundle/snapshot antes da publicação
- Construção do payload com múltiplos escopos (merge)
- Criação de versão hospedada no AppConfig
- Início de deployment
- Polling de status do deployment (sucesso, rollback, timeout)
- Falhas de I/O com o AppConfig

Requisitos cobertos: 4.1, 4.2, 4.3, 4.5, 24.3, 24.4
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call, patch

import botocore.exceptions
import pytest

from validation_engine.application.publisher import PolicyPublisher
from validation_engine.domain.errors import InvalidPolicyBundle, PolicyBundleUnavailable
from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
    PolicyActivationManifest,
    ReferenceSnapshot,
    RuleBundle,
)
from validation_engine.domain.policy_ast import CompositionMode, RuleAST


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_error(code: str, operation: str = "CreateHostedConfigurationVersion") -> botocore.exceptions.ClientError:
    """Cria um ClientError boto3 com o código especificado."""
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": f"Simulated {code}"}},
        operation,
    )


def _make_bundle(
    snapshot_schema_version: str = "1.0",
    evaluator_min_version: str = "1.2.0",
) -> RuleBundle:
    """Cria um RuleBundle de teste com os parâmetros de compatibilidade fornecidos."""
    return RuleBundle(
        policy_set_id="bundle_test_001",
        artifact_hash="sha256:bundle_hash_001",
        ast=RuleAST(rules=(), composition_mode=CompositionMode.DENY_OVERRIDES),
        execution_plan={},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version="1.0",
            snapshot_schema_version=snapshot_schema_version,
            evaluator_min_version=evaluator_min_version,
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author="test",
            description="Test bundle",
            compiled_at="2026-03-11T10:00:00Z",
            source_hash="sha256:source_hash_001",
        ),
    )


def _make_snapshot(snapshot_schema_version: str = "1.0") -> ReferenceSnapshot:
    """Cria um ReferenceSnapshot de teste com o schema version fornecido."""
    return ReferenceSnapshot(
        snapshot_version="snap_001",
        snapshot_schema_version=snapshot_schema_version,
        created_at="2026-03-11T10:00:00Z",
        data={"daily_limit_minor": 500000},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def appconfig_mock() -> MagicMock:
    """Cliente AppConfig mockado."""
    mock = MagicMock()
    mock.create_hosted_configuration_version.return_value = {"VersionNumber": 1}
    mock.start_deployment.return_value = {"DeploymentNumber": 1}
    mock.get_deployment.return_value = {"State": "COMPLETE", "PercentageComplete": 100}
    return mock


@pytest.fixture
def publisher(appconfig_mock) -> PolicyPublisher:
    """PolicyPublisher configurado com cliente mockado e wait_for_deployment=False."""
    return PolicyPublisher(
        appconfig_client=appconfig_mock,
        application_id="ledger-validation-engine-dev",
        environment_id="dev",
        configuration_profile_id="policy-activation-manifests",
        deployment_strategy_id="AppConfig.AllAtOnce",
        activated_by="test-publisher",
        wait_for_deployment=False,
    )


@pytest.fixture
def publisher_with_wait(appconfig_mock) -> PolicyPublisher:
    """PolicyPublisher configurado com wait_for_deployment=True."""
    return PolicyPublisher(
        appconfig_client=appconfig_mock,
        application_id="ledger-validation-engine-dev",
        environment_id="dev",
        configuration_profile_id="policy-activation-manifests",
        deployment_strategy_id="AppConfig.AllAtOnce",
        activated_by="test-publisher",
        wait_for_deployment=True,
        deployment_timeout_seconds=30,
    )


@pytest.fixture
def valid_bundle() -> RuleBundle:
    """RuleBundle válido para testes."""
    return _make_bundle()


@pytest.fixture
def valid_snapshot() -> ReferenceSnapshot:
    """ReferenceSnapshot válido para testes."""
    return _make_snapshot()


# ---------------------------------------------------------------------------
# Testes de compatibilidade bundle/snapshot
# ---------------------------------------------------------------------------


class TestPolicyPublisherCompatibilityValidation:
    """Testes de validação de compatibilidade antes da publicação."""

    def test_raises_invalid_bundle_when_snapshot_schema_mismatch(
        self, publisher, appconfig_mock
    ):
        """Deve levantar InvalidPolicyBundle quando snapshot_schema_version diverge."""
        bundle = _make_bundle(snapshot_schema_version="1.0")
        snapshot = _make_snapshot(snapshot_schema_version="2.0")

        with pytest.raises(InvalidPolicyBundle) as exc_info:
            publisher.publish(bundle, snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert "snapshot_schema_version" in str(exc_info.value)
        # Não deve ter chamado o AppConfig
        appconfig_mock.create_hosted_configuration_version.assert_not_called()

    def test_raises_invalid_bundle_when_evaluator_version_empty(
        self, publisher, appconfig_mock
    ):
        """Deve levantar InvalidPolicyBundle quando evaluator_min_version está vazio."""
        bundle = _make_bundle(evaluator_min_version="")
        snapshot = _make_snapshot()

        with pytest.raises(InvalidPolicyBundle) as exc_info:
            publisher.publish(bundle, snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert "evaluator_min_version" in str(exc_info.value)
        appconfig_mock.create_hosted_configuration_version.assert_not_called()

    def test_does_not_raise_when_compatible(self, publisher, valid_bundle, valid_snapshot):
        """Não deve levantar exceção quando bundle e snapshot são compatíveis."""
        # Não deve levantar exceção
        publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")


# ---------------------------------------------------------------------------
# Testes de geração do manifesto
# ---------------------------------------------------------------------------


class TestPolicyPublisherManifestGeneration:
    """Testes de geração do PolicyActivationManifest."""

    def test_returns_policy_activation_manifest(self, publisher, valid_bundle, valid_snapshot):
        """Deve retornar PolicyActivationManifest tipado."""
        result = publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert isinstance(result, PolicyActivationManifest)

    def test_manifest_has_correct_artifact_hash(self, publisher, valid_bundle, valid_snapshot):
        """Deve incluir artifact_hash do bundle no manifesto."""
        result = publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert result.artifact_hash == valid_bundle.artifact_hash

    def test_manifest_has_correct_snapshot_version(self, publisher, valid_bundle, valid_snapshot):
        """Deve incluir snapshot_version no manifesto."""
        result = publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert result.snapshot_version == valid_snapshot.snapshot_version

    def test_manifest_has_correct_policy_scope_id(self, publisher, valid_bundle, valid_snapshot):
        """Deve incluir policy_scope_id correto no manifesto."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        result = publisher.publish(valid_bundle, valid_snapshot, scope_id)

        assert result.policy_scope_id == scope_id

    def test_manifest_has_correct_context_schema_version(
        self, publisher, valid_bundle, valid_snapshot
    ):
        """Deve incluir context_schema_version do bundle no manifesto."""
        result = publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert result.context_schema_version == valid_bundle.compatibility.context_schema_version

    def test_manifest_has_correct_evaluator_version(self, publisher, valid_bundle, valid_snapshot):
        """Deve incluir evaluator_version do bundle no manifesto."""
        result = publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert result.evaluator_version == valid_bundle.compatibility.evaluator_min_version

    def test_manifest_has_unique_activation_id(self, publisher, valid_bundle, valid_snapshot):
        """Deve gerar activation_id único para cada publicação."""
        result1 = publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")
        result2 = publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert result1.activation_id != result2.activation_id

    def test_manifest_activation_id_has_expected_format(
        self, publisher, valid_bundle, valid_snapshot
    ):
        """Deve gerar activation_id no formato act_{YYYYMMDD}_{uuid_curto}."""
        result = publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert result.activation_id.startswith("act_")
        parts = result.activation_id.split("_")
        assert len(parts) == 3
        assert len(parts[1]) == 8  # YYYYMMDD

    def test_manifest_activated_by_matches_publisher_config(
        self, publisher, valid_bundle, valid_snapshot
    ):
        """Deve incluir activated_by configurado no publisher."""
        result = publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert result.activated_by == "test-publisher"


# ---------------------------------------------------------------------------
# Testes de construção do payload
# ---------------------------------------------------------------------------


class TestPolicyPublisherPayloadBuilding:
    """Testes de construção do payload JSON para o AppConfig."""

    def test_payload_contains_version_1(self, publisher, appconfig_mock, valid_bundle, valid_snapshot):
        """Payload deve conter version='1'."""
        publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        call_kwargs = appconfig_mock.create_hosted_configuration_version.call_args[1]
        payload = json.loads(call_kwargs["Content"].decode("utf-8"))

        assert payload["version"] == "1"

    def test_payload_contains_new_scope(self, publisher, appconfig_mock, valid_bundle, valid_snapshot):
        """Payload deve conter o novo escopo publicado."""
        publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        call_kwargs = appconfig_mock.create_hosted_configuration_version.call_args[1]
        payload = json.loads(call_kwargs["Content"].decode("utf-8"))

        assert "tenantA:TRANSFER:PIX:*:prod" in payload["scopes"]

    def test_payload_preserves_existing_scopes(
        self, publisher, appconfig_mock, valid_bundle, valid_snapshot
    ):
        """Payload deve preservar escopos existentes ao publicar novo escopo."""
        existing_scopes = {
            "tenantB:PAYMENT:TED:*:prod": {
                "activation_id": "act_existing",
                "artifact_hash": "sha256:existing_hash",
                "snapshot_version": "snap_existing",
                "context_schema_version": "1.0",
                "evaluator_version": "1.2.0",
            }
        }

        publisher.publish(
            valid_bundle,
            valid_snapshot,
            "tenantA:TRANSFER:PIX:*:prod",
            existing_scopes=existing_scopes,
        )

        call_kwargs = appconfig_mock.create_hosted_configuration_version.call_args[1]
        payload = json.loads(call_kwargs["Content"].decode("utf-8"))

        # Ambos os escopos devem estar presentes
        assert "tenantA:TRANSFER:PIX:*:prod" in payload["scopes"]
        assert "tenantB:PAYMENT:TED:*:prod" in payload["scopes"]

    def test_payload_overwrites_existing_scope_with_same_id(
        self, publisher, appconfig_mock, valid_bundle, valid_snapshot
    ):
        """Deve sobrescrever escopo existente com mesmo policy_scope_id."""
        existing_scopes = {
            "tenantA:TRANSFER:PIX:*:prod": {
                "activation_id": "act_old",
                "artifact_hash": "sha256:old_hash",
                "snapshot_version": "snap_old",
                "context_schema_version": "1.0",
                "evaluator_version": "1.0.0",
            }
        }

        publisher.publish(
            valid_bundle,
            valid_snapshot,
            "tenantA:TRANSFER:PIX:*:prod",
            existing_scopes=existing_scopes,
        )

        call_kwargs = appconfig_mock.create_hosted_configuration_version.call_args[1]
        payload = json.loads(call_kwargs["Content"].decode("utf-8"))

        # O escopo deve ter sido atualizado com o novo artifact_hash
        scope = payload["scopes"]["tenantA:TRANSFER:PIX:*:prod"]
        assert scope["artifact_hash"] == valid_bundle.artifact_hash


# ---------------------------------------------------------------------------
# Testes de interação com AppConfig
# ---------------------------------------------------------------------------


class TestPolicyPublisherAppConfigInteraction:
    """Testes de interação com a API do AppConfig."""

    def test_calls_create_hosted_configuration_version(
        self, publisher, appconfig_mock, valid_bundle, valid_snapshot
    ):
        """Deve chamar CreateHostedConfigurationVersion com parâmetros corretos."""
        publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        appconfig_mock.create_hosted_configuration_version.assert_called_once()
        call_kwargs = appconfig_mock.create_hosted_configuration_version.call_args[1]

        assert call_kwargs["ApplicationId"] == "ledger-validation-engine-dev"
        assert call_kwargs["ConfigurationProfileId"] == "policy-activation-manifests"
        assert call_kwargs["ContentType"] == "application/json"

    def test_calls_start_deployment_with_correct_params(
        self, publisher, appconfig_mock, valid_bundle, valid_snapshot
    ):
        """Deve chamar StartDeployment com parâmetros corretos."""
        publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        appconfig_mock.start_deployment.assert_called_once_with(
            ApplicationId="ledger-validation-engine-dev",
            EnvironmentId="dev",
            DeploymentStrategyId="AppConfig.AllAtOnce",
            ConfigurationProfileId="policy-activation-manifests",
            ConfigurationVersion="1",
        )

    def test_raises_bundle_unavailable_on_create_version_failure(
        self, publisher, appconfig_mock, valid_bundle, valid_snapshot
    ):
        """Deve levantar PolicyBundleUnavailable em falha de CreateHostedConfigurationVersion."""
        appconfig_mock.create_hosted_configuration_version.side_effect = _make_client_error(
            "InternalServerException"
        )

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert "Falha ao criar versão hospedada" in str(exc_info.value)

    def test_raises_bundle_unavailable_on_start_deployment_failure(
        self, publisher, appconfig_mock, valid_bundle, valid_snapshot
    ):
        """Deve levantar PolicyBundleUnavailable em falha de StartDeployment."""
        appconfig_mock.start_deployment.side_effect = _make_client_error(
            "InternalServerException", "StartDeployment"
        )

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert "Falha ao iniciar deployment" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Testes de polling de deployment
# ---------------------------------------------------------------------------


class TestPolicyPublisherDeploymentPolling:
    """Testes de polling de status do deployment."""

    def test_waits_for_deployment_when_configured(
        self, publisher_with_wait, appconfig_mock, valid_bundle, valid_snapshot
    ):
        """Deve fazer polling do deployment quando wait_for_deployment=True."""
        publisher_with_wait.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        appconfig_mock.get_deployment.assert_called_once()

    def test_does_not_poll_when_wait_disabled(
        self, publisher, appconfig_mock, valid_bundle, valid_snapshot
    ):
        """Não deve fazer polling quando wait_for_deployment=False."""
        publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        appconfig_mock.get_deployment.assert_not_called()

    def test_raises_bundle_unavailable_when_deployment_rolled_back(
        self, publisher_with_wait, appconfig_mock, valid_bundle, valid_snapshot
    ):
        """Deve levantar PolicyBundleUnavailable quando deployment é revertido."""
        appconfig_mock.get_deployment.return_value = {
            "State": "ROLLED_BACK",
            "PercentageComplete": 0,
        }

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            publisher_with_wait.publish(
                valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod"
            )

        assert "revertido" in str(exc_info.value)

    def test_raises_bundle_unavailable_on_deployment_timeout(
        self, appconfig_mock, valid_bundle, valid_snapshot
    ):
        """Deve levantar PolicyBundleUnavailable quando timeout de deployment é atingido."""
        # Publisher com timeout muito curto para forçar timeout
        publisher = PolicyPublisher(
            appconfig_client=appconfig_mock,
            application_id="ledger-validation-engine-dev",
            environment_id="dev",
            configuration_profile_id="policy-activation-manifests",
            deployment_strategy_id="AppConfig.AllAtOnce",
            wait_for_deployment=True,
            deployment_timeout_seconds=0,  # timeout imediato
        )
        # Deployment nunca completa
        appconfig_mock.get_deployment.return_value = {
            "State": "DEPLOYING",
            "PercentageComplete": 50,
        }

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            publisher.publish(valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod")

        assert "Timeout" in str(exc_info.value)

    def test_succeeds_when_deployment_reaches_baking_state(
        self, publisher_with_wait, appconfig_mock, valid_bundle, valid_snapshot
    ):
        """Deve considerar deployment bem-sucedido quando atinge estado BAKING."""
        appconfig_mock.get_deployment.return_value = {
            "State": "BAKING",
            "PercentageComplete": 100,
        }

        # Não deve levantar exceção
        result = publisher_with_wait.publish(
            valid_bundle, valid_snapshot, "tenantA:TRANSFER:PIX:*:prod"
        )

        assert isinstance(result, PolicyActivationManifest)
