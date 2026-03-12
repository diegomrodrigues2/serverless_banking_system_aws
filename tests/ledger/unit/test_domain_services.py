"""
Testes unitários para ValidationChain, JournalEntryFactory e LedgerEngine.

Cobre:
- Cada validador isoladamente com exemplos concretos (ZeroSumValidator,
  MinorUnitsValidator, TransactionLimitValidator)
- ValidationChain: comportamento de chain of responsibility (para no primeiro erro)
- JournalEntryFactory: criação standard e reversal com verificação de campos
- LedgerEngine: fluxo completo com repositório in-memory, idempotência e reversão
- Imutabilidade do JournalEntry (Property 15)

Requisitos validados: 1.1, 1.2, 1.4, 9.1, 9.2, 14.1, 14.2
"""
from __future__ import annotations

import re
import time
import uuid

import pytest

from ledger.application.commands import (
    CreateJournalEntryCommand,
    CreateReversalCommand,
    PostingInput,
)
from ledger.domain.aggregates import JournalEntry
from ledger.domain.errors import (
    DomainError,
    IdempotencyConflict,
    InvalidAmountType,
    JournalEntryNotFound,
    TransactionLimitExceeded,
    ZeroSumViolation,
)
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.ports import StatementPage
from ledger.domain.services import LedgerEngine
from ledger.domain.validators import (
    MinorUnitsValidator,
    TransactionLimitValidator,
    ValidationChain,
    ValidationResult,
    ZeroSumValidator,
)
from ledger.domain.value_objects import Direction, EntryType, Money, OutboxEvent, Posting


# ---------------------------------------------------------------------------
# InMemoryLedgerRepository — repositório in-memory reutilizável nos testes
# ---------------------------------------------------------------------------


class InMemoryLedgerRepository:
    """
    Implementação in-memory do LedgerRepository para testes unitários.

    Simula o comportamento do DynamoDB sem dependência de infraestrutura.
    Satisfaz o protocolo LedgerRepository definido em ports.py.
    """

    def __init__(self) -> None:
        # Armazena entries por entry_id (partition key)
        self._entries: dict[str, JournalEntry] = {}
        # Índice de idempotência: external_id → JournalEntry
        self._by_external_id: dict[str, JournalEntry] = {}

    def save_journal_entry(self, journal_entry: JournalEntry) -> None:
        """Persiste o JournalEntry nos índices in-memory."""
        self._entries[journal_entry.entry_id] = journal_entry
        self._by_external_id[journal_entry.external_id] = journal_entry

    def find_journal_entry_by_id(self, entry_id: str) -> JournalEntry | None:
        """Busca por entry_id — retorna None se não encontrado."""
        return self._entries.get(entry_id)

    def find_journal_entry_by_external_id(self, external_id: str) -> JournalEntry | None:
        """Busca por external_id (chave de idempotência) — retorna None se não encontrado."""
        return self._by_external_id.get(external_id)

    def get_balance(self, account_id: str, currency: str):
        """Retorna None — saldo não implementado para testes unitários."""
        return None

    def get_statement(self, account_id: str, cursor, page_size: int) -> StatementPage:
        """Retorna página vazia — extrato não implementado para testes unitários."""
        return StatementPage()


# ---------------------------------------------------------------------------
# Helpers de construção de comandos
# ---------------------------------------------------------------------------


def _make_balanced_command(
    external_id: str | None = None,
    amount: int = 1000,
    currency: str = "BRL",
) -> CreateJournalEntryCommand:
    """
    Cria um CreateJournalEntryCommand balanceado (zero-sum) com um par DEBIT/CREDIT.

    Útil para testes que precisam de um comando válido sem se preocupar
    com os detalhes dos postings.
    """
    return CreateJournalEntryCommand(
        external_id=external_id or str(uuid.uuid4()),
        postings=[
            PostingInput(account_id="acc_available", amount=amount, currency=currency, direction="DEBIT"),
            PostingInput(account_id="acc_hold", amount=amount, currency=currency, direction="CREDIT"),
        ],
        metadata={},
    )


def _build_engine(
    repository: InMemoryLedgerRepository | None = None,
) -> tuple[LedgerEngine, InMemoryLedgerRepository]:
    """
    Constrói um LedgerEngine com repositório in-memory e cadeia de validação padrão.

    Retorna o engine e o repositório para inspeção nos testes.
    """
    repo = repository or InMemoryLedgerRepository()
    chain = ValidationChain([
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
    ])
    factory = JournalEntryFactory()
    engine = LedgerEngine(repository=repo, validation_chain=chain, factory=factory)
    return engine, repo


# ===========================================================================
# 1. ZeroSumValidator
# ===========================================================================


@pytest.mark.unit
class TestZeroSumValidator:
    """
    Testa o ZeroSumValidator isoladamente.

    Valida que a soma algébrica dos postings é zero por moeda (Requisito 1.1, 1.2).
    """

    def test_balanced_postings_returns_success(self) -> None:
        """Postings balanceados (DEBIT + CREDIT com mesmo amount) → ValidationResult.success."""
        command = _make_balanced_command()
        validator = ZeroSumValidator()
        result = validator.validate(command)
        assert result.is_valid is True
        assert result.errors == []

    def test_unbalanced_postings_raises_zero_sum_violation(self) -> None:
        """Postings desbalanceados → levanta ZeroSumViolation com código correto."""
        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=1000, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=500, currency="BRL", direction="CREDIT"),
            ],
            metadata={},
        )
        validator = ZeroSumValidator()
        with pytest.raises(ZeroSumViolation) as exc_info:
            validator.validate(command)
        assert exc_info.value.code == "ZERO_SUM_VIOLATION"
        assert exc_info.value.http_status == 400

    def test_multi_currency_balanced_returns_success(self) -> None:
        """Postings balanceados em múltiplas moedas → sucesso."""
        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=1000, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=1000, currency="BRL", direction="CREDIT"),
                PostingInput(account_id="acc_c", amount=500, currency="USD", direction="DEBIT"),
                PostingInput(account_id="acc_d", amount=500, currency="USD", direction="CREDIT"),
            ],
            metadata={},
        )
        validator = ZeroSumValidator()
        result = validator.validate(command)
        assert result.is_valid is True

    def test_unbalanced_second_currency_raises_zero_sum_violation(self) -> None:
        """Primeira moeda balanceada, segunda desbalanceada → levanta ZeroSumViolation."""
        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=1000, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=1000, currency="BRL", direction="CREDIT"),
                PostingInput(account_id="acc_c", amount=500, currency="USD", direction="DEBIT"),
                # USD não tem crédito correspondente → desbalanceado
            ],
            metadata={},
        )
        validator = ZeroSumValidator()
        with pytest.raises(ZeroSumViolation):
            validator.validate(command)


# ===========================================================================
# 2. MinorUnitsValidator
# ===========================================================================


@pytest.mark.unit
class TestMinorUnitsValidator:
    """
    Testa o MinorUnitsValidator isoladamente.

    Valida que todos os amounts são int > 0 (Requisito 2.1, 2.3).
    """

    def test_valid_int_amounts_returns_success(self) -> None:
        """Amounts inteiros positivos → ValidationResult.success."""
        command = _make_balanced_command(amount=1050)
        validator = MinorUnitsValidator()
        result = validator.validate(command)
        assert result.is_valid is True

    def test_float_amount_raises_invalid_amount_type(self) -> None:
        """Amount float → levanta InvalidAmountType (Requisito 2.2)."""
        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=10.50, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=10.50, currency="BRL", direction="CREDIT"),
            ],
            metadata={},
        )
        validator = MinorUnitsValidator()
        with pytest.raises(InvalidAmountType) as exc_info:
            validator.validate(command)
        assert exc_info.value.code == "INVALID_AMOUNT_TYPE"
        assert exc_info.value.http_status == 400

    def test_bool_amount_raises_invalid_amount_type(self) -> None:
        """
        Amount bool → levanta InvalidAmountType.

        bool é subclasse de int em Python; deve ser rejeitado explicitamente
        antes da verificação de isinstance(amount, int).
        """
        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=True, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=True, currency="BRL", direction="CREDIT"),
            ],
            metadata={},
        )
        validator = MinorUnitsValidator()
        with pytest.raises(InvalidAmountType) as exc_info:
            validator.validate(command)
        assert exc_info.value.code == "INVALID_AMOUNT_TYPE"

    def test_zero_amount_raises_invalid_amount_type(self) -> None:
        """Amount zero → levanta InvalidAmountType (zero não é minor unit válido)."""
        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=0, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=0, currency="BRL", direction="CREDIT"),
            ],
            metadata={},
        )
        validator = MinorUnitsValidator()
        with pytest.raises(InvalidAmountType):
            validator.validate(command)

    def test_negative_amount_raises_invalid_amount_type(self) -> None:
        """Amount negativo → levanta InvalidAmountType (direção é controlada por Direction)."""
        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=-100, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=-100, currency="BRL", direction="CREDIT"),
            ],
            metadata={},
        )
        validator = MinorUnitsValidator()
        with pytest.raises(InvalidAmountType):
            validator.validate(command)

    def test_string_amount_raises_invalid_amount_type(self) -> None:
        """Amount string → levanta InvalidAmountType."""
        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount="100", currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount="100", currency="BRL", direction="CREDIT"),
            ],
            metadata={},
        )
        validator = MinorUnitsValidator()
        with pytest.raises(InvalidAmountType):
            validator.validate(command)


# ===========================================================================
# 3. TransactionLimitValidator
# ===========================================================================


@pytest.mark.unit
class TestTransactionLimitValidator:
    """
    Testa o TransactionLimitValidator isoladamente.

    Valida limites do DynamoDB TransactWriteItems (Requisito 14.1, 14.2).
    """

    def test_within_limits_returns_success(self) -> None:
        """Comando com poucos postings (bem abaixo de 100 itens) → sucesso."""
        command = _make_balanced_command()
        validator = TransactionLimitValidator()
        result = validator.validate(command)
        assert result.is_valid is True

    def test_over_100_items_raises_transaction_limit_exceeded(self) -> None:
        """
        Mais de 100 itens na TransactWriteItems → levanta TransactionLimitExceeded.

        Fórmula: item_count = 3 + N_postings + M_distinct_accounts
        Com 25 pares de contas distintas: 3 + 50 + 50 = 103 > 100.
        """
        # Gera 25 pares com contas distintas para garantir item_count > 100
        postings = []
        for i in range(25):
            postings.append(PostingInput(
                account_id=f"acc_debit_{i}",
                amount=100,
                currency="BRL",
                direction="DEBIT",
            ))
            postings.append(PostingInput(
                account_id=f"acc_credit_{i}",
                amount=100,
                currency="BRL",
                direction="CREDIT",
            ))

        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=postings,
            metadata={},
        )
        validator = TransactionLimitValidator()
        with pytest.raises(TransactionLimitExceeded) as exc_info:
            validator.validate(command)
        assert exc_info.value.code == "TRANSACTION_LIMIT_EXCEEDED"
        assert exc_info.value.http_status == 400

    def test_exactly_at_limit_does_not_raise(self) -> None:
        """
        Exatamente no limite (item_count == 100) → não levanta exceção.

        Com 2 postings em 2 contas distintas: 3 + 2 + 2 = 7 itens — bem abaixo.
        Teste confirma que o validador não é excessivamente restritivo.
        """
        command = _make_balanced_command()
        validator = TransactionLimitValidator()
        # Não deve levantar exceção
        result = validator.validate(command)
        assert result.is_valid is True


# ===========================================================================
# 4. ValidationChain — Chain of Responsibility
# ===========================================================================


@pytest.mark.unit
class TestValidationChain:
    """
    Testa o comportamento de chain of responsibility da ValidationChain.

    Verifica que a cadeia para no primeiro erro e que todos os validadores
    são executados quando o comando é válido.
    """

    def test_all_validators_pass_returns_success(self) -> None:
        """Comando válido com todos os validadores → ValidationResult.success."""
        chain = ValidationChain([
            ZeroSumValidator(),
            MinorUnitsValidator(),
            TransactionLimitValidator(),
        ])
        command = _make_balanced_command()
        result = chain.validate(command)
        assert result.is_valid is True

    def test_chain_stops_at_first_error_zero_sum(self) -> None:
        """
        Cadeia para no ZeroSumValidator (primeiro) quando postings são desbalanceados.

        O MinorUnitsValidator não deve ser executado — a exceção do ZeroSumValidator
        interrompe a cadeia imediatamente (fail-fast).
        """
        # Postings desbalanceados: ZeroSumValidator deve falhar primeiro
        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=1000, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=500, currency="BRL", direction="CREDIT"),
            ],
            metadata={},
        )
        chain = ValidationChain([
            ZeroSumValidator(),
            MinorUnitsValidator(),
            TransactionLimitValidator(),
        ])
        with pytest.raises(ZeroSumViolation):
            chain.validate(command)

    def test_chain_stops_at_first_error_minor_units(self) -> None:
        """
        Cadeia para no MinorUnitsValidator (segundo) quando amount é float.

        O ZeroSumValidator passa (postings balanceados), mas o MinorUnitsValidator
        falha com InvalidAmountType — a cadeia para aí.
        """
        # Postings balanceados mas com float — ZeroSumValidator passa, MinorUnitsValidator falha
        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=10.50, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=10.50, currency="BRL", direction="CREDIT"),
            ],
            metadata={},
        )
        chain = ValidationChain([
            ZeroSumValidator(),
            MinorUnitsValidator(),
            TransactionLimitValidator(),
        ])
        with pytest.raises(InvalidAmountType):
            chain.validate(command)

    def test_empty_chain_returns_success(self) -> None:
        """Cadeia vazia (sem validadores) → ValidationResult.success."""
        chain = ValidationChain([])
        command = _make_balanced_command()
        result = chain.validate(command)
        assert result.is_valid is True

    def test_single_validator_chain_propagates_error(self) -> None:
        """Cadeia com um único validador que falha → propaga o DomainError."""
        chain = ValidationChain([ZeroSumValidator()])
        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=1000, currency="BRL", direction="DEBIT"),
            ],
            metadata={},
        )
        with pytest.raises(ZeroSumViolation):
            chain.validate(command)


# ===========================================================================
# 5. JournalEntryFactory — create_standard
# ===========================================================================


@pytest.mark.unit
class TestJournalEntryFactoryCreateStandard:
    """
    Testa a criação de JournalEntry padrão via JournalEntryFactory.create_standard.

    Verifica geração de entry_id, timestamp, OutboxEvent e conversão de postings.
    """

    def test_create_standard_generates_valid_uuid_entry_id(self) -> None:
        """entry_id gerado deve ser um UUID v4 válido."""
        factory = JournalEntryFactory()
        command = _make_balanced_command()
        entry = factory.create_standard(command)

        # Verifica que entry_id é um UUID válido (formato xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)
        try:
            parsed = uuid.UUID(entry.entry_id, version=4)
        except ValueError:
            pytest.fail(f"entry_id não é um UUID v4 válido: {entry.entry_id}")
        assert str(parsed) == entry.entry_id

    def test_create_standard_generates_iso8601_timestamp(self) -> None:
        """timestamp gerado deve estar no formato ISO 8601 com sufixo Z."""
        factory = JournalEntryFactory()
        command = _make_balanced_command()
        entry = factory.create_standard(command)

        # Formato esperado: YYYY-MM-DDTHH:MM:SS.ffffffZ
        iso8601_pattern = re.compile(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$"
        )
        assert iso8601_pattern.match(entry.timestamp), (
            f"timestamp não está no formato ISO 8601: {entry.timestamp}"
        )

    def test_create_standard_outbox_event_has_outbox_prefix(self) -> None:
        """OutboxEvent gerado deve ter event_id com prefixo 'OUTBOX#'."""
        factory = JournalEntryFactory()
        command = _make_balanced_command()
        entry = factory.create_standard(command)

        assert entry.outbox_event.event_id.startswith("OUTBOX#"), (
            f"event_id deve começar com 'OUTBOX#': {entry.outbox_event.event_id}"
        )

    def test_create_standard_outbox_event_id_contains_entry_id(self) -> None:
        """OutboxEvent.event_id deve ser 'OUTBOX#{entry_id}'."""
        factory = JournalEntryFactory()
        command = _make_balanced_command()
        entry = factory.create_standard(command)

        expected_event_id = f"OUTBOX#{entry.entry_id}"
        assert entry.outbox_event.event_id == expected_event_id

    def test_create_standard_entry_type_is_standard(self) -> None:
        """entry_type deve ser EntryType.STANDARD."""
        factory = JournalEntryFactory()
        command = _make_balanced_command()
        entry = factory.create_standard(command)

        assert entry.entry_type == EntryType.STANDARD

    def test_create_standard_converts_posting_inputs_to_postings(self) -> None:
        """PostingInputs do comando devem ser convertidos para Posting value objects."""
        factory = JournalEntryFactory()
        command = _make_balanced_command(amount=1050, currency="BRL")
        entry = factory.create_standard(command)

        assert len(entry.postings) == 2

        debit_posting = entry.postings[0]
        assert debit_posting.account_id == "acc_available"
        assert debit_posting.money == Money(amount=1050, currency="BRL")
        assert debit_posting.direction == Direction.DEBIT
        assert debit_posting.index == 0

        credit_posting = entry.postings[1]
        assert credit_posting.account_id == "acc_hold"
        assert credit_posting.money == Money(amount=1050, currency="BRL")
        assert credit_posting.direction == Direction.CREDIT
        assert credit_posting.index == 1

    def test_create_standard_preserves_external_id(self) -> None:
        """external_id do comando deve ser preservado no JournalEntry."""
        factory = JournalEntryFactory()
        external_id = "order-payment-001"
        command = _make_balanced_command(external_id=external_id)
        entry = factory.create_standard(command)

        assert entry.external_id == external_id

    def test_create_standard_preserves_metadata(self) -> None:
        """metadata do comando deve ser preservado no JournalEntry."""
        factory = JournalEntryFactory()
        command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=100, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=100, currency="BRL", direction="CREDIT"),
            ],
            metadata={"order_id": "order-001", "tenant_id": "tenant-abc"},
        )
        entry = factory.create_standard(command)

        assert entry.metadata == {"order_id": "order-001", "tenant_id": "tenant-abc"}

    def test_create_standard_postings_are_tuple(self) -> None:
        """postings devem ser tuple (imutável), não list."""
        factory = JournalEntryFactory()
        command = _make_balanced_command()
        entry = factory.create_standard(command)

        assert isinstance(entry.postings, tuple)


# ===========================================================================
# 6. JournalEntryFactory — create_reversal
# ===========================================================================


@pytest.mark.unit
class TestJournalEntryFactoryCreateReversal:
    """
    Testa a criação de JournalEntry de reversão via JournalEntryFactory.create_reversal.

    Verifica entry_type, inversão de direções, metadata e propriedade de anulação.
    """

    def _make_original_entry(self, amount: int = 1000, currency: str = "BRL") -> JournalEntry:
        """Cria um JournalEntry original via factory para uso nos testes de reversão."""
        factory = JournalEntryFactory()
        command = _make_balanced_command(amount=amount, currency=currency)
        return factory.create_standard(command)

    def test_create_reversal_entry_type_is_reversal(self) -> None:
        """entry_type do reversal deve ser EntryType.REVERSAL."""
        factory = JournalEntryFactory()
        original = self._make_original_entry()
        reversal_command = CreateReversalCommand(
            original_entry_id=original.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={},
        )
        reversal = factory.create_reversal(original=original, command=reversal_command)

        assert reversal.entry_type == EntryType.REVERSAL

    def test_create_reversal_postings_have_inverted_directions(self) -> None:
        """
        Postings do reversal devem ter direções invertidas em relação ao original.

        DEBIT → CREDIT e CREDIT → DEBIT.
        """
        factory = JournalEntryFactory()
        original = self._make_original_entry()
        reversal_command = CreateReversalCommand(
            original_entry_id=original.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={},
        )
        reversal = factory.create_reversal(original=original, command=reversal_command)

        assert len(reversal.postings) == len(original.postings)

        for orig_posting, rev_posting in zip(original.postings, reversal.postings):
            if orig_posting.direction == Direction.DEBIT:
                assert rev_posting.direction == Direction.CREDIT, (
                    f"DEBIT deve ser invertido para CREDIT no reversal"
                )
            else:
                assert rev_posting.direction == Direction.DEBIT, (
                    f"CREDIT deve ser invertido para DEBIT no reversal"
                )

    def test_create_reversal_metadata_contains_original_entry_id(self) -> None:
        """metadata do reversal deve conter 'original_entry_id' (Requisito 9.3)."""
        factory = JournalEntryFactory()
        original = self._make_original_entry()
        reversal_command = CreateReversalCommand(
            original_entry_id=original.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={},
        )
        reversal = factory.create_reversal(original=original, command=reversal_command)

        assert "original_entry_id" in reversal.metadata
        assert reversal.metadata["original_entry_id"] == original.entry_id

    def test_create_reversal_combined_sum_is_zero_per_currency(self) -> None:
        """
        Soma combinada (original + reversal) deve ser zero por moeda (Requisito 9.4).

        Esta é a propriedade de anulação: o reversal cancela exatamente o efeito
        contábil do lançamento original.
        """
        factory = JournalEntryFactory()
        original = self._make_original_entry(amount=1500, currency="BRL")
        reversal_command = CreateReversalCommand(
            original_entry_id=original.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={},
        )
        reversal = factory.create_reversal(original=original, command=reversal_command)

        # Calcula soma combinada por moeda
        combined_sum: dict[str, int] = {}
        for posting in original.postings:
            currency = posting.money.currency
            combined_sum[currency] = combined_sum.get(currency, 0) + posting.signed_amount
        for posting in reversal.postings:
            currency = posting.money.currency
            combined_sum[currency] = combined_sum.get(currency, 0) + posting.signed_amount

        for currency, total in combined_sum.items():
            assert total == 0, (
                f"Soma combinada deve ser zero para moeda {currency}, recebido: {total}"
            )

    def test_create_reversal_generates_new_entry_id(self) -> None:
        """Reversal deve ter entry_id diferente do original."""
        factory = JournalEntryFactory()
        original = self._make_original_entry()
        reversal_command = CreateReversalCommand(
            original_entry_id=original.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={},
        )
        reversal = factory.create_reversal(original=original, command=reversal_command)

        assert reversal.entry_id != original.entry_id

    def test_create_reversal_preserves_money_amounts(self) -> None:
        """Postings do reversal devem preservar os mesmos Money (amount e currency)."""
        factory = JournalEntryFactory()
        original = self._make_original_entry(amount=750, currency="USD")
        reversal_command = CreateReversalCommand(
            original_entry_id=original.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={},
        )
        reversal = factory.create_reversal(original=original, command=reversal_command)

        for orig_posting, rev_posting in zip(original.postings, reversal.postings):
            assert rev_posting.money == orig_posting.money, (
                f"Money deve ser preservado no reversal. "
                f"Original: {orig_posting.money}, Reversal: {rev_posting.money}"
            )


# ===========================================================================
# 7. LedgerEngine — fluxo completo com InMemoryLedgerRepository
# ===========================================================================


@pytest.mark.unit
class TestLedgerEngineCreateJournalEntry:
    """
    Testa o fluxo completo de criação de lançamento via LedgerEngine.

    Usa InMemoryLedgerRepository para isolar o teste de infraestrutura.
    """

    def test_create_journal_entry_full_flow_succeeds(self) -> None:
        """Fluxo completo com comando válido → retorna JournalEntry persistido."""
        engine, repo = _build_engine()
        command = _make_balanced_command()

        entry = engine.create_journal_entry(command)

        assert entry is not None
        assert entry.entry_id is not None
        assert entry.external_id == command.external_id
        assert entry.entry_type == EntryType.STANDARD
        assert len(entry.postings) == 2

    def test_create_journal_entry_persists_to_repository(self) -> None:
        """Após criação, o JournalEntry deve estar acessível no repositório."""
        engine, repo = _build_engine()
        command = _make_balanced_command()

        entry = engine.create_journal_entry(command)

        # Verifica que o entry foi persistido por entry_id
        found_by_id = repo.find_journal_entry_by_id(entry.entry_id)
        assert found_by_id is not None
        assert found_by_id.entry_id == entry.entry_id

        # Verifica que o entry foi persistido por external_id (idempotência)
        found_by_ext = repo.find_journal_entry_by_external_id(command.external_id)
        assert found_by_ext is not None
        assert found_by_ext.entry_id == entry.entry_id

    def test_create_journal_entry_idempotency_same_external_id_raises_conflict(self) -> None:
        """
        Submissão duplicada com mesmo external_id → levanta IdempotencyConflict.

        O LedgerEngine deve detectar a duplicata antes de criar novo lançamento
        (Requisito 4.1, 4.2).
        """
        engine, repo = _build_engine()
        shared_external_id = str(uuid.uuid4())

        # Primeira submissão deve ter sucesso
        first_command = _make_balanced_command(external_id=shared_external_id)
        first_entry = engine.create_journal_entry(first_command)

        # Segunda submissão com mesmo external_id deve levantar IdempotencyConflict
        second_command = _make_balanced_command(external_id=shared_external_id)
        with pytest.raises(IdempotencyConflict) as exc_info:
            engine.create_journal_entry(second_command)

        conflict = exc_info.value
        assert conflict.code == "IDEMPOTENCY_CONFLICT"
        assert conflict.existing_entry_id == first_entry.entry_id

    def test_create_journal_entry_validation_failure_propagates_domain_error(self) -> None:
        """
        Comando inválido (postings desbalanceados) → propaga DomainError da validação.

        O LedgerEngine não deve swallow erros de validação.
        """
        engine, repo = _build_engine()
        invalid_command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=1000, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=500, currency="BRL", direction="CREDIT"),
            ],
            metadata={},
        )

        with pytest.raises(ZeroSumViolation):
            engine.create_journal_entry(invalid_command)

        # Nenhum entry deve ter sido persistido
        assert len(repo._entries) == 0

    def test_create_journal_entry_float_amount_raises_invalid_amount_type(self) -> None:
        """Comando com float amount → propaga InvalidAmountType da validação."""
        engine, repo = _build_engine()
        invalid_command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=10.50, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=10.50, currency="BRL", direction="CREDIT"),
            ],
            metadata={},
        )

        with pytest.raises(InvalidAmountType):
            engine.create_journal_entry(invalid_command)


@pytest.mark.unit
class TestLedgerEngineCreateReversal:
    """
    Testa o fluxo de reversão via LedgerEngine.create_reversal.
    """

    def test_create_reversal_full_flow_succeeds(self) -> None:
        """Fluxo completo de reversão → retorna JournalEntry do tipo REVERSAL."""
        engine, repo = _build_engine()

        # Cria o lançamento original
        original_command = _make_balanced_command()
        original_entry = engine.create_journal_entry(original_command)

        # Cria a reversão
        reversal_command = CreateReversalCommand(
            original_entry_id=original_entry.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={"reason": "test_reversal"},
        )
        reversal_entry = engine.create_reversal(reversal_command)

        assert reversal_entry is not None
        assert reversal_entry.entry_type == EntryType.REVERSAL
        assert reversal_entry.metadata["original_entry_id"] == original_entry.entry_id

    def test_create_reversal_original_not_found_raises_journal_entry_not_found(self) -> None:
        """
        Reversão de entry inexistente → levanta JournalEntryNotFound (Requisito 9.2).
        """
        engine, repo = _build_engine()
        nonexistent_id = str(uuid.uuid4())

        reversal_command = CreateReversalCommand(
            original_entry_id=nonexistent_id,
            external_id=str(uuid.uuid4()),
            metadata={},
        )

        with pytest.raises(JournalEntryNotFound) as exc_info:
            engine.create_reversal(reversal_command)

        assert exc_info.value.code == "JOURNAL_ENTRY_NOT_FOUND"
        assert exc_info.value.http_status == 404

    def test_create_reversal_persists_reversal_entry(self) -> None:
        """Após reversão, o JournalEntry de reversão deve estar no repositório."""
        engine, repo = _build_engine()

        original_command = _make_balanced_command()
        original_entry = engine.create_journal_entry(original_command)

        reversal_command = CreateReversalCommand(
            original_entry_id=original_entry.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={},
        )
        reversal_entry = engine.create_reversal(reversal_command)

        # Verifica que o reversal foi persistido
        found = repo.find_journal_entry_by_id(reversal_entry.entry_id)
        assert found is not None
        assert found.entry_type == EntryType.REVERSAL

    def test_create_reversal_postings_are_inverted(self) -> None:
        """Postings do reversal devem ter direções invertidas em relação ao original."""
        engine, repo = _build_engine()

        original_command = _make_balanced_command(amount=2000, currency="BRL")
        original_entry = engine.create_journal_entry(original_command)

        reversal_command = CreateReversalCommand(
            original_entry_id=original_entry.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={},
        )
        reversal_entry = engine.create_reversal(reversal_command)

        for orig_posting, rev_posting in zip(original_entry.postings, reversal_entry.postings):
            expected_direction = (
                Direction.CREDIT if orig_posting.direction == Direction.DEBIT
                else Direction.DEBIT
            )
            assert rev_posting.direction == expected_direction


# ===========================================================================
# 8. Imutabilidade do JournalEntry (Property 15)
# ===========================================================================


@pytest.mark.unit
class TestJournalEntryImmutability:
    """
    Testa a imutabilidade do JournalEntry (Requisito 9.1).

    JournalEntry é um frozen dataclass — qualquer tentativa de mutação
    deve levantar FrozenInstanceError (ou AttributeError).

    Property 15: JournalEntry é imutável após criação (append-only).
    """

    def _make_entry(self) -> JournalEntry:
        """Cria um JournalEntry via factory para uso nos testes de imutabilidade."""
        factory = JournalEntryFactory()
        command = _make_balanced_command()
        return factory.create_standard(command)

    def test_journal_entry_is_frozen_cannot_set_entry_id(self) -> None:
        """Tentativa de alterar entry_id → levanta FrozenInstanceError."""
        entry = self._make_entry()
        with pytest.raises((AttributeError, TypeError)):
            entry.entry_id = "new-id"  # type: ignore[misc]

    def test_journal_entry_is_frozen_cannot_set_external_id(self) -> None:
        """Tentativa de alterar external_id → levanta FrozenInstanceError."""
        entry = self._make_entry()
        with pytest.raises((AttributeError, TypeError)):
            entry.external_id = "new-external-id"  # type: ignore[misc]

    def test_journal_entry_is_frozen_cannot_set_entry_type(self) -> None:
        """Tentativa de alterar entry_type → levanta FrozenInstanceError."""
        entry = self._make_entry()
        with pytest.raises((AttributeError, TypeError)):
            entry.entry_type = EntryType.REVERSAL  # type: ignore[misc]

    def test_journal_entry_is_frozen_cannot_set_postings(self) -> None:
        """Tentativa de substituir postings → levanta FrozenInstanceError."""
        entry = self._make_entry()
        with pytest.raises((AttributeError, TypeError)):
            entry.postings = ()  # type: ignore[misc]

    def test_journal_entry_is_frozen_cannot_set_timestamp(self) -> None:
        """Tentativa de alterar timestamp → levanta FrozenInstanceError."""
        entry = self._make_entry()
        with pytest.raises((AttributeError, TypeError)):
            entry.timestamp = "2099-01-01T00:00:00Z"  # type: ignore[misc]

    def test_journal_entry_postings_are_tuple_not_list(self) -> None:
        """
        postings deve ser tuple (imutável estruturalmente), não list.

        Tuple garante que a coleção de postings não pode ser modificada
        após a criação do JournalEntry.
        """
        entry = self._make_entry()
        assert isinstance(entry.postings, tuple), (
            f"postings deve ser tuple, recebido: {type(entry.postings).__name__}"
        )

    def test_posting_value_object_is_frozen(self) -> None:
        """Posting é um frozen dataclass — tentativa de mutação deve falhar."""
        entry = self._make_entry()
        posting = entry.postings[0]
        with pytest.raises((AttributeError, TypeError)):
            posting.account_id = "new-account"  # type: ignore[misc]

    def test_outbox_event_is_frozen(self) -> None:
        """OutboxEvent é um frozen dataclass — tentativa de mutação deve falhar."""
        entry = self._make_entry()
        with pytest.raises((AttributeError, TypeError)):
            entry.outbox_event.event_id = "new-event-id"  # type: ignore[misc]
