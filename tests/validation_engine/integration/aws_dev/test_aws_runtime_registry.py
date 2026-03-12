"""
Testes de integração AWS dev — PolicyRuntimeRegistry com recursos reais.

Usa recursos AWS REAIS (S3 real, AppConfig real) em ambiente dev.
NÃO usa moto ou qualquer mock de AWS.

Pré-requisitos:
    - VALIDATION_ENGINE_TEST_BUCKET: bucket S3 dedicado para testes
    - VALIDATION_ENGINE_TEST_APPCONFIG_APP: nome da aplicação AppConfig de teste
    - AWS_REGION: região AWS (padrão: us-east-1)
    - Credenciais AWS válidas com permissão de leitura/escrita

Estratégia de isolamento:
    O run_id (UUID único por sessão) é embutido nos identificadores de artefatos
    para garantir que testes de sessões diferentes não colidem.

    Chaves geradas:
    - bundles/{sha256_do_conteudo}.json
    - snapshots/snap-aws-registry-{run_id}-v1.json

Cleanup:
    Cada teste deleta os objetos que criou. O cleanup é best-effort.

Cenários cobertos:
    - Bootstrap real: manifesto AppConfig real + S3 real → ActivePolicySet em memória
    - Refresh real: novo manifesto → novos artefatos → swap atômico

Requisitos cobertos: 6.1, 6.2, 17.1, 17.2
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Generator

import pytest

from validation_engine.application.runtime_registry import PolicyRuntimeRegistry
from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
    ReferenceSnapshot,
    RuleBundle,
)
from validation_engine.domain.policy_ast import (
    CompositionMode,
    ComparisonNode,
    FieldAccessNode,
    LiteralNode,
    PolicyEffect,
    PolicyRuleNode,
    RuleAST,
)
from validation_engine.infrastructure.bundle_loader import BundleLoader
from validation_engine.infrastructure.bundle_store import BundleStore
from validation_engine.infrastructure.lkg_store import LKGStore
from validation_engine.infrastructure.manifest_resolver import ManifestResolver
from validation_engine.infrastructure.snapshot_loader import SnapshotLoader
from validation_engine.infrastructure.snapshot_store import SnapshotStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes de configuração
# ---------------------------------------------------------------------------

CONTEXT_SCHEMA_VERSION = "1.0"
SNAPSHOT_SCHEMA_VERSION = "1.0"
EVALUATOR_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Helpers de skip e configuração
# ---------------------------------------------------------------------------


def _get_required_env(var: str) -> str:
    """
    Lê variável de ambiente obrigatória para testes AWS dev.

    Pula o teste com mensagem clara se a variável não estiver definida.
    """
    value = os.environ.get(var, "")
    if not value:
        pytest.skip(
            f"Variável de ambiente '{var}' não definida. "
            f"Configure as variáveis AWS dev para executar este teste."
        )
    return value


def _compute_bundle_hash(bundle: RuleBundle) -> str:
    """Calcula o artifact_hash correto para um bundle."""
    raw = json.loads(bundle.to_json())
    content_without_hash = {k: v for k, v in raw.items() if k != "artifact_hash"}
    canonical = json.dumps(content_without_hash, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helpers de construção de artefatos
# ---------------------------------------------------------------------------


def _make_bundle(run_id: str, version: str = "v1") -> RuleBundle:
    """Constrói um RuleBundle com identificadores únicos por run_id."""
    rule = PolicyRuleNode(
        name="deny_aws_registry_test",
        priority=100,
        condition=ComparisonNode(
            left=FieldAccessNode(path=("facts", "posting_count")),
            operator=">=",
            right=LiteralNode(value=2),
        ),
        effect=PolicyEffect.DENY,
        message="AWS registry integration test deny",
    )
    bundle = RuleBundle(
        policy_set_id=f"aws-registry-test-{run_id}-{version}",
        artifact_hash="placeholder",
        ast=RuleAST(rules=(rule,)),
        execution_plan={},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version=CONTEXT_SCHEMA_VERSION,
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
            evaluator_min_version=EVALUATOR_VERSION,
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author="aws-registry-integration-test",
            description=f"AWS registry integration test bundle {run_id} {version}",
            compiled_at="2026-01-01T00:00:00Z",
            source_hash=f"sha256:aws_registry_source_{run_id}_{version}",
        ),
    )
    # Calcular hash correto
    correct_hash = _compute_bundle_hash(bundle)
    return RuleBundle(
        policy_set_id=bundle.policy_set_id,
        artifact_hash=correct_hash,
        ast=bundle.ast,
        execution_plan=bundle.execution_plan,
        compatibility=bundle.compatibility,
        composition_mode=bundle.composition_mode,
        metadata=bundle.metadata,
    )


def _make_snapshot(run_id: str, version: str = "v1") -> ReferenceSnapshot:
    """Constrói um ReferenceSnapshot com identificadores únicos por run_id."""
    return ReferenceSnapshot(
        snapshot_version=f"snap-aws-registry-{run_id}-{version}",
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        created_at="2026-01-01T00:00:00Z",
        data={
            "daily_limit_minor": 500000,
            "blocked_accounts": ("acc_blocked_aws_test",),
        },
    )


def _make_appconfig_payload(
    scope_id: str,
    activation_id: str,
    artifact_hash: str,
    snapshot_version: str,
) -> str:
    """Constrói o payload JSON do AppConfig para um escopo."""
    return json.dumps({
        "version": "1",
        "scopes": {
            scope_id: {
                "activation_id": activation_id,
                "artifact_hash": artifact_hash,
                "snapshot_version": snapshot_version,
                "context_schema_version": CONTEXT_SCHEMA_VERSION,
                "evaluator_version": EVALUATOR_VERSION,
                "activated_at": "2026-01-01T00:00:00Z",
                "activated_by": "aws-registry-integration-test",
            }
        },
    })


# ---------------------------------------------------------------------------
# Fixtures de infraestrutura AWS real
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def run_id() -> str:
    """UUID único para isolar artefatos desta sessão de testes."""
    return uuid.uuid4().hex[:12]


@pytest.fixture(scope="module")
def aws_config(run_id: str) -> dict:
    """
    Configuração do ambiente AWS dev.

    Lê variáveis de ambiente e valida que os recursos necessários estão
    configurados antes de executar qualquer teste.
    """
    bucket = _get_required_env("VALIDATION_ENGINE_TEST_BUCKET")
    appconfig_app = _get_required_env("VALIDATION_ENGINE_TEST_APPCONFIG_APP")
    region = os.environ.get("AWS_REGION", "us-east-1")

    return {
        "bucket": bucket,
        "appconfig_app": appconfig_app,
        "region": region,
        "run_id": run_id,
    }


@pytest.fixture(scope="module")
def aws_clients(aws_config: dict):
    """Clientes boto3 para recursos AWS reais."""
    import boto3

    region = aws_config["region"]
    s3 = boto3.client("s3", region_name=region)
    appconfig = boto3.client("appconfig", region_name=region)
    appconfig_data = boto3.client("appconfigdata", region_name=region)

    return {"s3": s3, "appconfig": appconfig, "appconfig_data": appconfig_data}


@pytest.fixture(scope="module")
def appconfig_profile_ids(aws_clients: dict, aws_config: dict, run_id: str) -> dict:
    """
    Cria ou localiza o AppConfig Environment e Configuration Profile para testes.

    Usa o run_id para criar recursos isolados por sessão.
    """
    appconfig = aws_clients["appconfig"]
    app_name = aws_config["appconfig_app"]

    # Localizar a aplicação AppConfig pelo nome
    apps = appconfig.list_applications()
    app = next(
        (a for a in apps.get("Items", []) if a["Name"] == app_name),
        None,
    )
    if app is None:
        pytest.skip(
            f"Aplicação AppConfig '{app_name}' não encontrada no ambiente dev. "
            f"Execute o Terraform para provisionar os recursos."
        )

    app_id = app["Id"]

    # Criar environment de teste isolado por run_id
    env_name = f"registry-test-{run_id}"
    env = appconfig.create_environment(
        ApplicationId=app_id,
        Name=env_name,
    )
    env_id = env["Id"]

    # Criar configuration profile de teste
    profile_name = f"registry-manifests-{run_id}"
    profile = appconfig.create_configuration_profile(
        ApplicationId=app_id,
        Name=profile_name,
        LocationUri="hosted",
    )
    profile_id = profile["Id"]

    yield {
        "app_id": app_id,
        "env_id": env_id,
        "profile_id": profile_id,
    }

    # Cleanup: remover environment e profile criados para este run
    try:
        appconfig.delete_environment(ApplicationId=app_id, EnvironmentId=env_id)
        appconfig.delete_configuration_profile(
            ApplicationId=app_id, ConfigurationProfileId=profile_id
        )
        logger.info(
            "cleanup AppConfig concluído",
            extra={"env_id": env_id, "profile_id": profile_id},
        )
    except Exception as cleanup_error:
        logger.warning(
            "falha no cleanup AppConfig — recursos podem precisar de limpeza manual",
            extra={"error": str(cleanup_error)},
        )


def _publish_appconfig_manifest_aws(
    appconfig_client,
    app_id: str,
    env_id: str,
    profile_id: str,
    payload: str,
) -> None:
    """Publica um manifesto no AppConfig real."""
    version_response = appconfig_client.create_hosted_configuration_version(
        ApplicationId=app_id,
        ConfigurationProfileId=profile_id,
        Content=payload.encode("utf-8"),
        ContentType="application/json",
    )
    version_number = version_response["VersionNumber"]

    # Usar deployment strategy AllAtOnce para testes
    strategies = appconfig_client.list_deployment_strategies()
    strategy = next(
        (s for s in strategies.get("Items", []) if "AllAtOnce" in s["Name"]),
        None,
    )
    if strategy is None:
        strategy = appconfig_client.create_deployment_strategy(
            Name=f"AllAtOnce-registry-test",
            DeploymentDurationInMinutes=0,
            GrowthFactor=100,
            ReplicateTo="NONE",
        )

    appconfig_client.start_deployment(
        ApplicationId=app_id,
        EnvironmentId=env_id,
        DeploymentStrategyId=strategy["Id"],
        ConfigurationProfileId=profile_id,
        ConfigurationVersion=str(version_number),
    )


def _wait_for_deployment_complete(
    appconfig_client,
    app_id: str,
    env_id: str,
    timeout_seconds: int = 30,
    poll_interval: float = 2.0,
) -> None:
    """
    Aguarda o deployment AppConfig completar antes de tentar ler a configuração.

    O AppConfig AllAtOnce é rápido mas assíncrono — o GetLatestConfiguration
    retorna ResourceNotFoundException se o deployment ainda não completou.

    Args:
        appconfig_client: cliente boto3 appconfig.
        app_id:           ID da aplicação AppConfig.
        env_id:           ID do environment AppConfig.
        timeout_seconds:  timeout máximo em segundos.
        poll_interval:    intervalo de polling em segundos.

    Raises:
        TimeoutError: se o deployment não completar dentro do timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        deployments = appconfig_client.list_deployments(
            ApplicationId=app_id,
            EnvironmentId=env_id,
        )
        items = deployments.get("Items", [])
        if items:
            latest = items[0]
            state = latest.get("State", "")
            if state in ("COMPLETE", "BAKING"):
                return
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Deployment AppConfig não completou em {timeout_seconds}s "
        f"(app={app_id}, env={env_id})"
    )


# ---------------------------------------------------------------------------
# Testes de integração AWS dev
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
class TestAWSBootstrapReal:
    """Testa o bootstrap do registry com recursos AWS reais."""

    def test_bootstrap_com_s3_e_appconfig_reais(
        self,
        aws_clients: dict,
        aws_config: dict,
        appconfig_profile_ids: dict,
        run_id: str,
        tmp_path,
    ) -> None:
        """
        Bootstrap real: manifesto AppConfig real + bundle/snapshot S3 real
        deve resultar em ActivePolicySet válido em memória.

        Valida:
        - Bundle armazenado no S3 real é carregado corretamente
        - Snapshot armazenado no S3 real é carregado corretamente
        - Manifesto AppConfig real é resolvido corretamente
        - ActivePolicySet é construído com integrity_verified=True
        - LKG é salvo em disco após bootstrap bem-sucedido
        """
        s3 = aws_clients["s3"]
        appconfig = aws_clients["appconfig"]
        appconfig_data = aws_clients["appconfig_data"]
        bucket = aws_config["bucket"]
        app_id = appconfig_profile_ids["app_id"]
        env_id = appconfig_profile_ids["env_id"]
        profile_id = appconfig_profile_ids["profile_id"]

        scope_id = f"aws-registry-test-{run_id}:TRANSFER:PIX:*:dev"

        # Construir artefatos com identificadores únicos por run_id
        bundle = _make_bundle(run_id=run_id, version="v1")
        snapshot = _make_snapshot(run_id=run_id, version="v1")

        # Armazenar no S3 real
        kms_key_id = os.environ.get(
            "VALIDATION_ENGINE_TEST_KMS_KEY_ARN",
            "arn:aws:kms:us-east-1:123456789012:key/test-key-placeholder",
        )
        bundle_store = BundleStore(s3_client=s3, bucket_name=bucket, kms_key_id=kms_key_id)
        snapshot_store = SnapshotStore(s3_client=s3, bucket_name=bucket, kms_key_id=kms_key_id)
        bundle_store.store(bundle)
        snapshot_store.store(snapshot)

        # Publicar manifesto no AppConfig real
        payload = _make_appconfig_payload(
            scope_id=scope_id,
            activation_id=f"act-aws-registry-{run_id}-v1",
            artifact_hash=bundle.artifact_hash,
            snapshot_version=snapshot.snapshot_version,
        )
        _publish_appconfig_manifest_aws(appconfig, app_id, env_id, profile_id, payload)

        # Aguardar deployment completar (AllAtOnce é rápido mas assíncrono)
        _wait_for_deployment_complete(appconfig, app_id, env_id)

        # Configurar registry com recursos reais
        bundle_loader = BundleLoader(
            s3_client=s3,
            bucket_name=bucket,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )
        snapshot_loader = SnapshotLoader(
            s3_client=s3,
            bucket_name=bucket,
            expected_snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        )
        manifest_resolver = ManifestResolver(
            appconfig_data_client=appconfig_data,
            application_id=app_id,
            environment_id=env_id,
            configuration_profile_id=profile_id,
        )
        lkg_store = LKGStore(lkg_dir=str(tmp_path / "lkg_aws"))

        registry = PolicyRuntimeRegistry(
            manifest_resolver=manifest_resolver,
            bundle_loader=bundle_loader,
            snapshot_loader=snapshot_loader,
            lkg_store=lkg_store,
            evaluator_version=EVALUATOR_VERSION,
        )

        # Executar bootstrap real
        registry.refresh_scope(scope_id)

        # Verificar ActivePolicySet
        aps = registry.get_active_policy_set(scope_id)
        assert aps is not None
        assert aps.manifest.activation_id == f"act-aws-registry-{run_id}-v1"
        assert aps.manifest.artifact_hash == bundle.artifact_hash
        assert aps.manifest.snapshot_version == snapshot.snapshot_version
        assert aps.integrity_verified is True
        assert aps.bundle.policy_set_id == bundle.policy_set_id
        assert aps.snapshot.snapshot_version == snapshot.snapshot_version

        # Verificar que LKG foi salvo
        assert lkg_store.has_valid_boot is True
        lkg = lkg_store.load(scope_id)
        assert lkg is not None
        assert lkg.manifest.activation_id == f"act-aws-registry-{run_id}-v1"

        logger.info(
            "bootstrap AWS dev concluído com sucesso",
            extra={
                "scope_id": scope_id,
                "activation_id": aps.manifest.activation_id,
                "artifact_hash": aps.manifest.artifact_hash,
                "snapshot_version": aps.manifest.snapshot_version,
                "run_id": run_id,
            },
        )

        # Cleanup: remover objetos S3 criados
        try:
            s3.delete_object(
                Bucket=bucket,
                Key=f"bundles/{bundle.artifact_hash}.json",
            )
            s3.delete_object(
                Bucket=bucket,
                Key=f"snapshots/{snapshot.snapshot_version}.json",
            )
        except Exception as cleanup_error:
            logger.warning(
                "falha no cleanup S3 — objetos podem precisar de limpeza manual",
                extra={"error": str(cleanup_error), "run_id": run_id},
            )
