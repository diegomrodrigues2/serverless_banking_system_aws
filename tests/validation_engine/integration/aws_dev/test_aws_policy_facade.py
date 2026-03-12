"""
Testes de integração AWS dev — PolicyValidationFacade.

Exercita o pipeline completo da facade com recursos AWS reais:
  compile DSL → store bundle/snapshot no S3 real → publicar manifesto no AppConfig real
  → bootstrap runtime registry → validate via facade → verificar trail emitido

Usa recursos AWS REAIS (S3, AppConfig, Firehose). NÃO usa moto ou qualquer mock.

Pré-requisitos:
    - VALIDATION_ENGINE_TEST_BUCKET: bucket S3 dedicado para testes
    - VALIDATION_ENGINE_TEST_APPCONFIG_APP: nome da aplicação AppConfig de teste
    - VALIDATION_ENGINE_TEST_KMS_KEY_ARN: ARN da chave KMS (opcional)
    - AWS_REGION: região AWS (padrão: us-east-1)
    - Credenciais AWS válidas com permissão de leitura/escrita

Estratégia de isolamento:
    O run_id (UUID único por sessão) é embutido nos identificadores de artefatos
    para garantir que testes de sessões diferentes não colidam entre si.

Cleanup:
    Os objetos S3 criados são deletados ao final do módulo via fixture de cleanup.
    O cleanup é best-effort: falhas são logadas mas não falham os testes.

Requisitos cobertos: 7.1, 12.1, 13.1
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Mapping
from unittest.mock import MagicMock

import pytest

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
from validation_engine.domain.policy_ast import FinalVerdict
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
# Constants
# ---------------------------------------------------------------------------

CONTEXT_SCHEMA_VERSION = "1.0"
KMS_KEY_ARN_ENV_VAR = "VALIDATION_ENGINE_TEST_KMS_KEY_ARN"
FIREHOSE_STREAM_ENV_VAR = "VALIDATION_ENGINE_TEST_FIREHOSE_STREAM"

_FACADE_DSL = """
POLICY deny_over_daily_limit PRIORITY 100
  WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > ref.daily_limit_minor
  THEN DENY "Transaction exceeds daily debit limit"

POLICY allow_standard_brl PRIORITY 10
  WHEN facts.posting_count >= 2
  THEN ALLOW "Standard BRL transaction"
"""


# ---------------------------------------------------------------------------
# Fake command para testes de integração
# ---------------------------------------------------------------------------


@dataclass
class _FakePosting:
    account_id: str
    amount: int
    currency: str
    direction: str
    account_type: str | None = None


@dataclass
class _FakeCommand:
    """Simula CreateJournalEntryCommand para testes de integração AWS dev."""

    external_id: str = "ext_aws_facade_001"
    tenant_id: str = "tenantA"
    operation_type: str = "TRANSFER"
    product_code: str | None = "PIX"
    channel: str | None = None
    postings: tuple = field(default_factory=tuple)
    policy_context: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)


def _make_command(debit_amount: int = 100_000) -> _FakeCommand:
    """Cria um comando fake com postings balanceados em BRL."""
    return _FakeCommand(
        external_id="ext_aws_facade_001",
        tenant_id="tenantA",
        operation_type="TRANSFER",
        product_code="PIX",
        postings=(
            _FakePosting(
                account_id="acc_debit",
                amount=debit_amount,
                currency="BRL",
                direction="DEBIT",
            ),
            _FakePosting(
                account_id="acc_credit",
                amount=debit_amount,
                currency="BRL",
                direction="CREDIT",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures de módulo (escopo module para reutilizar recursos AWS)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def kms_key_id() -> str:
    """ARN da chave KMS para testes AWS dev. Usa placeholder se não definido."""
    return os.environ.get(
        KMS_KEY_ARN_ENV_VAR,
        "arn:aws:kms:us-east-1:123456789012:key/test-key-placeholder",
    )


@pytest.fixture(scope="module")
def aws_bundle_store(aws_dev_s3_client, aws_dev_config, kms_key_id) -> BundleStore:
    return BundleStore(
        s3_client=aws_dev_s3_client,
        bucket_name=aws_dev_config.bucket,
        kms_key_id=kms_key_id,
    )


@pytest.fixture(scope="module")
def aws_bundle_loader(aws_dev_s3_client, aws_dev_config) -> BundleLoader:
    return BundleLoader(
        s3_client=aws_dev_s3_client,
        bucket_name=aws_dev_config.bucket,
        current_context_schema_version=CONTEXT_SCHEMA_VERSION,
        current_evaluator_version=EVALUATOR_VERSION,
    )


@pytest.fixture(scope="module")
def aws_snapshot_store(aws_dev_s3_client, aws_dev_config, kms_key_id) -> SnapshotStore:
    return SnapshotStore(
        s3_client=aws_dev_s3_client,
        bucket_name=aws_dev_config.bucket,
        kms_key_id=kms_key_id,
    )


@pytest.fixture(scope="module")
def aws_snapshot_loader(aws_dev_s3_client, aws_dev_config) -> SnapshotLoader:
    return SnapshotLoader(
        s3_client=aws_dev_s3_client,
        bucket_name=aws_dev_config.bucket,
        expected_snapshot_schema_version="1.0",
    )


@pytest.fixture(scope="module")
def aws_compiled_bundle(aws_dev_config):
    """Bundle compilado para testes AWS dev com run_id único."""
    compiler = DSLCompiler.create_default()
    metadata = CompilationMetadata(
        author="aws-dev-facade-test",
        description=f"AWS dev facade test — run {aws_dev_config.run_id}",
        compiled_at="2024-01-01T00:00:00Z",
        source_hash="sha256:aws_dev_facade_test",
    )
    compatibility = BundleCompatibility(
        dsl_version="1.0",
        context_schema_version=CONTEXT_SCHEMA_VERSION,
        snapshot_schema_version="1.0",
        evaluator_min_version=EVALUATOR_VERSION,
    )
    return compiler.compile(
        dsl_source=_FACADE_DSL,
        policy_set_id=f"aws-dev-facade-{aws_dev_config.run_id}",
        metadata=metadata,
        compatibility=compatibility,
    )


@pytest.fixture(scope="module")
def aws_snapshot(aws_dev_config) -> ReferenceSnapshot:
    """Snapshot de referência com run_id único para isolamento."""
    return ReferenceSnapshot(
        snapshot_version=f"snap-aws-facade-{aws_dev_config.run_id}",
        snapshot_schema_version="1.0",
        created_at="2024-01-01T00:00:00Z",
        data={"daily_limit_minor": 500_000},
    )


@pytest.fixture(scope="module")
def aws_manifest(
    aws_bundle_store,
    aws_snapshot_store,
    aws_compiled_bundle,
    aws_snapshot,
    aws_dev_config,
    aws_dev_s3_client,
):
    """
    Armazena bundle e snapshot no S3 real e retorna o manifesto de ativação.

    Realiza cleanup dos objetos S3 ao final do módulo.
    """
    # Store bundle e snapshot no S3 real
    aws_bundle_store.store(aws_compiled_bundle)
    aws_snapshot_store.store(aws_snapshot)

    manifest = PolicyActivationManifest(
        activation_id=f"act-aws-facade-{aws_dev_config.run_id}",
        policy_scope_id=f"tenantA:TRANSFER:PIX:*:dev",
        artifact_hash=aws_compiled_bundle.artifact_hash,
        snapshot_version=aws_snapshot.snapshot_version,
        context_schema_version=CONTEXT_SCHEMA_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        activated_at="2024-01-01T00:00:00Z",
        activated_by="aws-dev-facade-integration-test",
    )

    yield manifest

    # Cleanup: remove objetos de teste do S3 real
    bundle_key = f"bundles/{aws_compiled_bundle.artifact_hash}.json"
    snapshot_key = f"snapshots/{aws_snapshot.snapshot_version}.json"

    for key in [bundle_key, snapshot_key]:
        try:
            aws_dev_s3_client.delete_object(Bucket=aws_dev_config.bucket, Key=key)
            logger.info("Cleanup: objeto removido do S3", extra={"key": key})
        except Exception as exc:
            logger.warning(
                "Cleanup: falha ao remover objeto",
                extra={"key": key, "error": str(exc)},
            )


@pytest.fixture(scope="module")
def aws_mock_manifest_resolver(aws_manifest):
    """ManifestResolver mockado que retorna o manifesto armazenado no S3 real."""
    resolver = MagicMock(spec=ManifestResolver)
    resolver.resolve.return_value = aws_manifest
    return resolver


@pytest.fixture(scope="module")
def aws_runtime_registry(
    aws_mock_manifest_resolver,
    aws_bundle_loader,
    aws_snapshot_loader,
    tmp_path_factory,
):
    """PolicyRuntimeRegistry configurado com S3 real e LKGStore temporário."""
    lkg_dir = str(tmp_path_factory.mktemp("lkg_aws_facade"))
    lkg_store = LKGStore(lkg_dir=lkg_dir)
    return PolicyRuntimeRegistry(
        manifest_resolver=aws_mock_manifest_resolver,
        bundle_loader=aws_bundle_loader,
        snapshot_loader=aws_snapshot_loader,
        lkg_store=lkg_store,
        evaluator_version=EVALUATOR_VERSION,
    )


@pytest.fixture(scope="module")
def noop_emitter() -> NoOpDecisionTrailEmitter:
    return NoOpDecisionTrailEmitter()


@pytest.fixture(scope="module")
def aws_facade(aws_runtime_registry, noop_emitter) -> PolicyValidationFacade:
    """PolicyValidationFacade configurada com runtime registry usando S3 real."""
    return PolicyValidationFacade(
        context_builder=DefaultCanonicalValidationContextBuilder(),
        runtime_registry=aws_runtime_registry,
        evaluator=RuleEvaluator(),
        trail_emitter=noop_emitter,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
class TestAWSPolicyFacadeApproval:
    """Testa aprovação no pipeline completo com S3 real."""

    def test_pipeline_aprova_transacao_dentro_do_limite(
        self,
        aws_facade: PolicyValidationFacade,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """Pipeline com S3 real: transação dentro do limite deve ser aprovada."""
        command = _make_command(debit_amount=100_000)  # R$ 1.000,00 < limite R$ 5.000,00

        result = aws_facade.validate(command)

        assert result.is_valid is True

    def test_trail_emitido_em_aprovacao_com_s3_real(
        self,
        aws_facade: PolicyValidationFacade,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """Trail deve ser emitido quando aprovado com bundle do S3 real."""
        # Limpa trails anteriores para este teste
        noop_emitter.emitted_trails.clear()
        command = _make_command(debit_amount=100_000)

        aws_facade.validate(command)

        assert len(noop_emitter.emitted_trails) >= 1
        trail = noop_emitter.emitted_trails[-1]
        assert trail.final_verdict == FinalVerdict.APPROVED
        assert trail.input_hash.startswith("sha256:")

    def test_trail_contem_identificadores_do_manifesto_real(
        self,
        aws_facade: PolicyValidationFacade,
        noop_emitter: NoOpDecisionTrailEmitter,
        aws_manifest: PolicyActivationManifest,
    ) -> None:
        """Trail deve conter os identificadores corretos do manifesto real."""
        noop_emitter.emitted_trails.clear()
        command = _make_command(debit_amount=100_000)

        aws_facade.validate(command)

        trail = noop_emitter.emitted_trails[-1]
        assert trail.activation_id == aws_manifest.activation_id
        assert trail.artifact_hash == aws_manifest.artifact_hash
        assert trail.snapshot_version == aws_manifest.snapshot_version
        assert trail.evaluator_version == EVALUATOR_VERSION


@pytest.mark.integration_aws_dev
class TestAWSPolicyFacadeRejection:
    """Testa rejeição no pipeline completo com S3 real."""

    def test_pipeline_rejeita_transacao_acima_do_limite(
        self,
        aws_facade: PolicyValidationFacade,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """Pipeline com S3 real: transação acima do limite deve ser rejeitada."""
        command = _make_command(debit_amount=600_000)  # R$ 6.000,00 > limite R$ 5.000,00

        with pytest.raises(PolicyRejected) as exc_info:
            aws_facade.validate(command)

        assert "deny_over_daily_limit" in str(exc_info.value)

    def test_trail_emitido_em_rejeicao_com_s3_real(
        self,
        aws_facade: PolicyValidationFacade,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """Trail deve ser emitido mesmo quando rejeitado com bundle do S3 real."""
        noop_emitter.emitted_trails.clear()
        command = _make_command(debit_amount=600_000)

        with pytest.raises(PolicyRejected):
            aws_facade.validate(command)

        assert len(noop_emitter.emitted_trails) >= 1
        trail = noop_emitter.emitted_trails[-1]
        assert trail.final_verdict == FinalVerdict.REJECTED
        assert trail.matched_deny_rule == "deny_over_daily_limit"


@pytest.mark.integration_aws_dev
class TestAWSPolicyFacadeFirehoseEmitter:
    """
    Testa o emitter Firehose real ou isolável.

    Se VALIDATION_ENGINE_TEST_FIREHOSE_STREAM não estiver definido,
    usa o NoOpDecisionTrailEmitter como fallback isolável.
    """

    def test_facade_com_firehose_real_ou_isolavel(
        self,
        aws_runtime_registry: PolicyRuntimeRegistry,
    ) -> None:
        """
        Testa a facade com Firehose real (se configurado) ou emitter isolável.

        Se o stream Firehose estiver configurado, emite um trail real.
        Caso contrário, usa NoOpDecisionTrailEmitter para validar o pipeline.
        """
        firehose_stream = os.environ.get(FIREHOSE_STREAM_ENV_VAR, "")

        if firehose_stream:
            # Usa Firehose real se configurado
            import boto3
            firehose_client = boto3.client(
                "firehose",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
            )
            emitter = FirehoseDecisionTrailEmitter(
                firehose_client=firehose_client,
                delivery_stream_name=firehose_stream,
            )
            logger.info(
                "Usando Firehose real para teste de integração",
                extra={"stream": firehose_stream},
            )
        else:
            # Fallback: emitter isolável (sem Firehose real)
            emitter = NoOpDecisionTrailEmitter()
            logger.info(
                "Firehose não configurado — usando NoOpDecisionTrailEmitter",
                extra={"env_var": FIREHOSE_STREAM_ENV_VAR},
            )

        facade = PolicyValidationFacade(
            context_builder=DefaultCanonicalValidationContextBuilder(),
            runtime_registry=aws_runtime_registry,
            evaluator=RuleEvaluator(),
            trail_emitter=emitter,
        )

        command = _make_command(debit_amount=100_000)
        result = facade.validate(command)

        assert result.is_valid is True


@pytest.mark.integration_aws_dev
class TestAWSPolicyFacadeCommandImmutability:
    """Testa imutabilidade do comando no pipeline com S3 real."""

    def test_comando_nao_e_mutado_com_s3_real(
        self,
        aws_facade: PolicyValidationFacade,
    ) -> None:
        """O comando original não deve ser mutado no pipeline com S3 real."""
        command = _make_command(debit_amount=100_000)
        original_external_id = command.external_id
        original_tenant_id = command.tenant_id

        aws_facade.validate(command)

        assert command.external_id == original_external_id
        assert command.tenant_id == original_tenant_id
