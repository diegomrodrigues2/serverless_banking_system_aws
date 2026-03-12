"""
Testes de integração AWS dev para ManifestResolver e PolicyPublisher.

Usa recursos AWS reais (AppConfig) em ambiente dev.
Valida o fluxo completo de publicação e resolução de manifestos contra
a infraestrutura real provisionada pelo módulo appconfig-validation.

Cenários cobertos:
- Publicar manifesto real no AppConfig dev
- Resolver manifesto publicado via ManifestResolver
- Múltiplos escopos no mesmo payload
- Rollback: publicar manifesto anterior e verificar resolução
- Validar deployment no dev (status COMPLETE)

Variáveis de ambiente necessárias:
    VALIDATION_ENGINE_TEST_APPCONFIG_APP:     ID da AppConfig Application de teste
    VALIDATION_ENGINE_TEST_APPCONFIG_ENV:     ID do AppConfig Environment de teste
    VALIDATION_ENGINE_TEST_APPCONFIG_PROFILE: ID do AppConfig Configuration Profile de teste
    VALIDATION_ENGINE_TEST_APPCONFIG_STRATEGY: ID da estratégia de deployment de teste
    AWS_REGION:                               região AWS (padrão: us-east-1)

Uso:
    pytest tests/validation_engine/integration/aws_dev/test_aws_appconfig_manifest.py -v
    pytest -m integration_aws_dev -v

Requisitos cobertos: 4.3, 22.2
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
import pytest

from validation_engine.application.publisher import PolicyPublisher
from validation_engine.domain.errors import PolicyBundleUnavailable
from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
    PolicyActivationManifest,
    ReferenceSnapshot,
    RuleBundle,
)
from validation_engine.domain.policy_ast import CompositionMode, RuleAST
from validation_engine.infrastructure.manifest_resolver import ManifestResolver

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuração do ambiente AWS dev
# ---------------------------------------------------------------------------

AWS_REGION_DEFAULT = "us-east-1"


def _get_appconfig_config() -> dict:
    """
    Lê configuração do AppConfig de teste a partir de variáveis de ambiente.

    Ignora os testes se as variáveis necessárias não estiverem definidas.

    Returns:
        Dicionário com IDs do AppConfig de teste.
    """
    app_id = os.environ.get("VALIDATION_ENGINE_TEST_APPCONFIG_APP", "")
    env_id = os.environ.get("VALIDATION_ENGINE_TEST_APPCONFIG_ENV", "")
    profile_id = os.environ.get("VALIDATION_ENGINE_TEST_APPCONFIG_PROFILE", "")
    strategy_id = os.environ.get("VALIDATION_ENGINE_TEST_APPCONFIG_STRATEGY", "")
    region = os.environ.get("AWS_REGION", AWS_REGION_DEFAULT)

    if not all([app_id, env_id, profile_id, strategy_id]):
        pytest.skip(
            "Variáveis de ambiente do AppConfig de teste não definidas. "
            "Defina VALIDATION_ENGINE_TEST_APPCONFIG_APP, "
            "VALIDATION_ENGINE_TEST_APPCONFIG_ENV, "
            "VALIDATION_ENGINE_TEST_APPCONFIG_PROFILE e "
            "VALIDATION_ENGINE_TEST_APPCONFIG_STRATEGY para executar estes testes."
        )

    return {
        "application_id": app_id,
        "environment_id": env_id,
        "configuration_profile_id": profile_id,
        "deployment_strategy_id": strategy_id,
        "region": region,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def appconfig_config() -> dict:
    """Configuração do AppConfig de teste lida das variáveis de ambiente."""
    return _get_appconfig_config()


@pytest.fixture(scope="session")
def appconfig_client(appconfig_config) -> object:
    """Cliente AppConfig para AWS dev."""
    return boto3.client("appconfig", region_name=appconfig_config["region"])


@pytest.fixture(scope="session")
def appconfig_data_client(appconfig_config) -> object:
    """Cliente AppConfigData para AWS dev."""
    return boto3.client("appconfigdata", region_name=appconfig_config["region"])


@pytest.fixture(scope="session")
def test_run_id() -> str:
    """ID único para esta sessão de testes — isola escopos de teste."""
    return str(uuid.uuid4()).replace("-", "")[:8]


@pytest.fixture(scope="session")
def publisher(appconfig_client, appconfig_config) -> PolicyPublisher:
    """PolicyPublisher configurado para AWS dev."""
    return PolicyPublisher(
        appconfig_client=appconfig_client,
        application_id=appconfig_config["application_id"],
        environment_id=appconfig_config["environment_id"],
        configuration_profile_id=appconfig_config["configuration_profile_id"],
        deployment_strategy_id=appconfig_config["deployment_strategy_id"],
        activated_by="integration-test",
        wait_for_deployment=True,
        deployment_timeout_seconds=120,
    )


@pytest.fixture(scope="session")
def resolver(appconfig_data_client, appconfig_config) -> ManifestResolver:
    """ManifestResolver configurado para AWS dev."""
    return ManifestResolver(
        appconfig_data_client=appconfig_data_client,
        application_id=appconfig_config["application_id"],
        environment_id=appconfig_config["environment_id"],
        configuration_profile_id=appconfig_config["configuration_profile_id"],
    )


@pytest.fixture(scope="session")
def test_bundle() -> RuleBundle:
    """RuleBundle de teste para integração AWS dev."""
    return RuleBundle(
        policy_set_id="bundle_aws_dev_integration_test",
        artifact_hash="sha256:aws_dev_integration_hash_001",
        ast=RuleAST(rules=(), composition_mode=CompositionMode.DENY_OVERRIDES),
        execution_plan={},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version="1.0",
            snapshot_schema_version="1.0",
            evaluator_min_version="1.2.0",
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author="integration-test",
            description="AWS dev integration test bundle",
            compiled_at=datetime.now(tz=timezone.utc).isoformat(),
            source_hash="sha256:source_hash_aws_dev_integration",
        ),
    )


@pytest.fixture(scope="session")
def test_snapshot() -> ReferenceSnapshot:
    """ReferenceSnapshot de teste para integração AWS dev."""
    return ReferenceSnapshot(
        snapshot_version="snap_aws_dev_integration_001",
        snapshot_schema_version="1.0",
        created_at=datetime.now(tz=timezone.utc).isoformat(),
        data={"daily_limit_minor": 500000, "blocked_accounts": ()},
    )


# ---------------------------------------------------------------------------
# Testes de publicação e resolução
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
@pytest.mark.slow
class TestAWSAppConfigManifestPublishAndResolve:
    """
    Testes de publicação e resolução de manifestos no AppConfig real.

    Estes testes publicam manifestos reais no AppConfig dev e verificam
    que o ManifestResolver consegue resolvê-los corretamente.
    """

    def test_publish_manifest_succeeds(
        self, publisher, test_bundle, test_snapshot, test_run_id
    ):
        """Deve publicar manifesto no AppConfig dev sem erros."""
        scope_id = f"test-tenant-{test_run_id}:TRANSFER:PIX:*:dev"

        # Não deve levantar exceção
        manifest = publisher.publish(test_bundle, test_snapshot, scope_id)

        assert isinstance(manifest, PolicyActivationManifest)
        assert manifest.policy_scope_id == scope_id

        logger.info(
            "manifesto publicado com sucesso no AppConfig dev",
            extra={
                "activation_id": manifest.activation_id,
                "scope_id": scope_id,
            },
        )

    def test_resolve_published_manifest(
        self, publisher, resolver, test_bundle, test_snapshot, test_run_id
    ):
        """Deve resolver manifesto publicado no AppConfig dev."""
        scope_id = f"test-tenant-{test_run_id}:PAYMENT:TED:*:dev"

        # Publicar manifesto
        published = publisher.publish(test_bundle, test_snapshot, scope_id)

        # Invalidar sessão para forçar re-leitura do AppConfig
        resolver.invalidate_session()

        # Resolver manifesto publicado
        resolved = resolver.resolve(scope_id)

        assert resolved.activation_id == published.activation_id
        assert resolved.artifact_hash == published.artifact_hash
        assert resolved.snapshot_version == published.snapshot_version
        assert resolved.policy_scope_id == scope_id

    def test_resolved_manifest_has_correct_versions(
        self, publisher, resolver, test_bundle, test_snapshot, test_run_id
    ):
        """Manifesto resolvido deve ter versões corretas de contexto e evaluator."""
        scope_id = f"test-tenant-{test_run_id}:REVERSAL:*:*:dev"

        publisher.publish(test_bundle, test_snapshot, scope_id)
        resolver.invalidate_session()

        resolved = resolver.resolve(scope_id)

        assert resolved.context_schema_version == test_bundle.compatibility.context_schema_version
        assert resolved.evaluator_version == test_bundle.compatibility.evaluator_min_version

    def test_publish_multiple_scopes_and_resolve_each(
        self, publisher, resolver, test_bundle, test_snapshot, test_run_id
    ):
        """Deve publicar e resolver múltiplos escopos independentemente."""
        scope_a = f"test-tenant-a-{test_run_id}:TRANSFER:PIX:*:dev"
        scope_b = f"test-tenant-b-{test_run_id}:PAYMENT:TED:*:dev"

        # Publicar dois escopos sequencialmente
        # O segundo publish preserva o primeiro escopo no payload
        manifest_a = publisher.publish(test_bundle, test_snapshot, scope_a)

        # Para o segundo escopo, precisamos incluir o primeiro como existing_scopes
        # para que ambos coexistam no payload
        existing_scopes = {
            scope_a: {
                "activation_id": manifest_a.activation_id,
                "artifact_hash": manifest_a.artifact_hash,
                "snapshot_version": manifest_a.snapshot_version,
                "context_schema_version": manifest_a.context_schema_version,
                "evaluator_version": manifest_a.evaluator_version,
                "activated_at": manifest_a.activated_at,
                "activated_by": manifest_a.activated_by,
            }
        }
        manifest_b = publisher.publish(
            test_bundle, test_snapshot, scope_b, existing_scopes=existing_scopes
        )

        resolver.invalidate_session()

        resolved_a = resolver.resolve(scope_a)
        resolved_b = resolver.resolve(scope_b)

        assert resolved_a.activation_id == manifest_a.activation_id
        assert resolved_b.activation_id == manifest_b.activation_id

    def test_missing_scope_raises_bundle_unavailable(
        self, publisher, resolver, test_bundle, test_snapshot, test_run_id
    ):
        """Deve levantar PolicyBundleUnavailable para escopo não publicado."""
        # Publicar um escopo para garantir que há um manifesto ativo
        scope_id = f"test-tenant-{test_run_id}:TRANSFER:PIX:*:dev"
        publisher.publish(test_bundle, test_snapshot, scope_id)

        resolver.invalidate_session()

        # Tentar resolver escopo que não existe
        nonexistent_scope = f"nonexistent-tenant-{test_run_id}:UNKNOWN:*:*:dev"

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            resolver.resolve(nonexistent_scope)

        assert nonexistent_scope in str(exc_info.value)


# ---------------------------------------------------------------------------
# Testes de rollback
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
@pytest.mark.slow
class TestAWSAppConfigManifestRollback:
    """
    Testes de rollback de manifesto no AppConfig real.

    Valida que é possível reverter para uma versão anterior do manifesto
    publicando um novo manifesto com os valores anteriores.
    """

    def test_rollback_to_previous_manifest(
        self, publisher, resolver, test_bundle, test_snapshot, test_run_id
    ):
        """Deve resolver manifesto anterior após rollback."""
        scope_id = f"test-rollback-{test_run_id}:TRANSFER:PIX:*:dev"

        # Publicar versão v1
        manifest_v1 = publisher.publish(test_bundle, test_snapshot, scope_id)

        # Publicar versão v2 com snapshot diferente (simulando atualização)
        snapshot_v2 = ReferenceSnapshot(
            snapshot_version="snap_v2_rollback_test",
            snapshot_schema_version="1.0",
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            data={"daily_limit_minor": 1000000},
        )
        manifest_v2 = publisher.publish(test_bundle, snapshot_v2, scope_id)

        # Verificar que v2 está ativo
        resolver.invalidate_session()
        resolved_v2 = resolver.resolve(scope_id)
        assert resolved_v2.snapshot_version == manifest_v2.snapshot_version

        # Rollback: publicar manifesto apontando para v1
        # Rollback é implementado publicando novo manifesto com valores anteriores
        rollback_bundle = RuleBundle(
            policy_set_id=test_bundle.policy_set_id,
            artifact_hash=manifest_v1.artifact_hash,
            ast=test_bundle.ast,
            execution_plan=test_bundle.execution_plan,
            compatibility=test_bundle.compatibility,
            composition_mode=test_bundle.composition_mode,
            metadata=test_bundle.metadata,
        )
        rollback_snapshot = ReferenceSnapshot(
            snapshot_version=manifest_v1.snapshot_version,
            snapshot_schema_version=test_snapshot.snapshot_schema_version,
            created_at=test_snapshot.created_at,
            data=test_snapshot.data,
        )
        publisher.publish(rollback_bundle, rollback_snapshot, scope_id)

        # Verificar que rollback restaurou v1
        resolver.invalidate_session()
        resolved_rollback = resolver.resolve(scope_id)

        assert resolved_rollback.snapshot_version == manifest_v1.snapshot_version

        logger.info(
            "rollback de manifesto validado com sucesso no AppConfig dev",
            extra={
                "scope_id": scope_id,
                "v1_snapshot": manifest_v1.snapshot_version,
                "v2_snapshot": manifest_v2.snapshot_version,
                "rollback_snapshot": resolved_rollback.snapshot_version,
            },
        )


# ---------------------------------------------------------------------------
# Testes de validação do deployment
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
@pytest.mark.slow
class TestAWSAppConfigDeploymentValidation:
    """Testes de validação do deployment no AppConfig dev."""

    def test_deployment_completes_successfully(
        self, publisher, appconfig_client, appconfig_config, test_bundle, test_snapshot, test_run_id
    ):
        """Deployment deve completar com sucesso no AppConfig dev."""
        scope_id = f"test-deployment-{test_run_id}:TRANSFER:PIX:*:dev"

        # Publicar com wait_for_deployment=True (já configurado no publisher)
        manifest = publisher.publish(test_bundle, test_snapshot, scope_id)

        # Verificar que o deployment completou listando deployments
        deployments = appconfig_client.list_deployments(
            ApplicationId=appconfig_config["application_id"],
            EnvironmentId=appconfig_config["environment_id"],
        )

        # Deve haver pelo menos um deployment
        assert len(deployments.get("Items", [])) > 0

        # O deployment mais recente deve estar em estado final
        latest_deployment = deployments["Items"][0]
        assert latest_deployment["State"] in ("COMPLETE", "BAKING")

        logger.info(
            "deployment AppConfig dev validado",
            extra={
                "activation_id": manifest.activation_id,
                "deployment_state": latest_deployment["State"],
                "deployment_number": latest_deployment.get("DeploymentNumber"),
            },
        )
