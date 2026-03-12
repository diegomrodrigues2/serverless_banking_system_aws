"""
Property Test: Isolamento de escopo multi-tenant.

Property 9 do design:
    Para toda avaliação, o PolicyScope resolvido deve pertencer ao tenant
    e operação do comando. Nenhuma rule de outro tenant ou escopo pode
    ser aplicada.

Estratégia:
    Gera pares de comandos com tenant_ids distintos e verifica que:
    1. O tenant_id do comando é preservado no DecisionSummary.
    2. Comandos de tenants diferentes produzem summaries com scope_ids distintos.
    3. O policy_context de um tenant não vaza para outro.

Requisitos: 5.5, 12.4
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from hypothesis import given, settings, assume
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


class InMemoryRepo:
    """Repositório in-memory mínimo."""

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


class TenantAwareFacade:
    """
    Facade que gera DecisionSummary com policy_scope_id derivado do tenant_id.

    Simula o comportamento real da PolicyValidationFacade que resolve
    o escopo a partir do tenant_id do comando.
    """

    def validate(self, command: object) -> ValidationResult:
        tenant_id = getattr(command, "tenant_id", "unknown")
        policy_context = getattr(command, "policy_context", {})
        operation = policy_context.get("operation_type", "TRANSFER")

        @dataclass(frozen=True)
        class _Summary:
            final_verdict: str = "APPROVED"
            policy_scope_id: str = f"{tenant_id}:{operation}:*:*:prod"
            activation_id: str = "act_001"
            artifact_hash: str = "sha256:abc"
            snapshot_version: str = "snap_001"
            evaluator_version: str = "1.0.0"
            input_hash: str = "sha256:input"
            matched_deny_rule: str | None = None
            evaluation_latency_ms: float = 1.0

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

        summary = _Summary()
        artifacts = ValidationArtifacts(decision_summary=summary)
        return ValidationResult.success(artifacts=artifacts)


# ---------------------------------------------------------------------------
# Hypothesis Strategies
# ---------------------------------------------------------------------------

tenant_id_strategy = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=16,
)
amount_strategy = st.integers(min_value=1, max_value=10_000_000)
currency_strategy = st.sampled_from(["BRL", "USD", "EUR"])


def _build_engine() -> tuple[LedgerEngine, InMemoryRepo]:
    repo = InMemoryRepo()
    facade = TenantAwareFacade()
    chain = ValidationChain([
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
        facade,
    ])
    factory = JournalEntryFactory()
    engine = LedgerEngine(repository=repo, validation_chain=chain, factory=factory)
    return engine, repo


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(tenant_id=tenant_id_strategy, amount=amount_strategy, currency=currency_strategy)
@settings(max_examples=50, deadline=None)
def test_summary_scope_contains_tenant_id(
    tenant_id: str,
    amount: int,
    currency: str,
) -> None:
    """
    Property 9: O policy_scope_id no DecisionSummary DEVE conter
    o tenant_id do comando que originou a transação.
    """
    engine, repo = _build_engine()
    cmd = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            PostingInput("acc_a", amount, currency, "DEBIT"),
            PostingInput("acc_b", amount, currency, "CREDIT"),
        ],
        tenant_id=tenant_id,
    )
    entry = engine.create_journal_entry(cmd)

    pv = entry.metadata["policy_validation"]
    # O scope_id DEVE começar com o tenant_id do comando.
    assert pv["policy_scope_id"].startswith(tenant_id)


@pytest.mark.property
@given(
    tenant_a=tenant_id_strategy,
    tenant_b=tenant_id_strategy,
    amount=amount_strategy,
    currency=currency_strategy,
)
@settings(max_examples=50, deadline=None)
def test_different_tenants_produce_different_scopes(
    tenant_a: str,
    tenant_b: str,
    amount: int,
    currency: str,
) -> None:
    """
    Property 9: Comandos de tenants distintos DEVEM produzir
    policy_scope_ids distintos no DecisionSummary.
    """
    assume(tenant_a != tenant_b)

    engine, repo = _build_engine()

    cmd_a = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            PostingInput("acc_a", amount, currency, "DEBIT"),
            PostingInput("acc_b", amount, currency, "CREDIT"),
        ],
        tenant_id=tenant_a,
    )
    cmd_b = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            PostingInput("acc_c", amount, currency, "DEBIT"),
            PostingInput("acc_d", amount, currency, "CREDIT"),
        ],
        tenant_id=tenant_b,
    )

    entry_a = engine.create_journal_entry(cmd_a)
    entry_b = engine.create_journal_entry(cmd_b)

    scope_a = entry_a.metadata["policy_validation"]["policy_scope_id"]
    scope_b = entry_b.metadata["policy_validation"]["policy_scope_id"]

    # Tenants distintos DEVEM ter scopes distintos.
    assert scope_a != scope_b


@pytest.mark.property
@given(
    tenant_id=tenant_id_strategy,
    amount=amount_strategy,
    currency=currency_strategy,
)
@settings(max_examples=50, deadline=None)
def test_policy_context_does_not_leak_to_metadata(
    tenant_id: str,
    amount: int,
    currency: str,
) -> None:
    """
    Property 9 (complemento): O policy_context do comando NÃO deve
    vazar para o metadata geral do JournalEntry. Ele é consumido
    apenas pelo Validation Engine.
    """
    engine, repo = _build_engine()
    cmd = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            PostingInput("acc_a", amount, currency, "DEBIT"),
            PostingInput("acc_b", amount, currency, "CREDIT"),
        ],
        tenant_id=tenant_id,
        policy_context={"secret_key": "should_not_leak"},
        metadata={"order_id": "o-1"},
    )
    entry = engine.create_journal_entry(cmd)

    # O metadata do entry NÃO deve conter policy_context diretamente.
    assert "secret_key" not in entry.metadata
    # O metadata original do comando é preservado.
    assert entry.metadata.get("order_id") == "o-1"