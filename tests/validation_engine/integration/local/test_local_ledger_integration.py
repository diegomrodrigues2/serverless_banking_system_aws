"""
Integração local end-to-end: Validation Engine + Ledger completo.

Exercita o pipeline completo do Write Path com o Validation Engine integrado:
  comando → ValidationChain (estruturais + policy) → JournalEntryFactory
  → LedgerRepository (in-memory) → persistência com DecisionSummary

Usa S3 mockado via moto, AppConfig mockado e repositório in-memory.
Não requer AWS real nem DynamoDB Local.

Cenários cobertos:
- Aprovação: summary persistido no metadata do JournalEntry
- Rejeição por policy: PolicyRejected antes da persistência
- Rejeição estrutural: validadores estruturais executam antes da policy
- Backward compatibility: engine sem facade continua funcionando
- Imutabilidade do comando ao longo de todo o pipeline

Requisitos cobertos: 1.3, 1.4, 7.1, 7.2, 7.3, 7.4, 12.4
"""

from __future__ import annotations

import copy
import tempfile
import uuid
from unittest.mock import MagicMock

import pytest

from ledger.application.commands import CreateJournalEntryCommand, PostingInput
from ledger.domain.aggregates import JournalEntry
from ledger.domain.errors import DomainError, ZeroSumViolation, InvalidAmountType
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.services import LedgerEngine
from ledger.domain.validators import (
    MinorUnitsValidator,
    TransactionLimitValidator,
    ValidationChain,
    ZeroSumValidator,
)
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
    description="Local ledger integration test",
    compiled_at="2024-01-01T00:00:00Z",
    source_hash="sha256:ledger_integration_test",
)

# DSL: deny transactions exceeding daily debit limit
_LEDGER_INTEGRATION_DSL = """
POLICY deny_over_limit PRIORITY 100
  WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > ref.daily_limit_minor
  THEN DENY "Transaction exceeds daily debit limit"

POLICY allow_standard PRIORITY 10
  WHEN facts.posting_count >= 2
  THEN ALLOW "Standard transaction"
"""

_SNAPSHOT = ReferenceSnapshot(
    snapshot_version="snap_ledger_int_001",
    snapshot_schema_version="1.0",
    created_at="2024-01-01T00:00:00Z",
    data={"daily_limit_minor": 500_000},
)


# ---------------------------------------------------------------------------
# In-memory repository (same pattern as test_ledger_contract_evolution.py)
# ---------------------------------------------------------------------------


class InMemoryLedgerRepository:
    """Repositório in-memory para testes de integração local."""

    def __init__(self) -> None:
        self._entries: dict[str, JournalEntry] = {}
        self._by_external_id: dict[str, JournalEntry] = {}

    def save_journal_entry(self, journal_entry: JournalEntry) -> None:
        self._entries[journal_entry.entry_id] = journal_entry
        self._by_external_id[journal_entry.external_id] = journal_entry

    def find_journal_entry_by_id(self, entry_id: str) -> JournalEntry | None:
        return self._entries.get(entry_id)

    def find_journal_entry_by_external_id(self, ext_id: str) -> JournalEntry | None:
        return self._by_external_id.get(ext_id)

    def get_balance(self, account_id: str, currency: str):
        return None

    def get_statement(self, account_id: str, cursor, page_size: int):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_command(
    debit_amount: int = 100_000,
    external_id: str | None = None,
    tenant_id: str = "tenantA",
    policy_context: dict | None = None,
    metadata: dict | None = None,
) -> CreateJournalEntryCommand:
    """Cria um comando balanceado (zero-sum) com postings BRL."""
    return CreateJournalEntryCommand(
        external_id=external_id or str(uuid.uuid4()),
        postings=[
            PostingInput("acc_debit", debit_amount, "BRL", "DEBIT"),
            PostingInput("acc_credit", debit_amount, "BRL", "CREDIT"),
        ],
        tenant_id=tenant_id,
        policy_context=policy_context or {},
        metadata=metadata or {},
    )


def _make_unbalanced_command(external_id: str | None = None) -> CreateJournalEntryCommand:
    """Cria um comando com postings desbalanceados (viola zero-sum)."""
    return CreateJournalEntryCommand(
        external_id=external_id or str(uuid.uuid4()),
        postings=[
            PostingInput("acc_debit", 1000, "BRL", "DEBIT"),
            PostingInput("acc_credit", 500, "BRL", "CREDIT"),
        ],
        tenant_id="tenantA",
    )


def _make_invalid_amount_command(external_id: str | None = None) -> CreateJournalEntryCommand:
    """Cria um comando com amount inválido (float em vez de int)."""
    return CreateJournalEntryCommand(
        external_id=external_id or str(uuid.uuid4()),
        postings=[
            PostingInput("acc_debit", 10.50, "BRL", "DEBIT"),
            PostingInput("acc_credit", 10.50, "BRL", "CREDIT"),
        ],
        tenant_id="tenantA",
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
    """Bundle compilado a partir da DSL de integração."""
    compiler = DSLCompiler.create_default()
    return compiler.compile(
        dsl_source=_LEDGER_INTEGRATION_DSL,
        policy_set_id="ledger_integration_bundle",
        metadata=_DEFAULT_META,
        compatibility=_DEFAULT_COMPAT,
    )


@pytest.fixture
def stored_artifacts(bundle_store, snapshot_store, compiled_bundle):
    """Armazena bundle e snapshot no S3 mockado e retorna o manifesto."""
    bundle_store.store(compiled_bundle)
    snapshot_store.store(_SNAPSHOT)
    return PolicyActivationManifest(
        activation_id="act_ledger_int_001",
        policy_scope_id=_SCOPE_ID,
        artifact_hash=compiled_bundle.artifact_hash,
        snapshot_version=_SNAPSHOT.snapshot_version,
        context_schema_version=_CONTEXT_SCHEMA_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        activated_at="2024-01-01T00:00:00Z",
        activated_by="integration-test",
    )


@pytest.fixture
def mock_manifest_resolver(stored_artifacts):
    resolver = MagicMock(spec=ManifestResolver)
    resolver.resolve.return_value = stored_artifacts
    return resolver


@pytest.fixture
def runtime_registry(mock_manifest_resolver, bundle_loader, snapshot_loader, lkg_temp_dir):
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
    """PolicyValidationFacade configurada com componentes locais."""
    return PolicyValidationFacade(
        context_builder=DefaultCanonicalValidationContextBuilder(),
        runtime_registry=runtime_registry,
        evaluator=RuleEvaluator(),
        trail_emitter=noop_emitter,
    )


@pytest.fixture
def repo():
    return InMemoryLedgerRepository()


@pytest.fixture
def engine_with_facade(repo, facade):
    """LedgerEngine com ValidationChain incluindo validadores estruturais + facade."""
    chain = ValidationChain(
        validators=[
            ZeroSumValidator(),
            MinorUnitsValidator(),
            TransactionLimitValidator(),
            facade,
        ]
    )
    factory = JournalEntryFactory()
    return LedgerEngine(repository=repo, validation_chain=chain, factory=factory)


@pytest.fixture
def engine_without_facade(repo):
    """LedgerEngine sem facade — backward compatibility."""
    chain = ValidationChain(
        validators=[
            ZeroSumValidator(),
            MinorUnitsValidator(),
            TransactionLimitValidator(),
        ]
    )
    factory = JournalEntryFactory()
    return LedgerEngine(repository=repo, validation_chain=chain, factory=factory)


# ---------------------------------------------------------------------------
# Tests: Aprovação end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestEndToEndApproval:
    """Pipeline completo: comando → chain → facade → factory → repository."""

    def test_approved_transaction_persists_with_summary(
        self, engine_with_facade, repo, stored_artifacts
    ) -> None:
        """Transação aprovada persiste JournalEntry com DecisionSummary no metadata."""
        cmd = _make_command(debit_amount=100_000)  # R$ 1.000 < limite R$ 5.000
        entry = engine_with_facade.create_journal_entry(cmd)

        # Entry persistido no repositório
        persisted = repo.find_journal_entry_by_id(entry.entry_id)
        assert persisted is not None
        assert persisted.external_id == cmd.external_id

        # DecisionSummary presente no metadata
        assert "policy_validation" in entry.metadata
        pv = entry.metadata["policy_validation"]
        assert pv["final_verdict"] == "APPROVED"
        assert pv["policy_scope_id"] == _SCOPE_ID
        assert pv["activation_id"] == stored_artifacts.activation_id
        assert pv["artifact_hash"] == stored_artifacts.artifact_hash
        assert pv["snapshot_version"] == stored_artifacts.snapshot_version
        assert pv["evaluator_version"] == EVALUATOR_VERSION
        assert pv["input_hash"].startswith("sha256:")
        assert pv["matched_deny_rule"] is None
        assert isinstance(pv["evaluation_latency_ms"], float)

    def test_approved_transaction_emits_trail(
        self, engine_with_facade, noop_emitter
    ) -> None:
        """Trail é emitido quando a transação é aprovada pelo pipeline completo."""
        cmd = _make_command(debit_amount=100_000)
        engine_with_facade.create_journal_entry(cmd)

        assert len(noop_emitter.emitted_trails) == 1
        trail = noop_emitter.emitted_trails[0]
        assert trail.final_verdict == FinalVerdict.APPROVED
        assert trail.external_id == cmd.external_id
        assert trail.tenant_id == cmd.tenant_id

    def test_approved_entry_has_correct_postings(self, engine_with_facade) -> None:
        """JournalEntry aprovado contém postings corretos convertidos para Value Objects."""
        cmd = _make_command(debit_amount=200_000)
        entry = engine_with_facade.create_journal_entry(cmd)

        assert len(entry.postings) == 2
        assert entry.validate_zero_sum() is True

    def test_approved_preserves_command_metadata(self, engine_with_facade) -> None:
        """Metadata original do comando é preservado junto com o summary."""
        cmd = _make_command(
            debit_amount=100_000,
            metadata={"order_id": "order-integration-001"},
        )
        entry = engine_with_facade.create_journal_entry(cmd)

        assert entry.metadata["order_id"] == "order-integration-001"
        assert "policy_validation" in entry.metadata


# ---------------------------------------------------------------------------
# Tests: Rejeição por policy
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestEndToEndPolicyRejection:
    """Rejeição por policy: transação estruturalmente válida mas rejeitada pela DSL."""

    def test_policy_rejection_raises_before_persistence(
        self, engine_with_facade, repo
    ) -> None:
        """Transação acima do limite é rejeitada e NÃO persistida."""
        cmd = _make_command(debit_amount=600_000)  # R$ 6.000 > limite R$ 5.000

        with pytest.raises(PolicyRejected) as exc_info:
            engine_with_facade.create_journal_entry(cmd)

        assert exc_info.value.code == "POLICY_REJECTED"
        assert exc_info.value.http_status == 422
        # Nada persistido no repositório
        assert repo.find_journal_entry_by_external_id(cmd.external_id) is None

    def test_policy_rejection_emits_trail(
        self, engine_with_facade, noop_emitter
    ) -> None:
        """Trail é emitido mesmo quando a transação é rejeitada por policy."""
        cmd = _make_command(debit_amount=600_000)

        with pytest.raises(PolicyRejected):
            engine_with_facade.create_journal_entry(cmd)

        assert len(noop_emitter.emitted_trails) == 1
        trail = noop_emitter.emitted_trails[0]
        assert trail.final_verdict == FinalVerdict.REJECTED
        assert trail.matched_deny_rule == "deny_over_limit"


# ---------------------------------------------------------------------------
# Tests: Validadores estruturais executam antes da policy
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestStructuralValidatorsBeforePolicy:
    """Validadores estruturais rejeitam ANTES do Validation Engine (Requisito 1.3, 1.4)."""

    def test_zero_sum_violation_rejects_before_policy(
        self, engine_with_facade, noop_emitter
    ) -> None:
        """ZeroSumViolation é levantada antes da policy ser avaliada."""
        cmd = _make_unbalanced_command()

        with pytest.raises(ZeroSumViolation):
            engine_with_facade.create_journal_entry(cmd)

        # Policy NÃO foi avaliada — nenhum trail emitido
        assert len(noop_emitter.emitted_trails) == 0

    def test_invalid_amount_rejects_before_policy(
        self, engine_with_facade, noop_emitter
    ) -> None:
        """InvalidAmountType é levantada antes da policy ser avaliada."""
        cmd = _make_invalid_amount_command()

        with pytest.raises(InvalidAmountType):
            engine_with_facade.create_journal_entry(cmd)

        # Policy NÃO foi avaliada — nenhum trail emitido
        assert len(noop_emitter.emitted_trails) == 0

    def test_structural_rejection_does_not_persist(
        self, engine_with_facade, repo
    ) -> None:
        """Rejeição estrutural não persiste nada no repositório."""
        cmd = _make_unbalanced_command()

        with pytest.raises(ZeroSumViolation):
            engine_with_facade.create_journal_entry(cmd)

        assert repo.find_journal_entry_by_external_id(cmd.external_id) is None


# ---------------------------------------------------------------------------
# Tests: Backward compatibility
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestBackwardCompatibility:
    """Engine sem facade continua funcionando — sem regressão no ledger existente."""

    def test_engine_without_facade_creates_entry_without_summary(
        self, engine_without_facade, repo
    ) -> None:
        """Engine sem facade produz JournalEntry sem policy_validation no metadata."""
        cmd = _make_command(debit_amount=100_000)
        entry = engine_without_facade.create_journal_entry(cmd)

        assert entry is not None
        assert "policy_validation" not in entry.metadata
        assert repo.find_journal_entry_by_id(entry.entry_id) is not None

    def test_engine_without_facade_still_validates_structural(
        self, engine_without_facade
    ) -> None:
        """Engine sem facade ainda valida invariantes estruturais."""
        cmd = _make_unbalanced_command()

        with pytest.raises(ZeroSumViolation):
            engine_without_facade.create_journal_entry(cmd)

    def test_command_without_policy_context_works(
        self, engine_with_facade
    ) -> None:
        """Comando sem policy_context funciona normalmente (default vazio)."""
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_a", 1000, "BRL", "DEBIT"),
                PostingInput("acc_b", 1000, "BRL", "CREDIT"),
            ],
            tenant_id="tenantA",
        )
        entry = engine_with_facade.create_journal_entry(cmd)
        assert entry is not None
        assert "policy_validation" in entry.metadata


# ---------------------------------------------------------------------------
# Tests: Imutabilidade do comando
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestCommandImmutability:
    """O comando original não é mutado em nenhum ponto do pipeline end-to-end."""

    def test_command_not_mutated_on_approval(self, engine_with_facade) -> None:
        """Comando não é mutado quando a transação é aprovada."""
        cmd = _make_command(
            debit_amount=100_000,
            tenant_id="tenantA",
            policy_context={"product": "PIX"},
            metadata={"order_id": "o-1"},
        )
        cmd_snapshot = copy.deepcopy(cmd)

        engine_with_facade.create_journal_entry(cmd)

        assert cmd.external_id == cmd_snapshot.external_id
        assert cmd.tenant_id == cmd_snapshot.tenant_id
        assert cmd.policy_context == cmd_snapshot.policy_context
        assert cmd.metadata == cmd_snapshot.metadata
        assert "policy_validation" not in cmd.metadata

    def test_command_not_mutated_on_policy_rejection(self, engine_with_facade) -> None:
        """Comando não é mutado quando a transação é rejeitada por policy."""
        cmd = _make_command(debit_amount=600_000, metadata={"trace": "t-1"})
        cmd_snapshot = copy.deepcopy(cmd)

        with pytest.raises(PolicyRejected):
            engine_with_facade.create_journal_entry(cmd)

        assert cmd.external_id == cmd_snapshot.external_id
        assert cmd.metadata == cmd_snapshot.metadata
