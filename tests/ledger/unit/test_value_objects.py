"""
Testes unitários para Value Objects e hierarquia de erros do domínio.

Cobre criação válida/inválida de Money, comportamento de signed_amount
em Posting, validação de OutboxEvent e instanciação de cada DomainError
com os campos corretos.

Requisitos validados: 1.3, 2.1, 2.2, 2.3
"""
import pytest

from ledger.domain.value_objects import Direction, Money, OutboxEvent, Posting
from ledger.domain.errors import (
    DomainError,
    IdempotencyConflict,
    InvalidAmountType,
    JournalEntryNotFound,
    OptimisticLockConflict,
    TransactionLimitExceeded,
    TransactionSizeExceeded,
    ZeroSumViolation,
)


# ---------------------------------------------------------------------------
# Money — criação válida
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMoneyValidCreation:
    """Money aceita inteiros positivos com currency de 3 caracteres."""

    def test_valid_money_creation(self) -> None:
        money = Money(amount=1050, currency="BRL")
        assert money.amount == 1050
        assert money.currency == "BRL"

    def test_valid_money_is_immutable(self) -> None:
        # frozen=True — qualquer tentativa de atribuição deve falhar
        money = Money(amount=100, currency="USD")
        with pytest.raises((AttributeError, TypeError)):
            money.amount = 200  # type: ignore[misc]

    def test_valid_money_equality_by_value(self) -> None:
        # Value Objects são iguais quando os valores são iguais
        assert Money(amount=500, currency="EUR") == Money(amount=500, currency="EUR")

    def test_valid_money_inequality_different_amount(self) -> None:
        assert Money(amount=100, currency="BRL") != Money(amount=200, currency="BRL")

    def test_valid_money_inequality_different_currency(self) -> None:
        assert Money(amount=100, currency="BRL") != Money(amount=100, currency="USD")


# ---------------------------------------------------------------------------
# Money — criação inválida
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMoneyInvalidCreation:
    """Money rejeita tipos e valores que violam invariantes de domínio."""

    def test_float_amount_raises_value_error(self) -> None:
        # Floats não são aceitos para evitar erros de arredondamento
        with pytest.raises(ValueError, match="int"):
            Money(amount=10.50, currency="BRL")  # type: ignore[arg-type]

    def test_zero_amount_raises_value_error(self) -> None:
        # amount deve ser > 0; zero não representa valor monetário válido
        with pytest.raises(ValueError, match="> 0"):
            Money(amount=0, currency="BRL")

    def test_negative_amount_raises_value_error(self) -> None:
        # Direção contábil é controlada por Direction, não pelo sinal do amount
        with pytest.raises(ValueError, match="> 0"):
            Money(amount=-100, currency="BRL")

    def test_bool_amount_raises_value_error(self) -> None:
        # bool é subclasse de int em Python; deve ser rejeitado explicitamente
        with pytest.raises(ValueError, match="bool"):
            Money(amount=True, currency="BRL")  # type: ignore[arg-type]

    def test_currency_too_short_raises_value_error(self) -> None:
        # ISO 4217 exige exatamente 3 caracteres
        with pytest.raises(ValueError, match="3 caracteres"):
            Money(amount=100, currency="BR")

    def test_currency_too_long_raises_value_error(self) -> None:
        # Código de 4 caracteres não é ISO 4217 válido
        with pytest.raises(ValueError, match="3 caracteres"):
            Money(amount=100, currency="BRLX")


# ---------------------------------------------------------------------------
# Posting — signed_amount (convenção de sinais)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPostingSignedAmount:
    """
    Verifica a convenção de sinais de partidas dobradas:
    DEBIT → signed_amount positivo
    CREDIT → signed_amount negativo

    Validates: Requirements 1.3
    """

    def test_debit_posting_signed_amount_is_positive(self) -> None:
        money = Money(amount=500, currency="BRL")
        posting = Posting(account_id="acc-1", money=money, direction=Direction.DEBIT, index=0)
        assert posting.signed_amount == 500

    def test_credit_posting_signed_amount_is_negative(self) -> None:
        money = Money(amount=500, currency="BRL")
        posting = Posting(account_id="acc-2", money=money, direction=Direction.CREDIT, index=1)
        assert posting.signed_amount == -500

    def test_debit_signed_amount_equals_money_amount(self) -> None:
        money = Money(amount=1050, currency="USD")
        posting = Posting(account_id="acc-1", money=money, direction=Direction.DEBIT, index=0)
        assert posting.signed_amount == money.amount

    def test_credit_signed_amount_is_negative_money_amount(self) -> None:
        money = Money(amount=1050, currency="USD")
        posting = Posting(account_id="acc-2", money=money, direction=Direction.CREDIT, index=1)
        assert posting.signed_amount == -money.amount

    def test_abs_signed_amount_equals_money_amount_for_debit(self) -> None:
        money = Money(amount=300, currency="EUR")
        posting = Posting(account_id="acc-1", money=money, direction=Direction.DEBIT, index=0)
        assert abs(posting.signed_amount) == money.amount

    def test_abs_signed_amount_equals_money_amount_for_credit(self) -> None:
        money = Money(amount=300, currency="EUR")
        posting = Posting(account_id="acc-2", money=money, direction=Direction.CREDIT, index=1)
        assert abs(posting.signed_amount) == money.amount

    def test_debit_and_credit_same_amount_sum_to_zero(self) -> None:
        # Propriedade fundamental de partidas dobradas: débito + crédito = 0
        money = Money(amount=750, currency="BRL")
        debit = Posting(account_id="acc-1", money=money, direction=Direction.DEBIT, index=0)
        credit = Posting(account_id="acc-2", money=money, direction=Direction.CREDIT, index=1)
        assert debit.signed_amount + credit.signed_amount == 0


# ---------------------------------------------------------------------------
# OutboxEvent — validação de prefixo
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOutboxEvent:
    """
    OutboxEvent exige prefixo "OUTBOX#" no event_id para filtragem
    correta no DynamoDB Stream.

    Validates: Requirements 7.5
    """

    def test_valid_outbox_event_id_with_prefix(self) -> None:
        event = OutboxEvent(
            event_id="OUTBOX#abc-123",
            entry_id="abc-123",
            event_type="TransactionCreated",
            payload={"entry_id": "abc-123"},
            expires_at=9999999999,
        )
        assert event.event_id.startswith("OUTBOX#")

    def test_outbox_event_without_prefix_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="OUTBOX#"):
            OutboxEvent(
                event_id="abc-123",  # sem prefixo obrigatório
                entry_id="abc-123",
                event_type="TransactionCreated",
                payload={},
                expires_at=9999999999,
            )

    def test_outbox_event_empty_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="OUTBOX#"):
            OutboxEvent(
                event_id="",
                entry_id="abc-123",
                event_type="TransactionCreated",
                payload={},
                expires_at=9999999999,
            )

    def test_outbox_event_wrong_prefix_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="OUTBOX#"):
            OutboxEvent(
                event_id="EVENT#abc-123",  # prefixo errado
                entry_id="abc-123",
                event_type="TransactionCreated",
                payload={},
                expires_at=9999999999,
            )


# ---------------------------------------------------------------------------
# Erros de domínio — instanciação e campos
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDomainErrors:
    """
    Verifica que cada DomainError é instanciado com os campos corretos:
    code, http_status e campos específicos de cada subclasse.
    """

    def test_zero_sum_violation_code_and_status(self) -> None:
        error = ZeroSumViolation(currency="BRL", total=100)
        assert error.code == "ZERO_SUM_VIOLATION"
        assert error.http_status == 400
        assert isinstance(error, DomainError)

    def test_zero_sum_violation_message_contains_currency(self) -> None:
        error = ZeroSumViolation(currency="USD", total=50)
        assert "USD" in error.message

    def test_invalid_amount_type_code_and_status(self) -> None:
        error = InvalidAmountType(received_type="float")
        assert error.code == "INVALID_AMOUNT_TYPE"
        assert error.http_status == 400
        assert isinstance(error, DomainError)

    def test_invalid_amount_type_message_contains_type(self) -> None:
        error = InvalidAmountType(received_type="float")
        assert "float" in error.message

    def test_optimistic_lock_conflict_code_and_status(self) -> None:
        error = OptimisticLockConflict(account_id="acc-1", expected_version=3)
        assert error.code == "OPTIMISTIC_LOCK_CONFLICT"
        assert error.http_status == 409
        assert isinstance(error, DomainError)

    def test_idempotency_conflict_code_and_status(self) -> None:
        error = IdempotencyConflict(external_id="ext-1", existing_entry_id="entry-abc")
        assert error.code == "IDEMPOTENCY_CONFLICT"
        assert error.http_status == 200
        assert isinstance(error, DomainError)

    def test_idempotency_conflict_has_existing_entry_id(self) -> None:
        error = IdempotencyConflict(external_id="ext-1", existing_entry_id="entry-abc")
        assert error.existing_entry_id == "entry-abc"

    def test_transaction_limit_exceeded_code_and_status(self) -> None:
        error = TransactionLimitExceeded(item_count=101)
        assert error.code == "TRANSACTION_LIMIT_EXCEEDED"
        assert error.http_status == 400
        assert isinstance(error, DomainError)

    def test_transaction_size_exceeded_code_and_status(self) -> None:
        error = TransactionSizeExceeded(size_bytes=5_000_000)
        assert error.code == "TRANSACTION_SIZE_EXCEEDED"
        assert error.http_status == 400
        assert isinstance(error, DomainError)

    def test_journal_entry_not_found_code_and_status(self) -> None:
        error = JournalEntryNotFound(entry_id="entry-xyz")
        assert error.code == "JOURNAL_ENTRY_NOT_FOUND"
        assert error.http_status == 404
        assert isinstance(error, DomainError)

    def test_journal_entry_not_found_message_contains_entry_id(self) -> None:
        error = JournalEntryNotFound(entry_id="entry-xyz")
        assert "entry-xyz" in error.message

    def test_domain_errors_are_exceptions(self) -> None:
        # Todos os DomainErrors devem poder ser capturados como Exception
        errors = [
            ZeroSumViolation(currency="BRL", total=1),
            InvalidAmountType(received_type="float"),
            OptimisticLockConflict(account_id="acc-1", expected_version=0),
            IdempotencyConflict(external_id="ext-1", existing_entry_id="entry-1"),
            TransactionLimitExceeded(item_count=101),
            TransactionSizeExceeded(size_bytes=5_000_000),
            JournalEntryNotFound(entry_id="entry-1"),
        ]
        for error in errors:
            assert isinstance(error, Exception)
            assert isinstance(error, DomainError)
