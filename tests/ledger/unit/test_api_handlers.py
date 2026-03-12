"""
Testes unitários para a camada de API do Double-Entry Ledger.

Cobre:
- SchemaValidator: validação de payloads para POST /entries e POST /reversals
- WriteHandler: fluxo completo de criação de entries e reversals
- ReadHandler: consulta de saldo e extrato com paginação
- Tradução de cada DomainError para HTTP response estruturada

Requisitos validados: 16.1, 16.2, 16.3
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest

from ledger.api.read_handler import handle_get_balance, handle_get_statement
from ledger.api.schema_validator import (
    validate_create_entry_payload,
    validate_create_reversal_payload,
)
from ledger.api.write_handler import handle_create_entry, handle_create_reversal
from ledger.application.handlers import CommandHandler, QueryHandler
from ledger.domain.errors import (
    IdempotencyConflict,
    InvalidAmountType,
    JournalEntryNotFound,
    OptimisticLockConflict,
    TransactionLimitExceeded,
    TransactionSizeExceeded,
    ZeroSumViolation,
)
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.ports import StatementPage
from ledger.domain.value_objects import Balance, Direction, Money, Posting
from ledger.application.commands import CreateJournalEntryCommand, PostingInput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_journal_entry(amount: int = 1000, currency: str = "BRL"):
    """Cria um JournalEntry via factory para uso nos testes."""
    factory = JournalEntryFactory()
    command = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            PostingInput(account_id="acc_a", amount=amount, currency=currency, direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=amount, currency=currency, direction="CREDIT"),
        ],
        metadata={},
    )
    return factory.create_standard(command)


def _make_event(body=None, path_params=None, query_params=None):
    """Constrói evento Lambda simulando API Gateway proxy integration."""
    return {
        "body": json.dumps(body) if body is not None else None,
        "pathParameters": path_params or {},
        "queryStringParameters": query_params or {},
        "requestContext": {"requestId": "req-test-001"},
    }


def _body(response):
    """Desserializa o body JSON da resposta do handler."""
    return json.loads(response["body"])


def _valid_entry_payload(external_id=None, amount=1000):
    """Payload válido para POST /entries."""
    return {
        "external_id": external_id or str(uuid.uuid4()),
        "postings": [
            {"account_id": "acc_a", "amount": amount, "currency": "BRL", "direction": "DEBIT"},
            {"account_id": "acc_b", "amount": amount, "currency": "BRL", "direction": "CREDIT"},
        ],
        "metadata": {"order_id": "order-001"},
    }


def _valid_reversal_payload(original_entry_id=None):
    """Payload válido para POST /reversals."""
    return {
        "original_entry_id": original_entry_id or str(uuid.uuid4()),
        "external_id": str(uuid.uuid4()),
        "metadata": {"reason": "test"},
    }


# ===========================================================================
# 1. SchemaValidator — POST /entries
# ===========================================================================


@pytest.mark.unit
class TestSchemaValidatorCreateEntry:
    """Testa validate_create_entry_payload para POST /entries."""

    def test_valid_payload_passes(self):
        result = validate_create_entry_payload(_valid_entry_payload())
        assert result.is_valid is True
        assert result.errors == []

    def test_missing_external_id_fails(self):
        payload = _valid_entry_payload()
        del payload["external_id"]
        result = validate_create_entry_payload(payload)
        assert result.is_valid is False
        assert any("external_id" in e for e in result.errors)

    def test_missing_postings_fails(self):
        payload = _valid_entry_payload()
        del payload["postings"]
        result = validate_create_entry_payload(payload)
        assert result.is_valid is False
        assert any("postings" in e for e in result.errors)

    def test_empty_postings_list_fails(self):
        payload = _valid_entry_payload()
        payload["postings"] = []
        result = validate_create_entry_payload(payload)
        assert result.is_valid is False

    def test_float_amount_fails(self):
        payload = _valid_entry_payload()
        payload["postings"][0]["amount"] = 10.50
        result = validate_create_entry_payload(payload)
        assert result.is_valid is False
        assert any("float" in e for e in result.errors)

    def test_bool_amount_fails(self):
        payload = _valid_entry_payload()
        payload["postings"][0]["amount"] = True
        result = validate_create_entry_payload(payload)
        assert result.is_valid is False
        assert any("bool" in e for e in result.errors)

    def test_string_amount_fails(self):
        payload = _valid_entry_payload()
        payload["postings"][0]["amount"] = "1000"
        result = validate_create_entry_payload(payload)
        assert result.is_valid is False

    def test_invalid_direction_fails(self):
        payload = _valid_entry_payload()
        payload["postings"][0]["direction"] = "INVALID"
        result = validate_create_entry_payload(payload)
        assert result.is_valid is False
        assert any("direction" in e for e in result.errors)

    def test_invalid_currency_length_fails(self):
        payload = _valid_entry_payload()
        payload["postings"][0]["currency"] = "BR"
        result = validate_create_entry_payload(payload)
        assert result.is_valid is False
        assert any("currency" in e for e in result.errors)

    def test_missing_account_id_in_posting_fails(self):
        payload = _valid_entry_payload()
        del payload["postings"][0]["account_id"]
        result = validate_create_entry_payload(payload)
        assert result.is_valid is False

    def test_metadata_not_dict_fails(self):
        payload = _valid_entry_payload()
        payload["metadata"] = "not a dict"
        result = validate_create_entry_payload(payload)
        assert result.is_valid is False

    def test_metadata_optional(self):
        payload = _valid_entry_payload()
        del payload["metadata"]
        result = validate_create_entry_payload(payload)
        assert result.is_valid is True

    def test_non_dict_payload_fails(self):
        result = validate_create_entry_payload("not a dict")
        assert result.is_valid is False

    def test_multiple_postings_valid(self):
        payload = _valid_entry_payload()
        payload["postings"].append(
            {"account_id": "acc_c", "amount": 500, "currency": "USD", "direction": "DEBIT"}
        )
        result = validate_create_entry_payload(payload)
        assert result.is_valid is True


# ===========================================================================
# 2. SchemaValidator — POST /reversals
# ===========================================================================


@pytest.mark.unit
class TestSchemaValidatorCreateReversal:
    """Testa validate_create_reversal_payload para POST /reversals."""

    def test_valid_payload_passes(self):
        result = validate_create_reversal_payload(_valid_reversal_payload())
        assert result.is_valid is True

    def test_missing_original_entry_id_fails(self):
        payload = _valid_reversal_payload()
        del payload["original_entry_id"]
        result = validate_create_reversal_payload(payload)
        assert result.is_valid is False
        assert any("original_entry_id" in e for e in result.errors)

    def test_missing_external_id_fails(self):
        payload = _valid_reversal_payload()
        del payload["external_id"]
        result = validate_create_reversal_payload(payload)
        assert result.is_valid is False
        assert any("external_id" in e for e in result.errors)

    def test_empty_original_entry_id_fails(self):
        payload = _valid_reversal_payload()
        payload["original_entry_id"] = "   "
        result = validate_create_reversal_payload(payload)
        assert result.is_valid is False

    def test_metadata_optional(self):
        payload = _valid_reversal_payload()
        del payload["metadata"]
        result = validate_create_reversal_payload(payload)
        assert result.is_valid is True

    def test_non_dict_payload_fails(self):
        result = validate_create_reversal_payload(42)
        assert result.is_valid is False


# ===========================================================================
# 3. WriteHandler — POST /entries
# ===========================================================================


@pytest.mark.unit
class TestWriteHandlerCreateEntry:
    """Testa handle_create_entry para POST /entries."""

    def test_valid_payload_returns_201(self):
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_journal_entry.return_value = _make_journal_entry()

        response = handle_create_entry(_make_event(body=_valid_entry_payload()), {}, mock_handler)

        assert response["statusCode"] == 201
        body = _body(response)
        assert body["status"] == "success"
        assert "entry_id" in body["data"]
        assert "postings" in body["data"]

    def test_response_contains_all_entry_fields(self):
        mock_handler = MagicMock(spec=CommandHandler)
        entry = _make_journal_entry(amount=2500, currency="USD")
        mock_handler.handle_create_journal_entry.return_value = entry

        response = handle_create_entry(_make_event(body=_valid_entry_payload()), {}, mock_handler)
        data = _body(response)["data"]

        assert data["entry_id"] == entry.entry_id
        assert data["external_id"] == entry.external_id
        assert data["entry_type"] == "STANDARD"
        assert len(data["postings"]) == 2
        assert data["timestamp"] == entry.timestamp

    def test_missing_body_returns_400(self):
        mock_handler = MagicMock(spec=CommandHandler)
        event = _make_event()  # body=None

        response = handle_create_entry(event, {}, mock_handler)

        assert response["statusCode"] == 400
        mock_handler.handle_create_journal_entry.assert_not_called()

    def test_invalid_json_body_returns_400(self):
        mock_handler = MagicMock(spec=CommandHandler)
        event = {"body": "not valid json", "pathParameters": {}, "queryStringParameters": {}, "requestContext": {}}

        response = handle_create_entry(event, {}, mock_handler)

        assert response["statusCode"] == 400
        body = _body(response)
        assert "error" in body

    def test_float_amount_returns_400_without_calling_handler(self):
        mock_handler = MagicMock(spec=CommandHandler)
        payload = _valid_entry_payload()
        payload["postings"][0]["amount"] = 10.50

        response = handle_create_entry(_make_event(body=payload), {}, mock_handler)

        assert response["statusCode"] == 400
        body = _body(response)
        assert body["error"]["code"] == "SCHEMA_VALIDATION_ERROR"
        mock_handler.handle_create_journal_entry.assert_not_called()

    def test_idempotency_conflict_returns_200_with_existing_entry_id(self):
        existing_id = str(uuid.uuid4())
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_journal_entry.side_effect = IdempotencyConflict(
            external_id="ext-001", existing_entry_id=existing_id
        )

        response = handle_create_entry(_make_event(body=_valid_entry_payload()), {}, mock_handler)

        assert response["statusCode"] == 200
        body = _body(response)
        assert body["status"] == "success"
        assert body["data"]["entry_id"] == existing_id
        assert body["data"]["idempotent"] is True

    def test_response_has_content_type_header(self):
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_journal_entry.return_value = _make_journal_entry()

        response = handle_create_entry(_make_event(body=_valid_entry_payload()), {}, mock_handler)

        assert response["headers"]["Content-Type"] == "application/json"


# ===========================================================================
# 4. WriteHandler — DomainError translation
# ===========================================================================


@pytest.mark.unit
class TestWriteHandlerDomainErrorTranslation:
    """
    Testa a tradução de cada DomainError para HTTP response estruturada.

    Cada erro de domínio deve ser mapeado para o HTTP status correto
    com o código de erro estruturado no body.
    """

    @pytest.mark.parametrize("error,expected_status,expected_code", [
        (
            ZeroSumViolation(currency="BRL", total=500),
            400,
            "ZERO_SUM_VIOLATION",
        ),
        (
            InvalidAmountType(received_type="float"),
            400,
            "INVALID_AMOUNT_TYPE",
        ),
        (
            TransactionLimitExceeded(item_count=101),
            400,
            "TRANSACTION_LIMIT_EXCEEDED",
        ),
        (
            TransactionSizeExceeded(size_bytes=5_000_000),
            400,
            "TRANSACTION_SIZE_EXCEEDED",
        ),
        (
            OptimisticLockConflict(account_id="acc_001", expected_version=3),
            409,
            "OPTIMISTIC_LOCK_CONFLICT",
        ),
    ])
    def test_domain_error_mapped_to_correct_http_status(self, error, expected_status, expected_code):
        """Cada DomainError deve ser mapeado para o HTTP status e código corretos."""
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_journal_entry.side_effect = error

        response = handle_create_entry(_make_event(body=_valid_entry_payload()), {}, mock_handler)

        assert response["statusCode"] == expected_status
        body = _body(response)
        assert "error" in body
        assert body["error"]["code"] == expected_code
        assert isinstance(body["error"]["message"], str)
        assert len(body["error"]["message"]) > 0

    def test_unexpected_exception_returns_500(self):
        """Exceções inesperadas devem retornar HTTP 500 sem expor detalhes internos."""
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_journal_entry.side_effect = RuntimeError("unexpected")

        response = handle_create_entry(_make_event(body=_valid_entry_payload()), {}, mock_handler)

        assert response["statusCode"] == 500
        body = _body(response)
        assert body["error"]["code"] == "INTERNAL_ERROR"
        # Não deve expor detalhes do erro interno
        assert "unexpected" not in body["error"]["message"]


# ===========================================================================
# 5. WriteHandler — POST /reversals
# ===========================================================================


@pytest.mark.unit
class TestWriteHandlerCreateReversal:
    """Testa handle_create_reversal para POST /reversals."""

    def test_valid_payload_returns_201(self):
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_reversal.return_value = _make_journal_entry()

        response = handle_create_reversal(_make_event(body=_valid_reversal_payload()), {}, mock_handler)

        assert response["statusCode"] == 201
        body = _body(response)
        assert body["status"] == "success"
        assert "entry_id" in body["data"]

    def test_missing_original_entry_id_returns_400(self):
        mock_handler = MagicMock(spec=CommandHandler)
        payload = _valid_reversal_payload()
        del payload["original_entry_id"]

        response = handle_create_reversal(_make_event(body=payload), {}, mock_handler)

        assert response["statusCode"] == 400
        mock_handler.handle_create_reversal.assert_not_called()

    def test_journal_entry_not_found_returns_404(self):
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_reversal.side_effect = JournalEntryNotFound(
            entry_id="nonexistent-id"
        )

        response = handle_create_reversal(_make_event(body=_valid_reversal_payload()), {}, mock_handler)

        assert response["statusCode"] == 404
        body = _body(response)
        assert body["error"]["code"] == "JOURNAL_ENTRY_NOT_FOUND"

    def test_idempotency_on_reversal_returns_200(self):
        existing_id = str(uuid.uuid4())
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_reversal.side_effect = IdempotencyConflict(
            external_id="rev-ext-001", existing_entry_id=existing_id
        )

        response = handle_create_reversal(_make_event(body=_valid_reversal_payload()), {}, mock_handler)

        assert response["statusCode"] == 200
        body = _body(response)
        assert body["data"]["idempotent"] is True
        assert body["data"]["entry_id"] == existing_id

    def test_optimistic_lock_conflict_returns_409(self):
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_reversal.side_effect = OptimisticLockConflict(
            account_id="acc_001", expected_version=5
        )

        response = handle_create_reversal(_make_event(body=_valid_reversal_payload()), {}, mock_handler)

        assert response["statusCode"] == 409
        body = _body(response)
        assert body["error"]["code"] == "OPTIMISTIC_LOCK_CONFLICT"


# ===========================================================================
# 6. ReadHandler — GET /balances/{account_id}
# ===========================================================================


@pytest.mark.unit
class TestReadHandlerGetBalance:
    """Testa handle_get_balance para GET /balances/{account_id}."""

    def test_existing_balance_returns_200_with_data(self):
        mock_handler = MagicMock(spec=QueryHandler)
        mock_handler.handle_get_balance.return_value = Balance(
            account_id="acc_available_001",
            currency="BRL",
            balance_amount=5000,
            version=3,
            last_update="2026-03-10T14:00:00Z",
        )

        event = _make_event(
            path_params={"account_id": "acc_available_001"},
            query_params={"currency": "BRL"},
        )
        response = handle_get_balance(event, {}, mock_handler)

        assert response["statusCode"] == 200
        body = _body(response)
        assert body["status"] == "success"
        data = body["data"]
        assert data["account_id"] == "acc_available_001"
        assert data["currency"] == "BRL"
        assert data["balance_amount"] == 5000
        assert data["version"] == 3

    def test_nonexistent_balance_returns_200_with_null_data(self):
        mock_handler = MagicMock(spec=QueryHandler)
        mock_handler.handle_get_balance.return_value = None

        event = _make_event(
            path_params={"account_id": "acc_nonexistent"},
            query_params={"currency": "BRL"},
        )
        response = handle_get_balance(event, {}, mock_handler)

        assert response["statusCode"] == 200
        body = _body(response)
        assert body["status"] == "success"
        assert body["data"] is None

    def test_missing_account_id_returns_400(self):
        mock_handler = MagicMock(spec=QueryHandler)
        event = _make_event(path_params={}, query_params={"currency": "BRL"})

        response = handle_get_balance(event, {}, mock_handler)

        assert response["statusCode"] == 400
        mock_handler.handle_get_balance.assert_not_called()

    def test_missing_currency_returns_400(self):
        mock_handler = MagicMock(spec=QueryHandler)
        event = _make_event(
            path_params={"account_id": "acc_001"},
            query_params={},
        )

        response = handle_get_balance(event, {}, mock_handler)

        assert response["statusCode"] == 400
        mock_handler.handle_get_balance.assert_not_called()

    def test_invalid_currency_length_returns_400(self):
        mock_handler = MagicMock(spec=QueryHandler)
        event = _make_event(
            path_params={"account_id": "acc_001"},
            query_params={"currency": "BR"},  # 2 chars, inválido
        )

        response = handle_get_balance(event, {}, mock_handler)

        assert response["statusCode"] == 400

    def test_query_handler_called_with_correct_params(self):
        mock_handler = MagicMock(spec=QueryHandler)
        mock_handler.handle_get_balance.return_value = None

        event = _make_event(
            path_params={"account_id": "acc_001"},
            query_params={"currency": "usd"},  # lowercase — deve ser normalizado para uppercase
        )
        handle_get_balance(event, {}, mock_handler)

        call_args = mock_handler.handle_get_balance.call_args[0][0]
        assert call_args.account_id == "acc_001"
        assert call_args.currency == "USD"  # normalizado para uppercase


# ===========================================================================
# 7. ReadHandler — GET /statements/{account_id}
# ===========================================================================


@pytest.mark.unit
class TestReadHandlerGetStatement:
    """Testa handle_get_statement para GET /statements/{account_id}."""

    def test_returns_200_with_postings(self):
        postings = [
            Posting(
                account_id="acc_001",
                money=Money(amount=1000, currency="BRL"),
                direction=Direction.DEBIT,
                index=0,
            )
        ]
        mock_handler = MagicMock(spec=QueryHandler)
        mock_handler.handle_get_statement.return_value = StatementPage(
            postings=postings,
            next_cursor="POSTING#2026-03-10T14:00:00Z#uuid-abc#0",
            has_more=True,
        )

        event = _make_event(
            path_params={"account_id": "acc_001"},
            query_params={"page_size": "10"},
        )
        response = handle_get_statement(event, {}, mock_handler)

        assert response["statusCode"] == 200
        body = _body(response)
        assert body["status"] == "success"
        data = body["data"]
        assert len(data["postings"]) == 1
        assert data["postings"][0]["account_id"] == "acc_001"
        assert data["postings"][0]["amount"] == 1000
        assert data["postings"][0]["direction"] == "DEBIT"
        assert data["has_more"] is True
        assert data["next_cursor"] == "POSTING#2026-03-10T14:00:00Z#uuid-abc#0"

    def test_empty_statement_returns_200(self):
        mock_handler = MagicMock(spec=QueryHandler)
        mock_handler.handle_get_statement.return_value = StatementPage(
            postings=[], next_cursor=None, has_more=False
        )

        event = _make_event(
            path_params={"account_id": "acc_001"},
            query_params={},
        )
        response = handle_get_statement(event, {}, mock_handler)

        assert response["statusCode"] == 200
        body = _body(response)
        data = body["data"]
        assert data["postings"] == []
        assert data["has_more"] is False
        assert data["next_cursor"] is None

    def test_missing_account_id_returns_400(self):
        mock_handler = MagicMock(spec=QueryHandler)
        event = _make_event(path_params={}, query_params={})

        response = handle_get_statement(event, {}, mock_handler)

        assert response["statusCode"] == 400
        mock_handler.handle_get_statement.assert_not_called()

    def test_cursor_passed_to_query_handler(self):
        cursor = "POSTING#2026-03-10T14:00:00Z#uuid-abc#0"
        mock_handler = MagicMock(spec=QueryHandler)
        mock_handler.handle_get_statement.return_value = StatementPage(
            postings=[], next_cursor=None, has_more=False
        )

        event = _make_event(
            path_params={"account_id": "acc_001"},
            query_params={"cursor": cursor, "page_size": "5"},
        )
        handle_get_statement(event, {}, mock_handler)

        call_args = mock_handler.handle_get_statement.call_args[0][0]
        assert call_args.cursor == cursor
        assert call_args.page_size == 5

    def test_default_page_size_is_20(self):
        mock_handler = MagicMock(spec=QueryHandler)
        mock_handler.handle_get_statement.return_value = StatementPage(
            postings=[], next_cursor=None, has_more=False
        )

        event = _make_event(
            path_params={"account_id": "acc_001"},
            query_params={},  # sem page_size
        )
        handle_get_statement(event, {}, mock_handler)

        call_args = mock_handler.handle_get_statement.call_args[0][0]
        assert call_args.page_size == 20

    def test_page_size_above_max_returns_400(self):
        mock_handler = MagicMock(spec=QueryHandler)
        event = _make_event(
            path_params={"account_id": "acc_001"},
            query_params={"page_size": "101"},  # acima do máximo de 100
        )

        response = handle_get_statement(event, {}, mock_handler)

        assert response["statusCode"] == 400
        mock_handler.handle_get_statement.assert_not_called()

    def test_invalid_page_size_returns_400(self):
        mock_handler = MagicMock(spec=QueryHandler)
        event = _make_event(
            path_params={"account_id": "acc_001"},
            query_params={"page_size": "not_a_number"},
        )

        response = handle_get_statement(event, {}, mock_handler)

        assert response["statusCode"] == 400

    def test_response_includes_metadata_with_account_id(self):
        mock_handler = MagicMock(spec=QueryHandler)
        mock_handler.handle_get_statement.return_value = StatementPage(
            postings=[], next_cursor=None, has_more=False
        )

        event = _make_event(
            path_params={"account_id": "acc_001"},
            query_params={},
        )
        response = handle_get_statement(event, {}, mock_handler)

        body = _body(response)
        assert body["metadata"]["account_id"] == "acc_001"

    def test_unexpected_exception_returns_500(self):
        mock_handler = MagicMock(spec=QueryHandler)
        mock_handler.handle_get_statement.side_effect = RuntimeError("db connection failed")

        event = _make_event(
            path_params={"account_id": "acc_001"},
            query_params={},
        )
        response = handle_get_statement(event, {}, mock_handler)

        assert response["statusCode"] == 500
        body = _body(response)
        assert body["error"]["code"] == "INTERNAL_ERROR"
