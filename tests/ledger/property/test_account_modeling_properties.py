"""
Testes de propriedade para modelagem de contas por usuário.

Propriedades cobertas:
- Property 11: Modelagem de contas por usuário (Available + Hold)
  Para qualquer operação de bloqueio de saldo, o JournalEntry deve conter
  um posting de débito na conta Available e um posting de crédito na conta Hold
  do mesmo usuário. Para liberação, o sentido é invertido.

Requisitos validados: 6.1
"""
from __future__ import annotations

import uuid

import pytest
from hypothesis import given, strategies as st

from ledger.application.commands import CreateJournalEntryCommand, PostingInput
from ledger.domain.aggregates import JournalEntry
from ledger.domain.errors import IdempotencyConflict
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.ports import StatementPage
from ledger.domain.services import LedgerEngine
from ledger.domain.validators import (
    MinorUnitsValidator,
    TransactionLimitValidator,
    ValidationChain,
    ZeroSumValidator,
)
from ledger.domain.value_objects import AccountType, Direction, Posting

# ---------------------------------------------------------------------------
# InMemoryLedgerRepository — repositório in-memory para testes de propriedade
# ---------------------------------------------------------------------------


class InMemoryLedgerRepository:
    """
    Implementação in-memory do LedgerRepository para testes de propriedade.

    Satisfaz o protocolo LedgerRepository sem dependência de infraestrutura.
    """

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

    def get_statement(self, account_id: str, cursor, page_size: int) -> StatementPage:
        return StatementPage()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_engine() -> tuple[LedgerEngine, InMemoryLedgerRepository]:
    """Constrói LedgerEngine com repositório in-memory e cadeia de validação padrão."""
    repo = InMemoryLedgerRepository()
    chain = ValidationChain([
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
    ])
    factory = JournalEntryFactory()
    engine = LedgerEngine(repository=repo, validation_chain=chain, factory=factory)
    return engine, repo


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Moedas suportadas pelo sistema
currencies = st.sampled_from(["BRL", "USD", "EUR", "GBP"])

# Valores monetários válidos em minor units
valid_amounts = st.integers(min_value=1, max_value=10_000_000_00)

# Gera tenant_ids alfanuméricos
tenant_id_strategy = st.text(
    min_size=1,
    max_size=36,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)


@st.composite
def hold_operation_command_strategy(draw: st.DrawFn) -> tuple[CreateJournalEntryCommand, str, str]:
    """
    Gera um comando de bloqueio de saldo (Available → Hold) com os IDs das contas.

    Retorna:
        (command, available_account_id, hold_account_id)
    """
    tenant_id = draw(tenant_id_strategy)
    amount = draw(valid_amounts)
    currency = draw(currencies)

    # Contas do usuário: Available e Hold pertencem ao mesmo tenant
    available_account_id = f"acc_available_{tenant_id}"
    hold_account_id = f"acc_hold_{tenant_id}"

    command = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            # Bloqueio: débito em Available (sai do disponível)
            PostingInput(
                account_id=available_account_id,
                amount=amount,
                currency=currency,
                direction="DEBIT",
            ),
            # Bloqueio: crédito em Hold (entra no bloqueado)
            PostingInput(
                account_id=hold_account_id,
                amount=amount,
                currency=currency,
                direction="CREDIT",
            ),
        ],
        metadata={"tenant_id": tenant_id, "operation": "hold"},
    )
    return command, available_account_id, hold_account_id


@st.composite
def release_operation_command_strategy(draw: st.DrawFn) -> tuple[CreateJournalEntryCommand, str, str]:
    """
    Gera um comando de liberação de saldo (Hold → Available) com os IDs das contas.

    Retorna:
        (command, hold_account_id, available_account_id)
    """
    tenant_id = draw(tenant_id_strategy)
    amount = draw(valid_amounts)
    currency = draw(currencies)

    available_account_id = f"acc_available_{tenant_id}"
    hold_account_id = f"acc_hold_{tenant_id}"

    command = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            # Liberação: débito em Hold (sai do bloqueado)
            PostingInput(
                account_id=hold_account_id,
                amount=amount,
                currency=currency,
                direction="DEBIT",
            ),
            # Liberação: crédito em Available (volta ao disponível)
            PostingInput(
                account_id=available_account_id,
                amount=amount,
                currency=currency,
                direction="CREDIT",
            ),
        ],
        metadata={"tenant_id": tenant_id, "operation": "release"},
    )
    return command, hold_account_id, available_account_id


# ---------------------------------------------------------------------------
# Property 11: Modelagem de contas por usuário (Available + Hold)
# ---------------------------------------------------------------------------


# Feature: double-entry-ledger, Property 11: Modelagem de contas por usuário
@pytest.mark.property
@given(hold_data=hold_operation_command_strategy())
def test_hold_operation_debits_available_credits_hold(
    hold_data: tuple[CreateJournalEntryCommand, str, str],
) -> None:
    """
    **Validates: Requirements 6.1, 6.2**

    Para qualquer operação de bloqueio de saldo, o JournalEntry criado deve:
    1. Conter um posting de DEBIT na conta Available do usuário
    2. Conter um posting de CREDIT na conta Hold do usuário
    3. Satisfazer a regra de zero-sum (débito == crédito em valor absoluto)

    Invariante verificada:
        Para todo JournalEntry de bloqueio:
        - exists posting: account_id == available_account_id AND direction == DEBIT
        - exists posting: account_id == hold_account_id AND direction == CREDIT
        - sum(signed_amount) == 0 por moeda
    """
    command, available_account_id, hold_account_id = hold_data
    engine, repo = _build_engine()

    entry = engine.create_journal_entry(command)

    # Verifica que o lançamento foi criado e persistido
    assert entry is not None
    assert entry.entry_id is not None

    # Verifica que existe posting de DEBIT na conta Available
    available_debits = [
        p for p in entry.postings
        if p.account_id == available_account_id and p.direction == Direction.DEBIT
    ]
    assert len(available_debits) >= 1, (
        f"Operação de bloqueio deve ter DEBIT na conta Available ({available_account_id}). "
        f"Postings: {entry.postings}"
    )

    # Verifica que existe posting de CREDIT na conta Hold
    hold_credits = [
        p for p in entry.postings
        if p.account_id == hold_account_id and p.direction == Direction.CREDIT
    ]
    assert len(hold_credits) >= 1, (
        f"Operação de bloqueio deve ter CREDIT na conta Hold ({hold_account_id}). "
        f"Postings: {entry.postings}"
    )

    # Verifica invariante de zero-sum (partidas dobradas)
    assert entry.validate_zero_sum() is True, (
        f"JournalEntry de bloqueio deve satisfazer zero-sum. "
        f"Postings: {entry.postings}"
    )


# Feature: double-entry-ledger, Property 11: Modelagem de contas por usuário
@pytest.mark.property
@given(release_data=release_operation_command_strategy())
def test_release_operation_debits_hold_credits_available(
    release_data: tuple[CreateJournalEntryCommand, str, str],
) -> None:
    """
    **Validates: Requirements 6.1, 6.3**

    Para qualquer operação de liberação de saldo, o JournalEntry criado deve:
    1. Conter um posting de DEBIT na conta Hold do usuário
    2. Conter um posting de CREDIT na conta Available do usuário
    3. Satisfazer a regra de zero-sum

    Invariante verificada:
        Para todo JournalEntry de liberação:
        - exists posting: account_id == hold_account_id AND direction == DEBIT
        - exists posting: account_id == available_account_id AND direction == CREDIT
        - sum(signed_amount) == 0 por moeda
    """
    command, hold_account_id, available_account_id = release_data
    engine, repo = _build_engine()

    entry = engine.create_journal_entry(command)

    # Verifica que existe posting de DEBIT na conta Hold
    hold_debits = [
        p for p in entry.postings
        if p.account_id == hold_account_id and p.direction == Direction.DEBIT
    ]
    assert len(hold_debits) >= 1, (
        f"Operação de liberação deve ter DEBIT na conta Hold ({hold_account_id}). "
        f"Postings: {entry.postings}"
    )

    # Verifica que existe posting de CREDIT na conta Available
    available_credits = [
        p for p in entry.postings
        if p.account_id == available_account_id and p.direction == Direction.CREDIT
    ]
    assert len(available_credits) >= 1, (
        f"Operação de liberação deve ter CREDIT na conta Available ({available_account_id}). "
        f"Postings: {entry.postings}"
    )

    # Verifica invariante de zero-sum
    assert entry.validate_zero_sum() is True, (
        f"JournalEntry de liberação deve satisfazer zero-sum. "
        f"Postings: {entry.postings}"
    )


# Feature: double-entry-ledger, Property 11: Modelagem de contas por usuário
@pytest.mark.property
@given(
    hold_data=hold_operation_command_strategy(),
    release_data=release_operation_command_strategy(),
)
def test_hold_and_release_round_trip_zero_sum(
    hold_data: tuple[CreateJournalEntryCommand, str, str],
    release_data: tuple[CreateJournalEntryCommand, str, str],
) -> None:
    """
    **Validates: Requirements 6.1, 6.2, 6.3**

    Para qualquer par de operações hold + release com o mesmo valor,
    a soma combinada dos postings de ambos os lançamentos deve ser zero
    por moeda (propriedade de round-trip).

    Esta propriedade garante que bloquear e liberar o mesmo valor
    não altera o saldo líquido do usuário.

    Invariante verificada:
        sum(signed_amount for all postings in hold + release) == 0 por moeda
        (quando hold e release têm o mesmo amount e currency)
    """
    hold_command, hold_available_id, hold_hold_id = hold_data
    release_command, release_hold_id, release_available_id = release_data

    # Para garantir round-trip, usamos o mesmo amount e currency
    # Reconstruímos os comandos com os mesmos valores para o teste de round-trip
    amount = hold_command.postings[0].amount
    currency = hold_command.postings[0].currency

    # Cria um par de hold + release com o mesmo amount/currency
    hold_cmd = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            PostingInput(account_id="acc_available_user1", amount=amount, currency=currency, direction="DEBIT"),
            PostingInput(account_id="acc_hold_user1", amount=amount, currency=currency, direction="CREDIT"),
        ],
        metadata={},
    )
    release_cmd = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            PostingInput(account_id="acc_hold_user1", amount=amount, currency=currency, direction="DEBIT"),
            PostingInput(account_id="acc_available_user1", amount=amount, currency=currency, direction="CREDIT"),
        ],
        metadata={},
    )

    engine, repo = _build_engine()
    hold_entry = engine.create_journal_entry(hold_cmd)
    release_entry = engine.create_journal_entry(release_cmd)

    # Calcula soma combinada de todos os postings (hold + release) por moeda
    combined_sum: dict[str, int] = {}
    for posting in hold_entry.postings:
        c = posting.money.currency
        combined_sum[c] = combined_sum.get(c, 0) + posting.signed_amount
    for posting in release_entry.postings:
        c = posting.money.currency
        combined_sum[c] = combined_sum.get(c, 0) + posting.signed_amount

    for c, total in combined_sum.items():
        assert total == 0, (
            f"Round-trip hold + release deve resultar em soma zero para moeda {c}. "
            f"Total: {total}"
        )
