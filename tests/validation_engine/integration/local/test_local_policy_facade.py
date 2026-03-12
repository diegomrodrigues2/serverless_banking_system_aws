"""
Integração local: pipeline completo da PolicyValidationFacade.

Exercita o pipeline completo do Data Plane sem o ledger:
  compile DSL → store bundle/snapshot → bootstrap runtime registry
  → build context → validate via facade → verify trail emitted

Usa S3 mockado via moto e AppConfig mockado. Não requer AWS real.

Cobre:
- Pipeline completo local sem ledger ainda (Requisito 7.1, 12.1, 13.1)
- Aprovação: ValidationResult.success() com trail emitido
- Rejeição: PolicyRejected com trail emitido
- Runtime não pronto: PolicyEngineNotReady propagado
- Falha do emitter: não afeta resultado da validação
- Imutabilidade do comando no pipeline completo

Requisitos cobertos: 7.1, 12.1, 13.1
"""

from __future__ import annotations

import tempfile
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
from validation_engine.domain.errors import PolicyEngineNotReady, PolicyRejected
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_KMS_KEY_ID = "arn:aws:kms:us-east-1:123456789012:key/test-key-id"
_CONTEXT_SCHEMA_VERSION = "1.0"
_SCOPE_ID = "tenantA:TRANSFER:PIX:*:prod"

_DEFAULT_COMPAT = BundleCompatibility(
    dsl_version="1.0",
    context_schema_version=_CONTEXT_SCHEMA_VERSION,
    snapshot_schema_version="1.0",
    evaluator_min_version=EVALUATOR_VERSION,
)

_DEFAULT_META = CompilationMetadata(
    author="integration-test",
    description="Local facade integration test",
    compiled_at="2024-01-01T00:00:00Z",
    source_hash="sha256:facade_local_test",
)

# DSL com regras para o teste de integração da facade
_FACADE_DSL = """
POLICY deny_over_limit PRIORITY 100
  WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > ref.daily_limit_minor
  THEN DENY "Transaction exceeds daily limit"

POLICY allow_standard PRIORITY 10
  WHEN facts.posting_count >= 2
  THEN ALLOW "Standard transaction"
"""

# Snapshot com dados de referência
_SNAPSHOT = ReferenceSnapshot(
    snapshot_version="snap_facade_local_001",
    snapshot_schema_version="1.0",
    created_at="2024-01-01T00:00:00Z",
    data={"daily_limit_minor": 500_000},
)


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
    """Simula CreateJournalEntryCommand para testes de integração local."""

    external_id: str = "ext_facade_local_001"
    tenant_id: str = "tenantA"
    operation_type: str = "TRANSFER"
    product_code: str | None = "PIX"
    channel: str | None = None
    postings: tuple = field(default_factory=tuple)
    policy_context: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)


def _make_command(
    debit_amount: int = 100_000,
    external_id: str = "ext_facade_local_001",
) -> _FakeCommand:
    """Cria um comando fake com postings balanceados em BRL."""
    return _FakeCommand(
        external_id=external_id,
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bundle_store(moto_s3_client, local_s3_bucket):
    return BundleStore(
        s3_client=moto_s3_client,
        bucket_name=local_s3_bucket,
        kms_key_id=_FAKE_KMS_KEY_ID,
    )


@pytest.fixture
def bundle_loader(moto_s3_client, local_s3_bucket):
    return BundleLoader(
        s3_client=moto_s3_client,
        bucket_name=local_s3_bucket,
        current_context_schema_version=_CONTEXT_SCHEMA_VERSION,
        current_evaluator_version=EVALUATOR_VERSION,
    )


@pytest.fixture
def snapshot_store(moto_s3_client, local_s3_bucket):
    return SnapshotStore(
        s3_client=moto_s3_client,
        bucket_name=local_s3_bucket,
        kms_key_id=_FAKE_KMS_KEY_ID,
    )


@pytest.fixture
def snapshot_loader(moto_s3_client, local_s3_bucket):
    return SnapshotLoader(
        s3_client=moto_s3_client,
        bucket_name=local_s3_bucket,
        expected_snapshot_schema_version="1.0",
    )


@pytest.fixture
def compiled_bundle():
    """Bundle compilado a partir do DSL da facade."""
    compiler = DSLCompiler.create_default()
    return compiler.compile(
        dsl_source=_FACADE_DSL,
        policy_set_id="facade_local_bundle",
        metadata=_DEFAULT_META,
        compatibility=_DEFAULT_COMPAT,
    )


@pytest.fixture
def stored_artifacts(
    bundle_store,
    snapshot_store,
    compiled_bundle,
):
    """Armazena bundle e snapshot no S3 mockado e retorna o manifesto."""
    bundle_store.store(compiled_bundle)
    snapshot_store.store(_SNAPSHOT)

    manifest = PolicyActivationManifest(
        activation_id="act_facade_local_001",
        policy_scope_id=_SCOPE_ID,
        artifact_hash=compiled_bundle.artifact_hash,
        snapshot_version=_SNAPSHOT.snapshot_version,
        context_schema_version=_CONTEXT_SCHEMA_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        activated_at="2024-01-01T00:00:00Z",
        activated_by="integration-test",
    )
    return manifest


@pytest.fixture
def mock_manifest_resolver(stored_artifacts):
    """ManifestResolver mockado que retorna o manifesto armazenado."""
    resolver = MagicMock(spec=ManifestResolver)
    resolver.resolve.return_value = stored_artifacts
    return resolver


@pytest.fixture
def runtime_registry(
    mock_manifest_resolver,
    bundle_loader,
    snapshot_loader,
    lkg_temp_dir,
):
    """PolicyRuntimeRegistry configurado com recursos locais."""
    lkg_store = LKGStore(lkg_dir=lkg_temp_dir)
    return PolicyRuntimeRegistry(
        manifest_resolver=mock_manifest_resolver,
        bundle_loader=bundle_loader,
        snapshot_loader=snapshot_loader,
        lkg_store=lkg_store,
        evaluator_version=EVALUATOR_VERSION,
    )


@pytest.fixture
def noop_emitter():
    return NoOpDecisionTrailEmitter()


@pytest.fixture
def facade(runtime_registry, noop_emitter):
    """PolicyValidationFacade configurada com todos os componentes locais."""
    return PolicyValidationFacade(
        context_builder=DefaultCanonicalValidationContextBuilder(),
        runtime_registry=runtime_registry,
        evaluator=RuleEvaluator(),
        trail_emitter=noop_emitter,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestLocalPolicyFacadeApproval:
    """Testa o caminho de aprovação no pipeline completo local."""

    def test_pipeline_completo_aprova_transacao_dentro_do_limite(
        self,
        facade: PolicyValidationFacade,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """Pipeline completo: transação dentro do limite deve ser aprovada."""
        command = _make_command(debit_amount=100_000)  # R$ 1.000,00 < limite R$ 5.000,00

        result = facade.validate(command)

        assert result.is_valid is True

    def test_pipeline_emite_trail_em_aprovacao(
        self,
        facade: PolicyValidationFacade,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """Pipeline completo: trail deve ser emitido quando aprovado."""
        command = _make_command(debit_amount=100_000)

        facade.validate(command)

        assert len(noop_emitter.emitted_trails) == 1
        trail = noop_emitter.emitted_trails[0]
        assert trail.final_verdict == FinalVerdict.APPROVED
        assert trail.external_id == command.external_id
        assert trail.tenant_id == command.tenant_id

    def test_trail_contem_identificadores_corretos(
        self,
        facade: PolicyValidationFacade,
        noop_emitter: NoOpDecisionTrailEmitter,
        stored_artifacts: PolicyActivationManifest,
    ) -> None:
        """Trail emitido deve conter os identificadores corretos do manifesto."""
        command = _make_command(debit_amount=100_000)

        facade.validate(command)

        trail = noop_emitter.emitted_trails[0]
        assert trail.activation_id == stored_artifacts.activation_id
        assert trail.artifact_hash == stored_artifacts.artifact_hash
        assert trail.snapshot_version == stored_artifacts.snapshot_version
        assert trail.evaluator_version == EVALUATOR_VERSION
        assert trail.input_hash.startswith("sha256:")

    def test_trail_contem_rules_avaliadas(
        self,
        facade: PolicyValidationFacade,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """Trail emitido deve conter os resultados de todas as rules avaliadas."""
        command = _make_command(debit_amount=100_000)

        facade.validate(command)

        trail = noop_emitter.emitted_trails[0]
        rule_names = {r.rule_name for r in trail.rules}
        assert "deny_over_limit" in rule_names
        assert "allow_standard" in rule_names


@pytest.mark.integration_local
class TestLocalPolicyFacadeRejection:
    """Testa o caminho de rejeição no pipeline completo local."""

    def test_pipeline_completo_rejeita_transacao_acima_do_limite(
        self,
        facade: PolicyValidationFacade,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """Pipeline completo: transação acima do limite deve ser rejeitada."""
        command = _make_command(debit_amount=600_000)  # R$ 6.000,00 > limite R$ 5.000,00

        with pytest.raises(PolicyRejected) as exc_info:
            facade.validate(command)

        assert "deny_over_limit" in str(exc_info.value)

    def test_trail_emitido_mesmo_em_rejeicao(
        self,
        facade: PolicyValidationFacade,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """Trail deve ser emitido mesmo quando a transação é rejeitada."""
        command = _make_command(debit_amount=600_000)

        with pytest.raises(PolicyRejected):
            facade.validate(command)

        assert len(noop_emitter.emitted_trails) == 1
        trail = noop_emitter.emitted_trails[0]
        assert trail.final_verdict == FinalVerdict.REJECTED
        assert trail.matched_deny_rule == "deny_over_limit"

    def test_policy_rejected_tem_codigo_correto(
        self,
        facade: PolicyValidationFacade,
    ) -> None:
        """PolicyRejected deve ter código POLICY_REJECTED e HTTP 422."""
        command = _make_command(debit_amount=600_000)

        with pytest.raises(PolicyRejected) as exc_info:
            facade.validate(command)

        assert exc_info.value.code == "POLICY_REJECTED"
        assert exc_info.value.http_status == 422


@pytest.mark.integration_local
class TestLocalPolicyFacadeEmitterIsolation:
    """Testa isolamento de falha do emitter no pipeline completo local."""

    def test_falha_do_firehose_nao_afeta_aprovacao(
        self,
        runtime_registry: PolicyRuntimeRegistry,
    ) -> None:
        """Falha do Firehose não deve afetar o resultado da validação."""
        mock_firehose_client = MagicMock()
        mock_firehose_client.put_record.side_effect = RuntimeError("Firehose indisponível")

        firehose_emitter = FirehoseDecisionTrailEmitter(
            firehose_client=mock_firehose_client,
            delivery_stream_name="test-stream",
        )

        facade = PolicyValidationFacade(
            context_builder=DefaultCanonicalValidationContextBuilder(),
            runtime_registry=runtime_registry,
            evaluator=RuleEvaluator(),
            trail_emitter=firehose_emitter,
        )

        command = _make_command(debit_amount=100_000)
        result = facade.validate(command)

        assert result.is_valid is True


@pytest.mark.integration_local
class TestLocalPolicyFacadeCommandImmutability:
    """Testa imutabilidade do comando no pipeline completo local."""

    def test_comando_nao_e_mutado_em_aprovacao(
        self,
        facade: PolicyValidationFacade,
    ) -> None:
        """O comando original não deve ser mutado quando aprovado."""
        command = _make_command(debit_amount=100_000, external_id="ext_imutavel_001")
        original_external_id = command.external_id
        original_tenant_id = command.tenant_id

        facade.validate(command)

        assert command.external_id == original_external_id
        assert command.tenant_id == original_tenant_id

    def test_comando_nao_e_mutado_em_rejeicao(
        self,
        facade: PolicyValidationFacade,
    ) -> None:
        """O comando original não deve ser mutado quando rejeitado."""
        command = _make_command(debit_amount=600_000, external_id="ext_imutavel_002")
        original_external_id = command.external_id

        with pytest.raises(PolicyRejected):
            facade.validate(command)

        assert command.external_id == original_external_id


@pytest.mark.integration_local
class TestLocalPolicyFacadeEngineNotReady:
    """Testa comportamento fail-closed quando o motor não está pronto."""

    def test_engine_not_ready_quando_sem_manifesto(
        self,
        bundle_loader,
        snapshot_loader,
        lkg_temp_dir,
        noop_emitter,
    ) -> None:
        """PolicyEngineNotReady deve ser levantado quando não há manifesto disponível."""
        # ManifestResolver que sempre falha
        failing_resolver = MagicMock(spec=ManifestResolver)
        from validation_engine.domain.errors import PolicyBundleUnavailable
        failing_resolver.resolve.side_effect = PolicyBundleUnavailable(
            "AppConfig indisponível"
        )

        lkg_store = LKGStore(lkg_dir=lkg_temp_dir)
        registry = PolicyRuntimeRegistry(
            manifest_resolver=failing_resolver,
            bundle_loader=bundle_loader,
            snapshot_loader=snapshot_loader,
            lkg_store=lkg_store,
            evaluator_version=EVALUATOR_VERSION,
        )

        facade = PolicyValidationFacade(
            context_builder=DefaultCanonicalValidationContextBuilder(),
            runtime_registry=registry,
            evaluator=RuleEvaluator(),
            trail_emitter=noop_emitter,
        )

        command = _make_command()

        with pytest.raises(PolicyEngineNotReady):
            facade.validate(command)
