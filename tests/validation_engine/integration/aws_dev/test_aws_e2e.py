"""
Teste end-to-end AWS dev — pipeline completo do Validation Engine com Ledger.

Exercita o fluxo real completo:
  1. Compile DSL → RuleBundle
  2. Store bundle e snapshot no S3 real
  3. Publish manifesto no AppConfig real
  4. Bootstrap runtime registry com recursos reais
  5. Evaluate via PolicyValidationFacade
  6. Persist DecisionSummary no DynamoDB real (via LedgerEngine)
  7. Emit DecisionTrail ao Firehose real

Verificações:
  - DecisionSummary presente no JournalEntry persistido no DynamoDB
  - DecisionTrail chega ao S3 via Firehose (com retry/wait)
  - Integridade: artifact_hash e snapshot_version corretos
  - Particionamento: trails seguem estrutura year/month/day/tenant_id/policy_scope_id

Usa recursos AWS REAIS (S3, AppConfig, DynamoDB, Firehose). NÃO usa moto.

Pré-requisitos:
    - VALIDATION_ENGINE_TEST_BUCKET: bucket S3 para bundles e snapshots
    - VALIDATION_ENGINE_TEST_APPCONFIG_APP: nome da aplicação AppConfig
    - VALIDATION_ENGINE_TEST_DYNAMODB_TABLE: tabela DynamoDB do ledger
    - VALIDATION_ENGINE_TEST_FIREHOSE_STREAM: nome do Firehose stream (opcional)
    - VALIDATION_ENGINE_TEST_TRAIL_BUCKET: bucket S3 de destino dos trails (opcional)
    - VALIDATION_ENGINE_TEST_KMS_KEY_ARN: ARN da chave KMS (opcional)
    - AWS_REGION: região AWS (padrão: us-east-1)

Estratégia de isolamento:
    O run_id (UUID único por sessão) é embutido em todos os identificadores
    para garantir que testes de sessões diferentes não colidam.

Requisitos cobertos: 2.1, 3.1, 4.3, 9.1, 12.4, 13.5, 21.1
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Generator, Mapping
from dataclasses import dataclass, field

import boto3
import pytest

from ledger.application.commands import CreateJournalEntryCommand, PostingInput
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.services import LedgerEngine
from ledger.domain.validators import (
    MinorUnitsValidator,
    TransactionLimitValidator,
    ValidationChain,
    ZeroSumValidator,
)
from ledger.infrastructure.dynamodb_repository import DynamoDBLedgerRepository
from validation_engine.application.context_builder import (
    DefaultCanonicalValidationContextBuilder,
)
from validation_engine.application.facade import PolicyValidationFacade
from validation_engine.application.runtime_registry import PolicyRuntimeRegistry
from validation_engine.domain.compiler import DSLCompiler
from validation_engine.domain.errors import PolicyRejected
from validation_engine.domain.evaluator import EVALUATOR_VERSION, RuleEvaluator
from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
    PolicyActivationManifest,
    ReferenceSnapshot,
)
from validation_engine.infrastructure.bundle_loader import BundleLoader
from validation_engine.infrastructure.bundle_store import BundleStore
from validation_engine.infrastructure.decision_trail_emitter import (
    FirehoseDecisionTrailEmitter,
    NoOpDecisionTrailEmitter,
)
from validation_engine.infrastructure.lkg_store import LKGStore
from validation_engine.infrastructure.manifest_resolver import ManifestResolver
from validation_engine.infrastructure.snapshot_loader import SnapshotLoader
from validation_engine.infrastructure.snapshot_store import SnapshotStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

CONTEXT_SCHEMA_VERSION = "1.0"
KMS_KEY_ARN_ENV_VAR = "VALIDATION_ENGINE_TEST_KMS_KEY_ARN"
FIREHOSE_STREAM_ENV_VAR = "VALIDATION_ENGINE_TEST_FIREHOSE_STREAM"
TRAIL_BUCKET_ENV_VAR = "VALIDATION_ENGINE_TEST_TRAIL_BUCKET"

# Timeout para aguardar entrega do Firehose ao S3 (segundos)
S3_DELIVERY_TIMEOUT_SECONDS = 300
S3_POLL_INTERVAL_SECONDS = 15

# DSL de teste: deny acima do limite diário, allow transações padrão
_E2E_DSL = """
POLICY deny_over_daily_limit PRIORITY 100
  WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > ref.daily_limit_minor
  THEN DENY "Transaction exceeds daily debit limit"

POLICY deny_blocked_account PRIORITY 90
  WHEN ANY(postings WHERE account_id IN ref.blocked_accounts)
  THEN DENY "Blocked account detected"

POLICY allow_standard_brl PRIORITY 10
  WHEN facts.posting_count >= 2
    AND COUNT(postings WHERE currency == "BRL") == facts.posting_count
  THEN ALLOW "Standard BRL flow"
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_required_env(var: str) -> str:
    """Lê variável de ambiente obrigatória. Pula o teste se ausente."""
    value = os.environ.get(var, "")
    if not value:
        pytest.skip(f"{var} não definido — testes E2E AWS dev ignorados")
    return value


def _make_command(
    debit_amount: int,
    external_id: str,
    tenant_id: str = "tenantA",
) -> CreateJournalEntryCommand:
    """Cria um comando balanceado (zero-sum) com postings BRL."""
    return CreateJournalEntryCommand(
        external_id=external_id,
        postings=[
            PostingInput("acc_e2e_debit", debit_amount, "BRL", "DEBIT"),
            PostingInput("acc_e2e_credit", debit_amount, "BRL", "CREDIT"),
        ],
        tenant_id=tenant_id,
        policy_context={},
        metadata={"test_type": "aws_e2e"},
    )


def _wait_for_appconfig_deployment(
    appconfig_client,
    app_id: str,
    env_id: str,
    timeout_seconds: int = 60,
    poll_interval: float = 2.0,
) -> None:
    """
    Aguarda o deployment AppConfig completar antes de tentar ler a configuração
    ou iniciar um novo deployment.

    Verifica o estado do environment: DEPLOYING indica deployment em andamento.
    ReadyForDeployment indica que está pronto para novo deployment.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        env = appconfig_client.get_environment(
            ApplicationId=app_id,
            EnvironmentId=env_id,
        )
        state = env.get("State", "")
        # ReadyForDeployment = pronto para novo deployment (nenhum ativo)
        # ROLLED_BACK = deployment anterior falhou e reverteu
        if state in ("ReadyForDeployment", "ROLLED_BACK"):
            return
        # DEPLOYING = aguardar conclusão
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Deployment AppConfig não completou em {timeout_seconds}s "
        f"(app={app_id}, env={env_id}, state={state})"
    )


def _wait_for_appconfig_deployment_started_and_complete(
    appconfig_client,
    app_id: str,
    env_id: str,
    deployment_number: int | None = None,
    timeout_seconds: int = 60,
    poll_interval: float = 1.0,
) -> None:
    """
    Aguarda que um deployment específico do AppConfig complete.

    Quando deployment_number é fornecido, usa get_deployment para rastrear
    o deployment exato — evitando race conditions onde list_deployments
    ainda mostra o deployment anterior como mais recente.

    Sem deployment_number, faz fallback para list_deployments (comportamento
    legado, menos confiável para deployments consecutivos rápidos).
    """
    deadline = time.monotonic() + timeout_seconds

    if deployment_number is not None:
        # Rastrear deployment específico via get_deployment — abordagem confiável.
        while time.monotonic() < deadline:
            response = appconfig_client.get_deployment(
                ApplicationId=app_id,
                EnvironmentId=env_id,
                DeploymentNumber=deployment_number,
            )
            state = response.get("State", "")
            if state in ("COMPLETE", "BAKING"):
                return
            if state == "ROLLED_BACK":
                raise RuntimeError(
                    f"Deployment {deployment_number} foi revertido "
                    f"(app={app_id}, env={env_id})"
                )
            time.sleep(poll_interval)
    else:
        # Fallback: verificar deployment mais recente via list_deployments.
        while time.monotonic() < deadline:
            deployments = appconfig_client.list_deployments(
                ApplicationId=app_id,
                EnvironmentId=env_id,
            )
            items = deployments.get("Items", [])
            if items:
                latest_state = items[0].get("State", "")
                if latest_state in ("COMPLETE", "BAKING", "ROLLED_BACK"):
                    return
            time.sleep(poll_interval)

    raise TimeoutError(
        f"Deployment AppConfig não completou em {timeout_seconds}s "
        f"(app={app_id}, env={env_id}, deployment_number={deployment_number})"
    )


def _wait_for_manifest_propagation(
    manifest_resolver: "ManifestResolver",
    registry: "PolicyRuntimeRegistry",
    scope_id: str,
    expected_activation_id: str,
    timeout_seconds: int = 30,
    poll_interval: float = 2.0,
) -> None:
    """
    Aguarda o data plane do AppConfig propagar o manifesto esperado.

    O control plane (appconfig) pode reportar deployment COMPLETE enquanto
    o data plane (appconfigdata) ainda serve a configuração anterior.
    Este helper faz retry com invalidação de sessão até o manifesto
    com o activation_id esperado aparecer no runtime.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        manifest_resolver.invalidate_session()
        time.sleep(poll_interval)
        registry.refresh_scope(scope_id)
        active_set = registry.get_active_policy_set(scope_id)
        if active_set.manifest.activation_id == expected_activation_id:
            return
        logger.info(
            "manifesto esperado ainda não propagado — retentando",
            extra={
                "expected_activation_id": expected_activation_id,
                "current_activation_id": active_set.manifest.activation_id,
            },
        )
    raise TimeoutError(
        f"Manifesto com activation_id='{expected_activation_id}' não propagou "
        f"no data plane em {timeout_seconds}s"
    )


def _wait_for_s3_trail(
    s3_client,
    bucket: str,
    prefix: str,
    timeout_seconds: int = S3_DELIVERY_TIMEOUT_SECONDS,
    poll_interval_seconds: int = S3_POLL_INTERVAL_SECONDS,
) -> list[str]:
    """
    Aguarda a chegada de objetos S3 sob um prefixo com retry/wait logic.

    O Firehose tem latência de entrega (buffer de 60s a 900s).
    Faz polling até encontrar objetos ou atingir o timeout.

    Returns:
        Lista de chaves S3 encontradas.

    Raises:
        TimeoutError: se nenhum objeto for encontrado dentro do timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        logger.info(
            "Verificando chegada de trail no S3",
            extra={
                "bucket": bucket,
                "prefix": prefix,
                "attempt": attempt,
                "remaining_seconds": int(deadline - time.monotonic()),
            },
        )

        response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
        objects = response.get("Contents", [])

        if objects:
            keys = [obj["Key"] for obj in objects]
            logger.info(
                "Trail encontrado no S3",
                extra={"bucket": bucket, "keys": keys, "attempt": attempt},
            )
            return keys

        time.sleep(poll_interval_seconds)

    raise TimeoutError(
        f"Trail não encontrado no S3 após {timeout_seconds}s. "
        f"Bucket: {bucket}, Prefix: {prefix}"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def e2e_run_id() -> str:
    """UUID único para isolar artefatos desta sessão E2E."""
    return uuid.uuid4().hex[:12]


@pytest.fixture(scope="module")
def e2e_aws_config(e2e_run_id: str) -> dict:
    """Configuração do ambiente AWS dev para o teste E2E."""
    bucket = _get_required_env("VALIDATION_ENGINE_TEST_BUCKET")
    appconfig_app = _get_required_env("VALIDATION_ENGINE_TEST_APPCONFIG_APP")
    dynamodb_table = _get_required_env("VALIDATION_ENGINE_TEST_DYNAMODB_TABLE")
    region = os.environ.get("AWS_REGION", "us-east-1")

    return {
        "bucket": bucket,
        "appconfig_app": appconfig_app,
        "dynamodb_table": dynamodb_table,
        "region": region,
        "run_id": e2e_run_id,
    }


@pytest.fixture(scope="module")
def e2e_aws_clients(e2e_aws_config: dict) -> dict:
    """Clientes boto3 para recursos AWS reais."""
    region = e2e_aws_config["region"]
    return {
        "s3": boto3.client("s3", region_name=region),
        "dynamodb": boto3.client("dynamodb", region_name=region),
        "appconfig": boto3.client("appconfig", region_name=region),
        "appconfig_data": boto3.client("appconfigdata", region_name=region),
        "firehose": boto3.client("firehose", region_name=region),
    }


@pytest.fixture(scope="module")
def e2e_kms_key_id() -> str:
    """ARN da chave KMS. Usa placeholder se não definido."""
    return os.environ.get(
        KMS_KEY_ARN_ENV_VAR,
        "arn:aws:kms:us-east-1:123456789012:key/test-key-placeholder",
    )


@pytest.fixture(scope="module")
def e2e_scope_id(e2e_run_id: str) -> str:
    """
    Policy scope ID para esta sessão E2E.

    O CreateJournalEntryCommand não tem operation_type como campo direto,
    então o DefaultCanonicalValidationContextBuilder retorna "UNKNOWN" para
    operation_type. O scope resultante é tenantA:UNKNOWN:*:*:prod.
    """
    return "tenantA:UNKNOWN:*:*:prod"


# ---------------------------------------------------------------------------
# Fixture: compile DSL → store bundle/snapshot → publish manifest
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def e2e_compiled_bundle(e2e_run_id: str):
    """Compila a DSL de teste em um RuleBundle."""
    compiler = DSLCompiler.create_default()
    metadata = CompilationMetadata(
        author="aws-e2e-test",
        description=f"AWS E2E test — run {e2e_run_id}",
        compiled_at="2024-01-01T00:00:00Z",
        source_hash=f"sha256:aws_e2e_{e2e_run_id}",
    )
    compatibility = BundleCompatibility(
        dsl_version="1.0",
        context_schema_version=CONTEXT_SCHEMA_VERSION,
        snapshot_schema_version="1.0",
        evaluator_min_version=EVALUATOR_VERSION,
    )
    return compiler.compile(
        dsl_source=_E2E_DSL,
        policy_set_id=f"aws-e2e-{e2e_run_id}",
        metadata=metadata,
        compatibility=compatibility,
    )


@pytest.fixture(scope="module")
def e2e_snapshot(e2e_run_id: str) -> ReferenceSnapshot:
    """Snapshot de referência com dados de teste."""
    return ReferenceSnapshot(
        snapshot_version=f"snap-e2e-{e2e_run_id}",
        snapshot_schema_version="1.0",
        created_at="2024-01-01T00:00:00Z",
        data={
            "daily_limit_minor": 500_000,  # R$ 5.000,00
            "blocked_accounts": ("blocked_acc_e2e_001",),
        },
    )


@pytest.fixture(scope="module")
def e2e_appconfig_resources(
    e2e_aws_clients: dict,
    e2e_aws_config: dict,
    e2e_run_id: str,
) -> Generator[dict, None, None]:
    """
    Cria AppConfig Environment e Configuration Profile isolados para o E2E.

    Cleanup ao final do módulo.
    """
    appconfig = e2e_aws_clients["appconfig"]
    app_name = e2e_aws_config["appconfig_app"]

    # Localizar a aplicação AppConfig pelo nome
    apps = appconfig.list_applications()
    app = next(
        (a for a in apps.get("Items", []) if a["Name"] == app_name),
        None,
    )
    if app is None:
        pytest.skip(
            f"Aplicação AppConfig '{app_name}' não encontrada. "
            f"Execute o Terraform para provisionar os recursos."
        )

    app_id = app["Id"]

    # Criar environment e profile isolados por run_id
    env = appconfig.create_environment(
        ApplicationId=app_id,
        Name=f"e2e-test-{e2e_run_id}",
    )
    profile = appconfig.create_configuration_profile(
        ApplicationId=app_id,
        Name=f"e2e-manifests-{e2e_run_id}",
        LocationUri="hosted",
    )

    yield {
        "app_id": app_id,
        "env_id": env["Id"],
        "profile_id": profile["Id"],
    }

    # Cleanup AppConfig resources
    try:
        appconfig.delete_environment(ApplicationId=app_id, EnvironmentId=env["Id"])
        # Deletar versões hosted antes de deletar o profile
        versions = appconfig.list_hosted_configuration_versions(
            ApplicationId=app_id,
            ConfigurationProfileId=profile["Id"],
        )
        for v in versions.get("Items", []):
            try:
                appconfig.delete_hosted_configuration_version(
                    ApplicationId=app_id,
                    ConfigurationProfileId=profile["Id"],
                    VersionNumber=v["VersionNumber"],
                )
            except Exception:
                pass
        appconfig.delete_configuration_profile(
            ApplicationId=app_id, ConfigurationProfileId=profile["Id"]
        )
        logger.info("cleanup AppConfig E2E concluído")
    except Exception as exc:
        logger.warning(f"falha no cleanup AppConfig E2E: {exc}")


@pytest.fixture(scope="module")
def e2e_published_manifest(
    e2e_aws_clients: dict,
    e2e_aws_config: dict,
    e2e_compiled_bundle,
    e2e_snapshot: ReferenceSnapshot,
    e2e_appconfig_resources: dict,
    e2e_scope_id: str,
    e2e_kms_key_id: str,
    e2e_run_id: str,
) -> Generator[PolicyActivationManifest, None, None]:
    """
    Armazena bundle/snapshot no S3 real, publica manifesto no AppConfig real.

    Retorna o manifesto publicado. Cleanup ao final do módulo.
    """
    s3 = e2e_aws_clients["s3"]
    appconfig = e2e_aws_clients["appconfig"]
    bucket = e2e_aws_config["bucket"]
    app_id = e2e_appconfig_resources["app_id"]
    env_id = e2e_appconfig_resources["env_id"]
    profile_id = e2e_appconfig_resources["profile_id"]

    # Store bundle e snapshot no S3 real
    bundle_store = BundleStore(s3_client=s3, bucket_name=bucket, kms_key_id=e2e_kms_key_id)
    snapshot_store = SnapshotStore(s3_client=s3, bucket_name=bucket, kms_key_id=e2e_kms_key_id)
    bundle_store.store(e2e_compiled_bundle)
    snapshot_store.store(e2e_snapshot)

    # Construir manifesto
    activation_id = f"act-e2e-{e2e_run_id}"
    manifest = PolicyActivationManifest(
        activation_id=activation_id,
        policy_scope_id=e2e_scope_id,
        artifact_hash=e2e_compiled_bundle.artifact_hash,
        snapshot_version=e2e_snapshot.snapshot_version,
        context_schema_version=CONTEXT_SCHEMA_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        activated_at="2024-01-01T00:00:00Z",
        activated_by="aws-e2e-integration-test",
    )

    # Publicar manifesto no AppConfig real
    payload = json.dumps({
        "version": "1",
        "scopes": {
            e2e_scope_id: {
                "activation_id": activation_id,
                "artifact_hash": e2e_compiled_bundle.artifact_hash,
                "snapshot_version": e2e_snapshot.snapshot_version,
                "context_schema_version": CONTEXT_SCHEMA_VERSION,
                "evaluator_version": EVALUATOR_VERSION,
                "activated_at": "2024-01-01T00:00:00Z",
                "activated_by": "aws-e2e-integration-test",
            }
        },
    })

    version_response = appconfig.create_hosted_configuration_version(
        ApplicationId=app_id,
        ConfigurationProfileId=profile_id,
        Content=payload.encode("utf-8"),
        ContentType="application/json",
    )
    version_number = version_response["VersionNumber"]

    # Deploy usando estratégia com FinalBakeTimeInMinutes=0 para testes rápidos
    # Preferir estratégia customizada do projeto (sem bake time) sobre AppConfig.AllAtOnce
    # que tem FinalBakeTimeInMinutes=10 e causaria timeout nos testes.
    strategies = appconfig.list_deployment_strategies()
    strategy = next(
        (s for s in strategies.get("Items", []) if s.get("FinalBakeTimeInMinutes", 99) == 0 and "AllAtOnce" in s["Name"]),
        None,
    ) or next(
        (s for s in strategies.get("Items", []) if s.get("FinalBakeTimeInMinutes", 99) == 0),
        None,
    )
    if strategy is None:
        strategy = appconfig.create_deployment_strategy(
            Name=f"AllAtOnce-e2e-{e2e_run_id}",
            DeploymentDurationInMinutes=0,
            GrowthFactor=100,
            FinalBakeTimeInMinutes=0,
            ReplicateTo="NONE",
        )

    deployment_response = appconfig.start_deployment(
        ApplicationId=app_id,
        EnvironmentId=env_id,
        DeploymentStrategyId=strategy["Id"],
        ConfigurationProfileId=profile_id,
        ConfigurationVersion=str(version_number),
    )
    deployment_number = deployment_response["DeploymentNumber"]

    # Aguardar deployment específico completar antes de tentar ler via appconfigdata
    _wait_for_appconfig_deployment_started_and_complete(
        appconfig, app_id, env_id, deployment_number=deployment_number,
    )

    yield manifest

    # Cleanup S3 objects
    bundle_key = f"bundles/{e2e_compiled_bundle.artifact_hash}.json"
    snapshot_key = f"snapshots/{e2e_snapshot.snapshot_version}.json"
    for key in [bundle_key, snapshot_key]:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            logger.info(f"cleanup S3 E2E: removido {key}")
        except Exception as exc:
            logger.warning(f"cleanup S3 E2E falhou para {key}: {exc}")


# ---------------------------------------------------------------------------
# Fixture: runtime registry + facade + ledger engine
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def e2e_trail_emitter(e2e_aws_clients: dict):
    """
    Trail emitter: Firehose real se configurado, NoOp caso contrário.

    Retorna uma tupla (emitter, is_firehose) para que os testes saibam
    se devem verificar a chegada do trail no S3.
    """
    stream_name = os.environ.get(FIREHOSE_STREAM_ENV_VAR, "")
    if stream_name:
        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=e2e_aws_clients["firehose"],
            delivery_stream_name=stream_name,
        )
        return emitter, True
    else:
        return NoOpDecisionTrailEmitter(), False


@pytest.fixture(scope="module")
def e2e_facade(
    e2e_aws_clients: dict,
    e2e_aws_config: dict,
    e2e_appconfig_resources: dict,
    e2e_published_manifest: PolicyActivationManifest,
    e2e_scope_id: str,
    e2e_trail_emitter,
    tmp_path_factory,
) -> PolicyValidationFacade:
    """
    PolicyValidationFacade configurada com recursos AWS reais.

    Bootstrap do runtime registry com manifesto, bundle e snapshot reais.
    """
    s3 = e2e_aws_clients["s3"]
    appconfig_data = e2e_aws_clients["appconfig_data"]
    bucket = e2e_aws_config["bucket"]
    app_id = e2e_appconfig_resources["app_id"]
    env_id = e2e_appconfig_resources["env_id"]
    profile_id = e2e_appconfig_resources["profile_id"]

    # Configurar componentes com recursos reais
    bundle_loader = BundleLoader(
        s3_client=s3,
        bucket_name=bucket,
        current_context_schema_version=CONTEXT_SCHEMA_VERSION,
        current_evaluator_version=EVALUATOR_VERSION,
    )
    snapshot_loader = SnapshotLoader(
        s3_client=s3,
        bucket_name=bucket,
        expected_snapshot_schema_version="1.0",
    )
    manifest_resolver = ManifestResolver(
        appconfig_data_client=appconfig_data,
        application_id=app_id,
        environment_id=env_id,
        configuration_profile_id=profile_id,
    )
    lkg_dir = str(tmp_path_factory.mktemp("lkg_e2e"))
    lkg_store = LKGStore(lkg_dir=lkg_dir)

    registry = PolicyRuntimeRegistry(
        manifest_resolver=manifest_resolver,
        bundle_loader=bundle_loader,
        snapshot_loader=snapshot_loader,
        lkg_store=lkg_store,
        evaluator_version=EVALUATOR_VERSION,
    )

    # Bootstrap: carregar policy ativa para o escopo E2E
    registry.refresh_scope(e2e_scope_id)

    emitter, _ = e2e_trail_emitter

    return PolicyValidationFacade(
        context_builder=DefaultCanonicalValidationContextBuilder(),
        runtime_registry=registry,
        evaluator=RuleEvaluator(),
        trail_emitter=emitter,
    )


@pytest.fixture(scope="module")
def e2e_dynamodb_repository(e2e_aws_clients: dict, e2e_aws_config: dict):
    """DynamoDBLedgerRepository apontando para a tabela real em AWS dev."""
    return DynamoDBLedgerRepository(
        dynamodb_client=e2e_aws_clients["dynamodb"],
        table_name=e2e_aws_config["dynamodb_table"],
    )


@pytest.fixture(scope="module")
def e2e_ledger_engine(
    e2e_dynamodb_repository: DynamoDBLedgerRepository,
    e2e_facade: PolicyValidationFacade,
) -> LedgerEngine:
    """
    LedgerEngine com ValidationChain completa (estruturais + policy facade).

    Usa DynamoDB real para persistência.
    """
    chain = ValidationChain(
        validators=[
            ZeroSumValidator(),
            MinorUnitsValidator(),
            TransactionLimitValidator(),
            e2e_facade,
        ]
    )
    factory = JournalEntryFactory()
    return LedgerEngine(
        repository=e2e_dynamodb_repository,
        validation_chain=chain,
        factory=factory,
    )


# ---------------------------------------------------------------------------
# Tests: Full E2E pipeline
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
class TestAWSEndToEndApproval:
    """
    Pipeline E2E completo: compile → store → publish → bootstrap → evaluate
    → persist summary no DynamoDB → emit trail ao Firehose.

    Requisitos: 2.1, 3.1, 4.3, 9.1, 12.4
    """

    def test_approved_transaction_persists_with_summary_in_dynamodb(
        self,
        e2e_ledger_engine: LedgerEngine,
        e2e_dynamodb_repository: DynamoDBLedgerRepository,
        e2e_published_manifest: PolicyActivationManifest,
        e2e_run_id: str,
    ) -> None:
        """
        Transação aprovada persiste JournalEntry com DecisionSummary no DynamoDB real.

        Verifica:
        - JournalEntry persistido no DynamoDB
        - DecisionSummary presente no metadata com campos corretos
        - artifact_hash e snapshot_version correspondem ao manifesto publicado
        """
        external_id = f"e2e-approved-{e2e_run_id}-{uuid.uuid4().hex[:8]}"
        cmd = _make_command(debit_amount=100_000, external_id=external_id)

        # Executar pipeline completo
        entry = e2e_ledger_engine.create_journal_entry(cmd)

        # Verificar persistência no DynamoDB real
        persisted = e2e_dynamodb_repository.find_journal_entry_by_id(entry.entry_id)
        assert persisted is not None, "JournalEntry não encontrado no DynamoDB"
        assert persisted.external_id == external_id

        # Verificar DecisionSummary no metadata
        assert "policy_validation" in entry.metadata, (
            "DecisionSummary ausente no metadata do JournalEntry"
        )
        pv = entry.metadata["policy_validation"]
        assert pv["final_verdict"] == "APPROVED"
        assert pv["activation_id"] == e2e_published_manifest.activation_id
        assert pv["artifact_hash"] == e2e_published_manifest.artifact_hash
        assert pv["snapshot_version"] == e2e_published_manifest.snapshot_version
        assert pv["evaluator_version"] == EVALUATOR_VERSION
        assert pv["input_hash"].startswith("sha256:")
        assert pv["matched_deny_rule"] is None
        assert isinstance(pv["evaluation_latency_ms"], float)

    def test_summary_integrity_matches_manifest(
        self,
        e2e_ledger_engine: LedgerEngine,
        e2e_published_manifest: PolicyActivationManifest,
        e2e_scope_id: str,
        e2e_run_id: str,
    ) -> None:
        """
        Integridade: artifact_hash e snapshot_version no summary correspondem
        exatamente ao manifesto publicado no AppConfig.
        """
        external_id = f"e2e-integrity-{e2e_run_id}-{uuid.uuid4().hex[:8]}"
        cmd = _make_command(debit_amount=50_000, external_id=external_id)

        entry = e2e_ledger_engine.create_journal_entry(cmd)

        pv = entry.metadata["policy_validation"]
        # Integridade: os hashes devem corresponder exatamente ao manifesto
        assert pv["artifact_hash"] == e2e_published_manifest.artifact_hash, (
            f"artifact_hash diverge: summary={pv['artifact_hash']} "
            f"vs manifest={e2e_published_manifest.artifact_hash}"
        )
        assert pv["snapshot_version"] == e2e_published_manifest.snapshot_version, (
            f"snapshot_version diverge: summary={pv['snapshot_version']} "
            f"vs manifest={e2e_published_manifest.snapshot_version}"
        )

    def test_approved_entry_has_correct_postings_in_dynamodb(
        self,
        e2e_ledger_engine: LedgerEngine,
        e2e_dynamodb_repository: DynamoDBLedgerRepository,
        e2e_run_id: str,
    ) -> None:
        """JournalEntry aprovado contém postings corretos no DynamoDB real."""
        external_id = f"e2e-postings-{e2e_run_id}-{uuid.uuid4().hex[:8]}"
        cmd = _make_command(debit_amount=200_000, external_id=external_id)

        entry = e2e_ledger_engine.create_journal_entry(cmd)

        persisted = e2e_dynamodb_repository.find_journal_entry_by_id(entry.entry_id)
        assert persisted is not None
        assert len(persisted.postings) == 2
        assert persisted.validate_zero_sum() is True


@pytest.mark.integration_aws_dev
class TestAWSEndToEndRejection:
    """
    Rejeição por policy no pipeline E2E com DynamoDB real.

    Requisitos: 9.1, 12.4
    """

    def test_policy_rejection_does_not_persist_in_dynamodb(
        self,
        e2e_ledger_engine: LedgerEngine,
        e2e_dynamodb_repository: DynamoDBLedgerRepository,
        e2e_run_id: str,
    ) -> None:
        """
        Transação acima do limite é rejeitada e NÃO persistida no DynamoDB.
        """
        external_id = f"e2e-rejected-{e2e_run_id}-{uuid.uuid4().hex[:8]}"
        cmd = _make_command(debit_amount=600_000, external_id=external_id)

        with pytest.raises(PolicyRejected) as exc_info:
            e2e_ledger_engine.create_journal_entry(cmd)

        assert exc_info.value.code == "POLICY_REJECTED"

        # Nada persistido no DynamoDB
        persisted = e2e_dynamodb_repository.find_journal_entry_by_external_id(external_id)
        assert persisted is None, (
            "JournalEntry rejeitado por policy não deveria existir no DynamoDB"
        )


@pytest.mark.integration_aws_dev
@pytest.mark.slow
class TestAWSEndToEndTrailDelivery:
    """
    Verifica que o DecisionTrail chega ao S3 via Firehose após o pipeline E2E.

    Estes testes são marcados como @pytest.mark.slow porque aguardam
    a entrega do Firehose ao S3 (latência de 60s a 5 minutos).

    Requisitos: 13.5, 21.1
    """

    def test_trail_arrives_in_s3_after_e2e_pipeline(
        self,
        e2e_ledger_engine: LedgerEngine,
        e2e_trail_emitter,
        e2e_aws_clients: dict,
        aws_dev_config,
        e2e_run_id: str,
    ) -> None:
        """
        Trail emitido durante o pipeline E2E deve chegar ao bucket S3 de destino.

        Pula se Firehose não estiver configurado.
        """
        _, is_firehose = e2e_trail_emitter
        if not is_firehose:
            pytest.skip("Firehose não configurado — trail delivery não verificável")

        trail_bucket = os.environ.get(TRAIL_BUCKET_ENV_VAR, "") or aws_dev_config.bucket

        external_id = f"e2e-trail-{e2e_run_id}-{uuid.uuid4().hex[:8]}"
        cmd = _make_command(debit_amount=100_000, external_id=external_id)

        # Executar pipeline completo (trail emitido ao Firehose)
        e2e_ledger_engine.create_journal_entry(cmd)

        # Aguardar chegada do trail ao S3
        try:
            found_keys = _wait_for_s3_trail(
                s3_client=e2e_aws_clients["s3"],
                bucket=trail_bucket,
                prefix="trails/",
                timeout_seconds=S3_DELIVERY_TIMEOUT_SECONDS,
                poll_interval_seconds=S3_POLL_INTERVAL_SECONDS,
            )
            assert len(found_keys) >= 1, "Pelo menos um trail deve existir no S3"
        except TimeoutError as exc:
            pytest.fail(
                f"Trail não chegou ao S3 dentro do timeout: {exc}\n"
                f"Verifique se o Firehose está configurado corretamente."
            )

    def test_trail_s3_key_follows_partition_structure(
        self,
        e2e_ledger_engine: LedgerEngine,
        e2e_trail_emitter,
        e2e_aws_clients: dict,
        aws_dev_config,
        e2e_run_id: str,
    ) -> None:
        """
        A chave S3 do trail deve seguir a estrutura de particionamento:
        trails/year=YYYY/month=MM/day=DD/tenant_id=X/policy_scope_id=Y/

        Requisito 21.1: particionamento para analytics via Athena.
        """
        _, is_firehose = e2e_trail_emitter
        if not is_firehose:
            pytest.skip("Firehose não configurado — particionamento não verificável")

        trail_bucket = os.environ.get(TRAIL_BUCKET_ENV_VAR, "") or aws_dev_config.bucket

        external_id = f"e2e-partition-{e2e_run_id}-{uuid.uuid4().hex[:8]}"
        cmd = _make_command(debit_amount=100_000, external_id=external_id)

        e2e_ledger_engine.create_journal_entry(cmd)

        # Construir prefixo esperado com particionamento por data e tenant
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        expected_prefix = (
            f"trails/"
            f"year={now.strftime('%Y')}/"
            f"month={now.strftime('%m')}/"
            f"day={now.strftime('%d')}/"
            f"tenant_id=tenantA/"
        )

        try:
            found_keys = _wait_for_s3_trail(
                s3_client=e2e_aws_clients["s3"],
                bucket=trail_bucket,
                prefix=expected_prefix,
                timeout_seconds=S3_DELIVERY_TIMEOUT_SECONDS,
                poll_interval_seconds=S3_POLL_INTERVAL_SECONDS,
            )
            assert len(found_keys) >= 1, (
                f"Nenhum trail encontrado com partição esperada: {expected_prefix}"
            )
            for key in found_keys:
                assert key.startswith(expected_prefix), (
                    f"Chave S3 '{key}' não segue a estrutura de partição esperada"
                )
        except TimeoutError as exc:
            pytest.fail(
                f"Trail não chegou ao S3 com partição esperada: {exc}\n"
                f"Prefixo esperado: {expected_prefix}"
            )


# ---------------------------------------------------------------------------
# Rollback test — fixtures and test class
# ---------------------------------------------------------------------------

# Rollback DSL: same rules as E2E, reused for both v1 and v2 bundles.
# The behavioral difference comes from the snapshot (daily_limit_minor).
_ROLLBACK_DSL = """
POLICY deny_over_daily_limit PRIORITY 100
  WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > ref.daily_limit_minor
  THEN DENY "Transaction exceeds daily debit limit"

POLICY allow_standard_brl PRIORITY 10
  WHEN facts.posting_count >= 2
    AND COUNT(postings WHERE currency == "BRL") == facts.posting_count
  THEN ALLOW "Standard BRL flow"
"""


@dataclass
class _RollbackPosting:
    """Posting duck-type para testes de rollback (sem dependência do ledger)."""

    account_id: str
    amount: int
    currency: str
    direction: str
    account_type: str | None = None


@dataclass
class _RollbackCommand:
    """Comando duck-type para testes de rollback via PolicyValidationFacade."""

    external_id: str = "ext_rollback_001"
    tenant_id: str = "tenantA"
    operation_type: str = "TRANSFER"
    product_code: str | None = "PIX"
    channel: str | None = None
    postings: tuple = field(default_factory=tuple)
    policy_context: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)


def _make_rollback_command(debit_amount: int, external_id: str) -> _RollbackCommand:
    """Cria comando duck-type com postings balanceados em BRL."""
    return _RollbackCommand(
        external_id=external_id,
        tenant_id="tenantA",
        operation_type="TRANSFER",
        product_code="PIX",
        postings=(
            _RollbackPosting("acc_rb_debit", debit_amount, "BRL", "DEBIT"),
            _RollbackPosting("acc_rb_credit", debit_amount, "BRL", "CREDIT"),
        ),
    )


# ---------------------------------------------------------------------------
# Rollback-specific fixtures (module scope, isolated AppConfig resources)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rb_run_id() -> str:
    """UUID único para isolar artefatos da sessão de rollback."""
    return uuid.uuid4().hex[:12]


@pytest.fixture(scope="module")
def rb_scope_id(rb_run_id: str) -> str:
    """
    Policy scope ID para a sessão de rollback.

    O _RollbackCommand tem operation_type="TRANSFER" e product_code="PIX",
    então o scope resolvido pelo context builder é tenantA:TRANSFER:PIX:*:prod.
    """
    return "tenantA:TRANSFER:PIX:*:prod"


@pytest.fixture(scope="module")
def rb_aws_config(rb_run_id: str) -> dict:
    """Configuração AWS dev para o teste de rollback."""
    bucket = _get_required_env("VALIDATION_ENGINE_TEST_BUCKET")
    appconfig_app = _get_required_env("VALIDATION_ENGINE_TEST_APPCONFIG_APP")
    region = os.environ.get("AWS_REGION", "us-east-1")
    return {
        "bucket": bucket,
        "appconfig_app": appconfig_app,
        "region": region,
        "run_id": rb_run_id,
    }


@pytest.fixture(scope="module")
def rb_aws_clients(rb_aws_config: dict) -> dict:
    """Clientes boto3 para recursos AWS reais (rollback)."""
    region = rb_aws_config["region"]
    return {
        "s3": boto3.client("s3", region_name=region),
        "appconfig": boto3.client("appconfig", region_name=region),
        "appconfig_data": boto3.client("appconfigdata", region_name=region),
    }


@pytest.fixture(scope="module")
def rb_kms_key_id() -> str:
    """ARN da chave KMS para rollback test."""
    return os.environ.get(
        KMS_KEY_ARN_ENV_VAR,
        "arn:aws:kms:us-east-1:123456789012:key/test-key-placeholder",
    )


@pytest.fixture(scope="module")
def rb_appconfig_resources(
    rb_aws_clients: dict,
    rb_aws_config: dict,
    rb_run_id: str,
) -> Generator[dict, None, None]:
    """
    Cria AppConfig Environment e Configuration Profile isolados para rollback.

    Cleanup ao final do módulo.
    """
    appconfig = rb_aws_clients["appconfig"]
    app_name = rb_aws_config["appconfig_app"]

    apps = appconfig.list_applications()
    app = next(
        (a for a in apps.get("Items", []) if a["Name"] == app_name),
        None,
    )
    if app is None:
        pytest.skip(
            f"Aplicação AppConfig '{app_name}' não encontrada. "
            f"Execute o Terraform para provisionar os recursos."
        )

    app_id = app["Id"]

    env = appconfig.create_environment(
        ApplicationId=app_id,
        Name=f"rb-test-{rb_run_id}",
    )
    profile = appconfig.create_configuration_profile(
        ApplicationId=app_id,
        Name=f"rb-manifests-{rb_run_id}",
        LocationUri="hosted",
    )

    yield {
        "app_id": app_id,
        "env_id": env["Id"],
        "profile_id": profile["Id"],
    }

    # Cleanup
    try:
        appconfig.delete_environment(ApplicationId=app_id, EnvironmentId=env["Id"])
        # Deletar versões hosted antes de deletar o profile
        versions = appconfig.list_hosted_configuration_versions(
            ApplicationId=app_id,
            ConfigurationProfileId=profile["Id"],
        )
        for v in versions.get("Items", []):
            try:
                appconfig.delete_hosted_configuration_version(
                    ApplicationId=app_id,
                    ConfigurationProfileId=profile["Id"],
                    VersionNumber=v["VersionNumber"],
                )
            except Exception:
                pass
        appconfig.delete_configuration_profile(
            ApplicationId=app_id, ConfigurationProfileId=profile["Id"]
        )
        logger.info("cleanup AppConfig rollback concluído")
    except Exception as exc:
        logger.warning(f"falha no cleanup AppConfig rollback: {exc}")


@pytest.fixture(scope="module")
def rb_compiled_bundle(rb_run_id: str):
    """Compila a DSL de rollback em um RuleBundle (usado por v1 e v2)."""
    compiler = DSLCompiler.create_default()
    metadata = CompilationMetadata(
        author="aws-rollback-test",
        description=f"AWS rollback test — run {rb_run_id}",
        compiled_at="2024-01-01T00:00:00Z",
        source_hash=f"sha256:aws_rb_{rb_run_id}",
    )
    compatibility = BundleCompatibility(
        dsl_version="1.0",
        context_schema_version=CONTEXT_SCHEMA_VERSION,
        snapshot_schema_version="1.0",
        evaluator_min_version=EVALUATOR_VERSION,
    )
    return compiler.compile(
        dsl_source=_ROLLBACK_DSL,
        policy_set_id=f"aws-rb-{rb_run_id}",
        metadata=metadata,
        compatibility=compatibility,
    )


@pytest.fixture(scope="module")
def rb_snapshot_v1(rb_run_id: str) -> ReferenceSnapshot:
    """Snapshot v1: daily_limit_minor = 500_000 (R$ 5.000,00)."""
    return ReferenceSnapshot(
        snapshot_version=f"snap-rb-v1-{rb_run_id}",
        snapshot_schema_version="1.0",
        created_at="2024-01-01T00:00:00Z",
        data={"daily_limit_minor": 500_000},
    )


@pytest.fixture(scope="module")
def rb_snapshot_v2(rb_run_id: str) -> ReferenceSnapshot:
    """Snapshot v2: daily_limit_minor = 1_000_000 (R$ 10.000,00)."""
    return ReferenceSnapshot(
        snapshot_version=f"snap-rb-v2-{rb_run_id}",
        snapshot_schema_version="1.0",
        created_at="2024-01-01T00:00:00Z",
        data={"daily_limit_minor": 1_000_000},
    )


def _publish_manifest_to_appconfig(
    appconfig_client,
    app_id: str,
    env_id: str,
    profile_id: str,
    scope_id: str,
    artifact_hash: str,
    snapshot_version: str,
    activation_id: str,
    run_id: str,
) -> PolicyActivationManifest:
    """
    Publica um manifesto no AppConfig e aguarda deployment AllAtOnce.

    Retorna o PolicyActivationManifest publicado.
    """
    manifest = PolicyActivationManifest(
        activation_id=activation_id,
        policy_scope_id=scope_id,
        artifact_hash=artifact_hash,
        snapshot_version=snapshot_version,
        context_schema_version=CONTEXT_SCHEMA_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        activated_at="2024-01-01T00:00:00Z",
        activated_by="aws-rollback-integration-test",
    )

    payload = json.dumps({
        "version": "1",
        "scopes": {
            scope_id: {
                "activation_id": activation_id,
                "artifact_hash": artifact_hash,
                "snapshot_version": snapshot_version,
                "context_schema_version": CONTEXT_SCHEMA_VERSION,
                "evaluator_version": EVALUATOR_VERSION,
                "activated_at": "2024-01-01T00:00:00Z",
                "activated_by": "aws-rollback-integration-test",
            }
        },
    })

    version_response = appconfig_client.create_hosted_configuration_version(
        ApplicationId=app_id,
        ConfigurationProfileId=profile_id,
        Content=payload.encode("utf-8"),
        ContentType="application/json",
    )
    version_number = version_response["VersionNumber"]

    # Deploy usando estratégia com FinalBakeTimeInMinutes=0 para testes rápidos
    strategies = appconfig_client.list_deployment_strategies()
    strategy = next(
        (s for s in strategies.get("Items", []) if s.get("FinalBakeTimeInMinutes", 99) == 0 and "AllAtOnce" in s["Name"]),
        None,
    ) or next(
        (s for s in strategies.get("Items", []) if s.get("FinalBakeTimeInMinutes", 99) == 0),
        None,
    )
    if strategy is None:
        strategy = appconfig_client.create_deployment_strategy(
            Name=f"AllAtOnce-rb-{run_id}",
            DeploymentDurationInMinutes=0,
            GrowthFactor=100,
            FinalBakeTimeInMinutes=0,
            ReplicateTo="NONE",
        )

    # Aguardar que qualquer deployment anterior complete antes de iniciar novo
    _wait_for_appconfig_deployment(appconfig_client, app_id, env_id)

    deployment_response = appconfig_client.start_deployment(
        ApplicationId=app_id,
        EnvironmentId=env_id,
        DeploymentStrategyId=strategy["Id"],
        ConfigurationProfileId=profile_id,
        ConfigurationVersion=str(version_number),
    )
    deployment_number = deployment_response["DeploymentNumber"]

    # Aguardar deployment específico completar antes de tentar ler via appconfigdata
    _wait_for_appconfig_deployment_started_and_complete(
        appconfig_client, app_id, env_id, deployment_number=deployment_number,
    )

    return manifest


@pytest.fixture(scope="module")
def rb_stored_artifacts(
    rb_aws_clients: dict,
    rb_aws_config: dict,
    rb_compiled_bundle,
    rb_snapshot_v1: ReferenceSnapshot,
    rb_snapshot_v2: ReferenceSnapshot,
    rb_kms_key_id: str,
) -> Generator[dict, None, None]:
    """
    Armazena bundle e ambos os snapshots (v1 e v2) no S3 real.

    Cleanup ao final do módulo.
    """
    s3 = rb_aws_clients["s3"]
    bucket = rb_aws_config["bucket"]

    bundle_store = BundleStore(s3_client=s3, bucket_name=bucket, kms_key_id=rb_kms_key_id)
    snapshot_store = SnapshotStore(s3_client=s3, bucket_name=bucket, kms_key_id=rb_kms_key_id)

    bundle_store.store(rb_compiled_bundle)
    snapshot_store.store(rb_snapshot_v1)
    snapshot_store.store(rb_snapshot_v2)

    yield {
        "bundle_hash": rb_compiled_bundle.artifact_hash,
        "snapshot_v1_version": rb_snapshot_v1.snapshot_version,
        "snapshot_v2_version": rb_snapshot_v2.snapshot_version,
    }

    # Cleanup S3 objects
    keys_to_delete = [
        f"bundles/{rb_compiled_bundle.artifact_hash}.json",
        f"snapshots/{rb_snapshot_v1.snapshot_version}.json",
        f"snapshots/{rb_snapshot_v2.snapshot_version}.json",
    ]
    for key in keys_to_delete:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            logger.info(f"cleanup S3 rollback: removido {key}")
        except Exception as exc:
            logger.warning(f"cleanup S3 rollback falhou para {key}: {exc}")


# ---------------------------------------------------------------------------
# Tests: Rollback E2E
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
class TestAWSEndToEndRollback:
    """
    Teste de rollback end-to-end: publica manifesto v2, reverte para v1,
    valida que o runtime volta ao comportamento do bundle/snapshot anterior.

    Cenário:
      - v1 snapshot: daily_limit_minor = 500_000 (R$ 5.000,00)
      - v2 snapshot: daily_limit_minor = 1_000_000 (R$ 10.000,00)
      - Valor de teste: 600_000 (R$ 6.000,00)
        - Com v1: DENIED (600k > 500k)
        - Com v2: APPROVED (600k < 1M)
        - Após rollback para v1: DENIED novamente (600k > 500k)

    Requisitos: 24.4, 24.5, 24.6
    """

    # Valor de teste: R$ 6.000,00 — acima do limite v1, abaixo do limite v2
    _TEST_AMOUNT = 600_000

    def test_rollback_restores_previous_policy_behavior(
        self,
        rb_aws_clients: dict,
        rb_aws_config: dict,
        rb_appconfig_resources: dict,
        rb_compiled_bundle,
        rb_snapshot_v1: ReferenceSnapshot,
        rb_snapshot_v2: ReferenceSnapshot,
        rb_stored_artifacts: dict,
        rb_scope_id: str,
        rb_run_id: str,
        tmp_path_factory,
    ) -> None:
        """
        Pipeline completo de rollback:
        1. Publica manifesto v1 → bootstrap → avalia 600k → DENIED
        2. Publica manifesto v2 → refresh → avalia 600k → APPROVED
        3. Rollback: publica manifesto apontando para v1 → refresh → avalia 600k → DENIED

        Valida que o runtime troca corretamente entre versões de bundle/snapshot
        e que o rollback restaura o comportamento anterior.
        """
        s3 = rb_aws_clients["s3"]
        appconfig_data = rb_aws_clients["appconfig_data"]
        appconfig = rb_aws_clients["appconfig"]
        bucket = rb_aws_config["bucket"]
        app_id = rb_appconfig_resources["app_id"]
        env_id = rb_appconfig_resources["env_id"]
        profile_id = rb_appconfig_resources["profile_id"]

        # --- Fase 1: Publicar v1 e verificar DENIED ---

        manifest_v1 = _publish_manifest_to_appconfig(
            appconfig_client=appconfig,
            app_id=app_id,
            env_id=env_id,
            profile_id=profile_id,
            scope_id=rb_scope_id,
            artifact_hash=rb_compiled_bundle.artifact_hash,
            snapshot_version=rb_snapshot_v1.snapshot_version,
            activation_id=f"act-rb-v1-{rb_run_id}",
            run_id=rb_run_id,
        )

        # Construir runtime com v1
        bundle_loader = BundleLoader(
            s3_client=s3,
            bucket_name=bucket,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )
        snapshot_loader = SnapshotLoader(
            s3_client=s3,
            bucket_name=bucket,
            expected_snapshot_schema_version="1.0",
        )
        manifest_resolver = ManifestResolver(
            appconfig_data_client=appconfig_data,
            application_id=app_id,
            environment_id=env_id,
            configuration_profile_id=profile_id,
        )
        lkg_dir = str(tmp_path_factory.mktemp("lkg_rollback"))
        lkg_store = LKGStore(lkg_dir=lkg_dir)

        registry = PolicyRuntimeRegistry(
            manifest_resolver=manifest_resolver,
            bundle_loader=bundle_loader,
            snapshot_loader=snapshot_loader,
            lkg_store=lkg_store,
            evaluator_version=EVALUATOR_VERSION,
        )
        registry.refresh_scope(rb_scope_id)

        facade = PolicyValidationFacade(
            context_builder=DefaultCanonicalValidationContextBuilder(),
            runtime_registry=registry,
            evaluator=RuleEvaluator(),
            trail_emitter=NoOpDecisionTrailEmitter(),
        )

        # v1: 600k > 500k limit → DENIED
        cmd_v1 = _make_rollback_command(
            debit_amount=self._TEST_AMOUNT,
            external_id=f"rb-v1-{rb_run_id}-{uuid.uuid4().hex[:8]}",
        )
        with pytest.raises(PolicyRejected) as exc_v1:
            facade.validate(cmd_v1)
        assert exc_v1.value.code == "POLICY_REJECTED"

        logger.info(
            "Fase 1 concluída: v1 rejeita corretamente",
            extra={"scope_id": rb_scope_id, "amount": self._TEST_AMOUNT},
        )

        # --- Fase 2: Publicar v2 e verificar APPROVED ---

        manifest_v2 = _publish_manifest_to_appconfig(
            appconfig_client=appconfig,
            app_id=app_id,
            env_id=env_id,
            profile_id=profile_id,
            scope_id=rb_scope_id,
            artifact_hash=rb_compiled_bundle.artifact_hash,
            snapshot_version=rb_snapshot_v2.snapshot_version,
            activation_id=f"act-rb-v2-{rb_run_id}",
            run_id=rb_run_id,
        )

        # Aguardar propagação do data plane AppConfig (eventual consistency).
        # O deployment pode estar COMPLETE no control plane enquanto o data plane
        # ainda serve a configuração anterior. Retry com invalidação de sessão
        # até o manifesto esperado aparecer.
        _wait_for_manifest_propagation(
            manifest_resolver=manifest_resolver,
            registry=registry,
            scope_id=rb_scope_id,
            expected_activation_id=f"act-rb-v2-{rb_run_id}",
        )

        # Verificar que o runtime agora usa v2
        active_set = registry.get_active_policy_set(rb_scope_id)
        assert active_set.manifest.snapshot_version == rb_snapshot_v2.snapshot_version, (
            f"Runtime deveria usar snapshot v2 ({rb_snapshot_v2.snapshot_version}), "
            f"mas está usando {active_set.manifest.snapshot_version}"
        )

        # v2: 600k < 1M limit → APPROVED
        cmd_v2 = _make_rollback_command(
            debit_amount=self._TEST_AMOUNT,
            external_id=f"rb-v2-{rb_run_id}-{uuid.uuid4().hex[:8]}",
        )
        result_v2 = facade.validate(cmd_v2)
        assert result_v2.is_valid, (
            "Transação de 600k deveria ser APPROVED com limite v2 de 1M"
        )

        logger.info(
            "Fase 2 concluída: v2 aprova corretamente",
            extra={"scope_id": rb_scope_id, "amount": self._TEST_AMOUNT},
        )

        # --- Fase 3: Rollback para v1 e verificar DENIED novamente ---

        _publish_manifest_to_appconfig(
            appconfig_client=appconfig,
            app_id=app_id,
            env_id=env_id,
            profile_id=profile_id,
            scope_id=rb_scope_id,
            artifact_hash=rb_compiled_bundle.artifact_hash,
            snapshot_version=rb_snapshot_v1.snapshot_version,
            activation_id=f"act-rb-rollback-{rb_run_id}",
            run_id=rb_run_id,
        )

        # Aguardar propagação do data plane AppConfig (eventual consistency).
        _wait_for_manifest_propagation(
            manifest_resolver=manifest_resolver,
            registry=registry,
            scope_id=rb_scope_id,
            expected_activation_id=f"act-rb-rollback-{rb_run_id}",
        )

        # Verificar que o runtime voltou para v1
        active_set_rollback = registry.get_active_policy_set(rb_scope_id)
        assert active_set_rollback.manifest.snapshot_version == rb_snapshot_v1.snapshot_version, (
            f"Após rollback, runtime deveria usar snapshot v1 "
            f"({rb_snapshot_v1.snapshot_version}), "
            f"mas está usando {active_set_rollback.manifest.snapshot_version}"
        )
        assert active_set_rollback.manifest.activation_id == f"act-rb-rollback-{rb_run_id}", (
            "activation_id após rollback deveria ser o novo ID de rollback"
        )

        # v1 restaurado: 600k > 500k limit → DENIED novamente
        cmd_rollback = _make_rollback_command(
            debit_amount=self._TEST_AMOUNT,
            external_id=f"rb-rollback-{rb_run_id}-{uuid.uuid4().hex[:8]}",
        )
        with pytest.raises(PolicyRejected) as exc_rollback:
            facade.validate(cmd_rollback)
        assert exc_rollback.value.code == "POLICY_REJECTED"

        logger.info(
            "Fase 3 concluída: rollback para v1 rejeita corretamente",
            extra={
                "scope_id": rb_scope_id,
                "amount": self._TEST_AMOUNT,
                "v1_snapshot": rb_snapshot_v1.snapshot_version,
                "v2_snapshot": rb_snapshot_v2.snapshot_version,
                "rollback_activation_id": active_set_rollback.manifest.activation_id,
            },
        )
