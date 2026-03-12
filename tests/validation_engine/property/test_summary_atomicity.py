"""
Property Test: DecisionSummary é persistido atomicamente com o JournalEntry.

Property 6 do design:
    Para toda transação aprovada, o DecisionSummary correspondente deve ser
    persistido junto com o JournalEntry na mesma operação. Não pode existir
    JournalEntry aprovado sem summary de policy, quando a policy validation
    está habilitada.

Estratégia:
    Gera comandos balanceados aleatórios, executa o fluxo completo do
    LedgerEngine com uma FakePolicyFacade injetada, e verifica que:
    1. Todo JournalEntry criado contém "policy_validation" no metadata.
    2. O summary contém todos os campos obrigatórios.
    3. O summary é consistente com o DecisionSummary retornado pela facade.

Requisitos: 1.1, 1.3, 12.4
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ledger.application.commands import CreateJournalEntryCommand, PostingInput
from ledger.domain.aggregates import JournalEntry
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.services import LedgerEngine
from ledger.domain.validators import (
    ValidationArtifacts,
    ValidationChain,
    ValidationResult,
    ZeroSumValidator,
    MinorUnitsValidator,
    TransactionLimitValidator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Campos obrigatórios do DecisionSummary no metadata (Requisito 12.2).
REQUIRED_SUMMARY_FIELDS = {
    "final_verdict",
    "policy_scope_id",
    "activation_id",
    "artifact_hash",
    "snapshot_version",
    "evaluator_version",
    "input_hash",
    "matched_deny_rule",
    "evaluation_latency_ms",
}


class InMemoryRepo:
    """Repositório in-memory mínimo para property tests."""

    def __init__(self) -> None:
        self._entries: dict[str, JournalEntry] = {}
        self._by_ext: dict[str, JournalEntry] = {}

    def save_journal_entry(self, je: JournalEntry) -> None:
        self._entries[je.entry_id] = je
        self._by_ext[je.external_id] = je

    def find_journal_entry_by_id(self, eid: str) -> JournalEntry | None:
        return self._entries.get(eid)

    def find_journal_entry_by_external_id(self, ext: str) -> JournalEntry | None:
        return self._by_ext.get(ext)

    def get_balance(self, *a, **kw):
        return None

    def get_statement(self, *a, **kw):
        return None


@dataclass(frozen=True)
class GeneratedSummary:
    """Summary gerado por Hypothesis para property tests."""

    final_verdict: str
    policy_scope_id: str
    activation_id: str
    artifact_hash: str
    snapshot_version: str
    evaluator_version: str
    input_hash: str
    matched_deny_rule: str | None
    evaluation_latency_ms: float

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


class StubFacade:
    """Facade stub que retorna summary gerado por Hypothesis."""

    def __init__(self, summary: GeneratedSummary) -> None:
        self._summary = summary

    def validate(self, command: object) -> ValidationResult:
        artifacts = ValidationArtifacts(decision_summary=self._summary)
        return ValidationResult.success(artifacts=artifacts)


# ---------------------------------------------------------------------------
# Hypothesis Strategies
# ---------------------------------------------------------------------------

# Gera strings alfanuméricas curtas para IDs e hashes.
_id_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=32,
)

summary_strategy = st.builds(
    GeneratedSummary,
    final_verdict=st.just("APPROVED"),
    policy_scope_id=_id_st,
    activation_id=_id_st,
    artifact_hash=_id_st,
    snapshot_version=_id_st,
    evaluator_version=_id_st,
    input_hash=_id_st,
    matched_deny_rule=st.none(),
    evaluation_latency_ms=st.floats(min_value=0.01, max_value=100.0),
)

amount_strategy = st.integers(min_value=1, max_value=10_000_000)
currency_strategy = st.sampled_from(["BRL", "USD", "EUR"])


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(summary=summary_strategy, amount=amount_strategy, currency=currency_strategy)
@settings(max_examples=50, deadline=None)
def test_approved_entry_always_contains_summary(
    summary: GeneratedSummary,
    amount: int,
    currency: str,
) -> None:
    """
    Property 6: Para toda transação aprovada com policy habilitada,
    o JournalEntry DEVE conter "policy_validation" no metadata com
    todos os campos obrigatórios do DecisionSummary.
    """
    facade = StubFacade(summary)
    repo = InMemoryRepo()
    chain = ValidationChain([
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
        facade,
    ])
    factory = JournalEntryFactory()
    engine = LedgerEngine(repository=repo, validation_chain=chain, factory=factory)

    cmd = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            PostingInput("acc_a", amount, currency, "DEBIT"),
            PostingInput("acc_b", amount, currency, "CREDIT"),
        ],
    )
    entry = engine.create_journal_entry(cmd)

    # O metadata DEVE conter policy_validation.
    assert "policy_validation" in entry.metadata
    pv = entry.metadata["policy_validation"]

    # Todos os campos obrigatórios DEVEM estar presentes.
    assert set(pv.keys()) == REQUIRED_SUMMARY_FIELDS

    # Os valores DEVEM ser consistentes com o summary gerado.
    assert pv["final_verdict"] == summary.final_verdict
    assert pv["policy_scope_id"] == summary.policy_scope_id
    assert pv["activation_id"] == summary.activation_id
    assert pv["artifact_hash"] == summary.artifact_hash
    assert pv["snapshot_version"] == summary.snapshot_version
