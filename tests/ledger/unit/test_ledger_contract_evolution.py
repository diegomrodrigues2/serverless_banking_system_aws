"""
Testes unitários e de contrato para a evolução do ledger (Task 11).

Valida:
- Backward compatibility: código existente sem policy continua funcionando.
- Propagação de artifacts: DecisionSummary flui da ValidationChain até o JournalEntry.
- Persistência do summary: DecisionSummary é gravado no metadata do JournalEntry.
- Isolamento: policy_context e metadata são campos separados no comando.
- Imutabilidade: o comando original não é mutado pela validação ou factory.

Requisitos cobertos: 7.2, 7.5, 7.6, 12.3, 12.4, 12.5
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field

import pytest

from ledger.application.commands import CreateJournalEntryCommand, PostingInput
from ledger.domain.aggregates import JournalEntry
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.services import LedgerEngine
from ledger.domain.validators import (
    ValidationArtifacts,
    ValidationChain,
    ValidationResult,
    ValidationStrategy,
    ZeroSumValidator,
    MinorUnitsValidator,
    TransactionLimitValidator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class InMemoryLedgerRepository:
    """Repositório in-memory para testes unitários."""

    def __init__(self) -> None:
        self._entries: dict[str, JournalEntry] = {}
        self._by_external_id: dict[str, JournalEntry] = {}

    def save_journal_entry(self, journal_entry: JournalEntry) -> None:
        self._entries[journal_entry.entry_id] = journal_entry
        self._by_external_id[journal_entry.external_id] = journal_entry

    def find_journal_entry_by_id(self, entry_id: str) -> JournalEntry | None:
        return self._entries.get(entry_id)

    def find_journal_entry_by_external_id(self, external_id: str) -> JournalEntry | None:
        return self._by_external_id.get(external_id)

    def get_balance(self, account_id: str, currency: str):
        return None

    def get_statement(self, account_id: str, cursor, page_size: int):
        return None


@dataclass(frozen=True)
class FakeDecisionSummary:
    """Simula DecisionSummary do validation engine para testes de contrato."""

    final_verdict: str = "APPROVED"
    policy_scope_id: str = "tenantA:TRANSFER:PIX:*:prod"
    activation_id: str = "act_001"
    artifact_hash: str = "sha256:abc123"
    snapshot_version: str = "snap_001"
    evaluator_version: str = "1.0.0"
    input_hash: str = "sha256:input_abc"
    matched_deny_rule: str | None = None
    evaluation_latency_ms: float = 2.5

    def to_metadata(self) -> dict:
        return {
            "policy_validation": {
                "final_verdict": self.final_verdict,
                "policy_scope_id": self.policy_scope_id,
                "activation_id": self.activation_id,
                "artifact_hash": self.artifact_hash,
                "snapshot_version": self.snapshot_version,
                "evaluator_version": self.evaluator_version,
                "input_hash": self.input_hash,
                "matched_deny_rule": self.matched_deny_rule,
                "evaluation_latency_ms": self.evaluation_latency_ms,
            }
        }


class FakePolicyFacade:
    """
    Simula PolicyValidationFacade para testes de contrato.

    Retorna ValidationResult.success() com DecisionSummary nos artefatos,
    simulando o comportamento real da facade sem dependências do validation engine.
    """

    def __init__(self, decision_summary: FakeDecisionSummary | None = None) -> None:
        self._summary = decision_summary or FakeDecisionSummary()

    def validate(self, command: object) -> ValidationResult:
        artifacts = ValidationArtifacts(decision_summary=self._summary)
        return ValidationResult.success(artifacts=artifacts)


def _make_balanced_command(
    external_id: str | None = None,
    tenant_id: str = "",
    policy_context: dict | None = None,
    metadata: dict | None = None,
) -> CreateJournalEntryCommand:
    """Cria um comando balanceado (zero-sum) com campos opcionais."""
    return CreateJournalEntryCommand(
        external_id=external_id or str(uuid.uuid4()),
        postings=[
            PostingInput("acc_a", 1000, "BRL", "DEBIT"),
            PostingInput("acc_b", 1000, "BRL", "CREDIT"),
        ],
        tenant_id=tenant_id,
        policy_context=policy_context or {},
        metadata=metadata or {},
    )


def _build_engine_with_facade(
    facade: FakePolicyFacade | None = None,
) -> tuple[LedgerEngine, InMemoryLedgerRepository]:
    """Constrói LedgerEngine com facade injetada na ValidationChain."""
    repo = InMemoryLedgerRepository()
    validators: list = [
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
    ]
    if facade is not None:
        validators.append(facade)
    chain = ValidationChain(validators=validators)
    factory = JournalEntryFactory()
    engine = LedgerEngine(repository=repo, validation_chain=chain, factory=factory)
    return engine, repo


# ---------------------------------------------------------------------------
# Backward Compatibility Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackwardCompatibility:
    """Verifica que código existente sem policy continua funcionando."""

    def test_command_without_tenant_id_defaults_to_empty(self) -> None:
        """Comando sem tenant_id usa string vazia como padrão."""
        cmd = CreateJournalEntryCommand(
            external_id="ext-001",
            postings=[
                PostingInput("acc_a", 1000, "BRL", "DEBIT"),
                PostingInput("acc_b", 1000, "BRL", "CREDIT"),
            ],
        )
        assert cmd.tenant_id == ""
        assert cmd.policy_context == {}

    def test_command_with_tenant_id_and_policy_context(self) -> None:
        """Comando com tenant_id e policy_context preserva os valores."""
        cmd = _make_balanced_command(
            tenant_id="tenantA",
            policy_context={"product_code": "PIX"},
        )
        assert cmd.tenant_id == "tenantA"
        assert cmd.policy_context == {"product_code": "PIX"}

    def test_engine_without_facade_produces_entry_without_summary(self) -> None:
        """Engine sem facade produz JournalEntry sem policy_validation no metadata."""
        engine, repo = _build_engine_with_facade(facade=None)
        cmd = _make_balanced_command()
        entry = engine.create_journal_entry(cmd)
        assert "policy_validation" not in entry.metadata

    def test_validation_result_success_without_artifacts(self) -> None:
        """ValidationResult.success() sem artefatos mantém artifacts vazio."""
        result = ValidationResult.success()
        assert result.is_valid is True
        assert result.artifacts.decision_summary is None

    def test_validation_result_success_with_artifacts(self) -> None:
        """ValidationResult.success() com artefatos carrega DecisionSummary."""
        summary = FakeDecisionSummary()
        artifacts = ValidationArtifacts(decision_summary=summary)
        result = ValidationResult.success(artifacts=artifacts)
        assert result.artifacts.decision_summary is summary


# ---------------------------------------------------------------------------
# Artifact Propagation Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestArtifactPropagation:
    """Verifica que DecisionSummary flui da ValidationChain até o JournalEntry."""

    def test_chain_accumulates_artifacts_from_facade(self) -> None:
        """ValidationChain acumula artefatos do PolicyValidationFacade."""
        facade = FakePolicyFacade()
        chain = ValidationChain([
            ZeroSumValidator(),
            MinorUnitsValidator(),
            facade,
        ])
        cmd = _make_balanced_command()
        result = chain.validate(cmd)
        assert result.artifacts.decision_summary is not None

    def test_chain_without_facade_has_empty_artifacts(self) -> None:
        """ValidationChain sem facade retorna artefatos vazios."""
        chain = ValidationChain([
            ZeroSumValidator(),
            MinorUnitsValidator(),
        ])
        cmd = _make_balanced_command()
        result = chain.validate(cmd)
        assert result.artifacts.decision_summary is None

    def test_engine_with_facade_persists_summary_in_metadata(self) -> None:
        """Engine com facade persiste DecisionSummary no metadata do JournalEntry."""
        facade = FakePolicyFacade()
        engine, repo = _build_engine_with_facade(facade=facade)
        cmd = _make_balanced_command()
        entry = engine.create_journal_entry(cmd)

        assert "policy_validation" in entry.metadata
        pv = entry.metadata["policy_validation"]
        assert pv["final_verdict"] == "APPROVED"
        assert pv["policy_scope_id"] == "tenantA:TRANSFER:PIX:*:prod"
        assert pv["activation_id"] == "act_001"
        assert pv["artifact_hash"] == "sha256:abc123"
        assert pv["snapshot_version"] == "snap_001"

    def test_engine_with_facade_preserves_command_metadata(self) -> None:
        """Engine com facade preserva metadata original do comando junto com summary."""
        facade = FakePolicyFacade()
        engine, repo = _build_engine_with_facade(facade=facade)
        cmd = _make_balanced_command(metadata={"order_id": "order-001"})
        entry = engine.create_journal_entry(cmd)

        # Metadata original preservado
        assert entry.metadata["order_id"] == "order-001"
        # Summary adicionado
        assert "policy_validation" in entry.metadata


# ---------------------------------------------------------------------------
# Summary Persistence Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSummaryPersistence:
    """Verifica que DecisionSummary é persistido corretamente no JournalEntry."""

    def test_summary_all_fields_present_in_metadata(self) -> None:
        """Todos os campos do DecisionSummary estão presentes no metadata."""
        summary = FakeDecisionSummary(
            final_verdict="APPROVED",
            policy_scope_id="tenantB:PAYMENT:*:*:prod",
            activation_id="act_002",
            artifact_hash="sha256:def456",
            snapshot_version="snap_002",
            evaluator_version="1.1.0",
            input_hash="sha256:input_def",
            matched_deny_rule=None,
            evaluation_latency_ms=3.7,
        )
        facade = FakePolicyFacade(decision_summary=summary)
        engine, repo = _build_engine_with_facade(facade=facade)
        cmd = _make_balanced_command()
        entry = engine.create_journal_entry(cmd)

        pv = entry.metadata["policy_validation"]
        assert pv["final_verdict"] == "APPROVED"
        assert pv["policy_scope_id"] == "tenantB:PAYMENT:*:*:prod"
        assert pv["activation_id"] == "act_002"
        assert pv["artifact_hash"] == "sha256:def456"
        assert pv["snapshot_version"] == "snap_002"
        assert pv["evaluator_version"] == "1.1.0"
        assert pv["input_hash"] == "sha256:input_def"
        assert pv["matched_deny_rule"] is None
        assert pv["evaluation_latency_ms"] == 3.7

    def test_factory_without_artifacts_produces_clean_metadata(self) -> None:
        """Factory sem artefatos produz metadata limpo (sem policy_validation)."""
        factory = JournalEntryFactory()
        cmd = _make_balanced_command(metadata={"key": "value"})
        entry = factory.create_standard(cmd, validation_artifacts=None)
        assert entry.metadata == {"key": "value"}
        assert "policy_validation" not in entry.metadata

    def test_factory_with_artifacts_merges_summary(self) -> None:
        """Factory com artefatos mescla DecisionSummary no metadata."""
        factory = JournalEntryFactory()
        summary = FakeDecisionSummary()
        artifacts = ValidationArtifacts(decision_summary=summary)
        cmd = _make_balanced_command(metadata={"order_id": "o-1"})
        entry = factory.create_standard(cmd, validation_artifacts=artifacts)

        assert entry.metadata["order_id"] == "o-1"
        assert "policy_validation" in entry.metadata
        assert entry.metadata["policy_validation"]["final_verdict"] == "APPROVED"


# ---------------------------------------------------------------------------
# Command Immutability Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCommandImmutability:
    """Verifica que o comando original não é mutado pela validação ou factory."""

    def test_factory_does_not_mutate_command_metadata(self) -> None:
        """Factory não muta o dict metadata do comando original (Requisito 7.5)."""
        original_metadata = {"order_id": "o-1"}
        cmd = _make_balanced_command(metadata=original_metadata)
        metadata_before = dict(cmd.metadata)

        factory = JournalEntryFactory()
        summary = FakeDecisionSummary()
        artifacts = ValidationArtifacts(decision_summary=summary)
        entry = factory.create_standard(cmd, validation_artifacts=artifacts)

        # O metadata do comando original NÃO foi mutado.
        assert cmd.metadata == metadata_before
        assert "policy_validation" not in cmd.metadata
        # O entry tem o summary mesclado.
        assert "policy_validation" in entry.metadata

    def test_engine_does_not_mutate_command(self) -> None:
        """Engine com facade não muta o comando original."""
        facade = FakePolicyFacade()
        engine, repo = _build_engine_with_facade(facade=facade)
        cmd = _make_balanced_command(
            tenant_id="tenantA",
            policy_context={"product": "PIX"},
            metadata={"order_id": "o-1"},
        )
        cmd_copy = copy.deepcopy(cmd)
        engine.create_journal_entry(cmd)

        # Todos os campos do comando permanecem inalterados.
        assert cmd.external_id == cmd_copy.external_id
        assert cmd.tenant_id == cmd_copy.tenant_id
        assert cmd.policy_context == cmd_copy.policy_context
        assert cmd.metadata == cmd_copy.metadata


# ---------------------------------------------------------------------------
# ValidationArtifacts Merge Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidationArtifactsMerge:
    """Verifica o comportamento do merge de ValidationArtifacts."""

    def test_merge_prefers_other_summary(self) -> None:
        """Merge prioriza o DecisionSummary do 'other'."""
        a = ValidationArtifacts(decision_summary="summary_a")
        b = ValidationArtifacts(decision_summary="summary_b")
        merged = a.merge(b)
        assert merged.decision_summary == "summary_b"

    def test_merge_keeps_self_when_other_is_none(self) -> None:
        """Merge mantém o summary do self quando other é None."""
        a = ValidationArtifacts(decision_summary="summary_a")
        b = ValidationArtifacts(decision_summary=None)
        merged = a.merge(b)
        assert merged.decision_summary == "summary_a"

    def test_merge_both_none_returns_none(self) -> None:
        """Merge de dois artefatos vazios retorna None."""
        a = ValidationArtifacts()
        b = ValidationArtifacts()
        merged = a.merge(b)
        assert merged.decision_summary is None

    def test_merge_is_immutable(self) -> None:
        """Merge retorna novo objeto, não muta os originais."""
        a = ValidationArtifacts(decision_summary="a")
        b = ValidationArtifacts(decision_summary="b")
        merged = a.merge(b)
        assert a.decision_summary == "a"
        assert b.decision_summary == "b"
        assert merged.decision_summary == "b"
