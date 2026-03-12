"""
Testes de propriedade para o formato de resposta da API do Double-Entry Ledger.

Property 20: Formato de resposta da API

Valida que os contratos de resposta da API são respeitados para qualquer
entrada válida gerada pelo Hypothesis:

- Respostas de sucesso SEMPRE seguem o formato:
  {"status": "success", "data": <qualquer>, "metadata": <dict>}

- Respostas de erro SEMPRE seguem o formato:
  {"error": {"code": <str>, "message": <str>}}

- Handlers de escrita retornam statusCode correto para cada cenário
- Handlers de leitura retornam statusCode correto para cada cenário
- Respostas de sucesso NUNCA contêm campo "error"
- Respostas de erro NUNCA contêm campo "status" ou "data"

Requisitos validados: 16.1, 16.2
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings, strategies as st

from ledger.api.read_handler import handle_get_balance, handle_get_statement
from ledger.api.write_handler import handle_create_entry, handle_create_reversal
from ledger.application.dtos import (
    ErrorResponseDTO,
    SuccessResponseDTO,
)
from ledger.application.handlers import CommandHandler, QueryHandler
from ledger.domain.aggregates import JournalEntry
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
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.ports import StatementPage
from ledger.domain.value_objects import Balance, Direction, EntryType, Money, OutboxEvent, Posting


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Moedas ISO 4217 suportadas
currencies = st.sampled_from(["BRL", "USD", "EUR", "GBP"])

# Valores monetários válidos em minor units
valid_amounts = st.integers(min_value=1, max_value=10_000_000)

# Strings não-vazias para IDs
non_empty_strings = st.text(
    min_size=1,
    max_size=64,
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
)

# Dicts simples para metadata
metadata_strategy = st.fixed_dictionaries({}) | st.fixed_dictionaries(
    {"order_id": st.text(min_size=1, max_size=32)}
)


@st.composite
def valid_posting_dict(draw: st.DrawFn) -> dict:
    """Gera um dict de posting válido para o payload da API."""
    return {
        "account_id": draw(non_empty_strings),
        "amount": draw(valid_amounts),
        "currency": draw(currencies),
        "direction": draw(st.sampled_from(["DEBIT", "CREDIT"])),
    }


@st.composite
def balanced_postings_list(draw: st.DrawFn) -> list[dict]:
    """
    Gera lista de postings balanceados (zero-sum) para o payload da API.

    Cada par DEBIT/CREDIT usa o mesmo amount e currency para garantir zero-sum.
    """
    n_pairs = draw(st.integers(min_value=1, max_value=5))
    postings = []
    for _ in range(n_pairs):
        amount = draw(valid_amounts)
        currency = draw(currencies)
        postings.append({
            "account_id": draw(non_empty_strings),
            "amount": amount,
            "currency": currency,
            "direction": "DEBIT",
        })
        postings.append({
            "account_id": draw(non_empty_strings),
            "amount": amount,
            "currency": currency,
            "direction": "CREDIT",
        })
    return postings


@st.composite
def valid_create_entry_payload(draw: st.DrawFn) -> dict:
    """Gera payload válido para POST /entries."""
    return {
        "external_id": draw(non_empty_strings),
        "postings": draw(balanced_postings_list()),
        "metadata": draw(metadata_strategy),
    }


@st.composite
def valid_create_reversal_payload(draw: st.DrawFn) -> dict:
    """Gera payload válido para POST /reversals."""
    return {
        "original_entry_id": str(uuid.uuid4()),
        "external_id": draw(non_empty_strings),
        "metadata": draw(metadata_strategy),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_journal_entry(amount: int = 1000, currency: str = "BRL") -> JournalEntry:
    """Cria um JournalEntry via factory para uso nos testes."""
    factory = JournalEntryFactory()
    from ledger.application.commands import CreateJournalEntryCommand, PostingInput
    command = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            PostingInput(account_id="acc_a", amount=amount, currency=currency, direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=amount, currency=currency, direction="CREDIT"),
        ],
        metadata={},
    )
    return factory.create_standard(command)


def _make_lambda_event(body: dict | None = None, path_params: dict | None = None,
                       query_params: dict | None = None) -> dict:
    """Constrói um evento Lambda simulando API Gateway proxy integration."""
    return {
        "body": json.dumps(body) if body is not None else None,
        "pathParameters": path_params or {},
        "queryStringParameters": query_params or {},
        "requestContext": {"requestId": str(uuid.uuid4())},
    }


def _parse_response_body(response: dict) -> dict:
    """Desserializa o body JSON da resposta do handler."""
    return json.loads(response["body"])


def _assert_success_format(body: dict) -> None:
    """
    Verifica que o body segue o formato de sucesso da API (Requisito 16.1).

    Formato esperado: {"status": "success", "data": <qualquer>, "metadata": <dict>}
    """
    assert "status" in body, f"Resposta de sucesso deve ter campo 'status': {body}"
    assert body["status"] == "success", f"Campo 'status' deve ser 'success': {body}"
    assert "data" in body, f"Resposta de sucesso deve ter campo 'data': {body}"
    assert "metadata" in body, f"Resposta de sucesso deve ter campo 'metadata': {body}"
    assert isinstance(body["metadata"], dict), f"'metadata' deve ser dict: {body}"
    assert "error" not in body, f"Resposta de sucesso não deve ter campo 'error': {body}"


def _assert_error_format(body: dict) -> None:
    """
    Verifica que o body segue o formato de erro da API (Requisito 16.2).

    Formato esperado: {"error": {"code": <str>, "message": <str>}}
    """
    assert "error" in body, f"Resposta de erro deve ter campo 'error': {body}"
    error = body["error"]
    assert isinstance(error, dict), f"'error' deve ser dict: {body}"
    assert "code" in error, f"'error' deve ter campo 'code': {body}"
    assert "message" in error, f"'error' deve ter campo 'message': {body}"
    assert isinstance(error["code"], str), f"'error.code' deve ser string: {body}"
    assert isinstance(error["message"], str), f"'error.message' deve ser string: {body}"
    assert "status" not in body, f"Resposta de erro não deve ter campo 'status': {body}"
    assert "data" not in body, f"Resposta de erro não deve ter campo 'data': {body}"


# ===========================================================================
# Property 20: Formato de resposta da API
# ===========================================================================


@pytest.mark.property
class TestAPIResponseFormatProperties:
    """
    Property 20: Formato de resposta da API.

    Valida que os contratos de resposta são respeitados para qualquer
    entrada gerada pelo Hypothesis.

    Validates: Requirements 16.1, 16.2
    """

    # -----------------------------------------------------------------------
    # Propriedade 20a: SuccessResponseDTO sempre segue o formato correto
    # -----------------------------------------------------------------------

    @given(
        data=st.one_of(
            st.none(),
            st.integers(),
            st.text(),
            st.dictionaries(st.text(min_size=1, max_size=10), st.integers()),
            st.lists(st.integers()),
        ),
        metadata=st.dictionaries(
            st.text(min_size=1, max_size=10),
            st.text(min_size=1, max_size=20),
            max_size=5,
        ),
    )
    def test_success_response_dto_always_valid_format(self, data: Any, metadata: dict) -> None:
        """
        **Property 20a: SuccessResponseDTO sempre produz formato válido.**

        Para qualquer data e metadata, SuccessResponseDTO.to_dict() deve
        retornar um dict com status="success", data e metadata.

        Validates: Requirement 16.1
        """
        response = SuccessResponseDTO(data=data, metadata=metadata)
        result = response.to_dict()

        _assert_success_format(result)
        assert result["data"] == data
        assert result["metadata"] == metadata

    # -----------------------------------------------------------------------
    # Propriedade 20b: ErrorResponseDTO sempre segue o formato correto
    # -----------------------------------------------------------------------

    @given(
        code=st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_")),
        message=st.text(min_size=1, max_size=200),
    )
    def test_error_response_dto_always_valid_format(self, code: str, message: str) -> None:
        """
        **Property 20b: ErrorResponseDTO sempre produz formato válido.**

        Para qualquer code e message, ErrorResponseDTO.to_dict() deve
        retornar um dict com error.code e error.message.

        Validates: Requirement 16.2
        """
        response = ErrorResponseDTO(code=code, message=message)
        result = response.to_dict()

        _assert_error_format(result)
        assert result["error"]["code"] == code
        assert result["error"]["message"] == message

    # -----------------------------------------------------------------------
    # Propriedade 20c: Write handler retorna formato correto em sucesso
    # -----------------------------------------------------------------------

    @given(payload=valid_create_entry_payload())
    @settings(max_examples=30)
    def test_write_handler_success_response_format(self, payload: dict) -> None:
        """
        **Property 20c: Write handler retorna formato de sucesso correto.**

        Para qualquer payload válido de POST /entries, quando o CommandHandler
        retorna com sucesso, a resposta deve seguir o formato de sucesso.

        Validates: Requirement 16.1
        """
        # Mock do CommandHandler que retorna um JournalEntry válido
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_journal_entry.return_value = _make_journal_entry()

        event = _make_lambda_event(body=payload)
        response = handle_create_entry(event, {}, mock_handler)

        assert response["statusCode"] == 201
        body = _parse_response_body(response)
        _assert_success_format(body)

        # Verifica campos específicos do JournalEntry na resposta
        data = body["data"]
        assert "entry_id" in data
        assert "external_id" in data
        assert "entry_type" in data
        assert "postings" in data
        assert "timestamp" in data

    # -----------------------------------------------------------------------
    # Propriedade 20d: Write handler retorna formato correto em erro de domínio
    # -----------------------------------------------------------------------

    @given(
        payload=valid_create_entry_payload(),
        error=st.one_of(
            st.builds(ZeroSumViolation, currency=st.just("BRL"), total=st.integers(min_value=1)),
            st.builds(InvalidAmountType, received_type=st.just("float")),
            st.builds(TransactionLimitExceeded, item_count=st.integers(min_value=101)),
            st.builds(TransactionSizeExceeded, size_bytes=st.integers(min_value=4_000_001)),
            st.builds(OptimisticLockConflict, account_id=st.just("acc_001"), expected_version=st.integers(min_value=0)),
        ),
    )
    @settings(max_examples=30)
    def test_write_handler_domain_error_response_format(self, payload: dict, error: DomainError) -> None:
        """
        **Property 20d: Write handler retorna formato de erro correto para DomainErrors.**

        Para qualquer DomainError levantado pelo CommandHandler, a resposta
        deve seguir o formato de erro com o http_status correto.

        Validates: Requirement 16.2
        """
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_journal_entry.side_effect = error

        event = _make_lambda_event(body=payload)
        response = handle_create_entry(event, {}, mock_handler)

        assert response["statusCode"] == error.http_status
        body = _parse_response_body(response)
        _assert_error_format(body)
        assert body["error"]["code"] == error.code

    # -----------------------------------------------------------------------
    # Propriedade 20e: Idempotência retorna HTTP 200 com formato de sucesso
    # -----------------------------------------------------------------------

    @given(payload=valid_create_entry_payload())
    @settings(max_examples=20)
    def test_write_handler_idempotency_returns_200_success_format(self, payload: dict) -> None:
        """
        **Property 20e: Idempotência retorna HTTP 200 com formato de sucesso.**

        Quando o CommandHandler levanta IdempotencyConflict, o handler deve
        retornar HTTP 200 (não erro) com formato de sucesso (Requisito 4.2).

        Validates: Requirements 4.2, 16.1
        """
        existing_entry_id = str(uuid.uuid4())
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_journal_entry.side_effect = IdempotencyConflict(
            external_id=payload["external_id"],
            existing_entry_id=existing_entry_id,
        )

        event = _make_lambda_event(body=payload)
        response = handle_create_entry(event, {}, mock_handler)

        # Idempotência deve retornar 200, não 409
        assert response["statusCode"] == 200
        body = _parse_response_body(response)
        _assert_success_format(body)
        assert body["data"]["entry_id"] == existing_entry_id
        assert body["data"]["idempotent"] is True

    # -----------------------------------------------------------------------
    # Propriedade 20f: Read handler de saldo retorna formato correto
    # -----------------------------------------------------------------------

    @given(
        account_id=non_empty_strings,
        currency=currencies,
        balance_amount=st.integers(min_value=-1_000_000, max_value=1_000_000),
        version=st.integers(min_value=0, max_value=1000),
    )
    @settings(max_examples=30)
    def test_read_handler_balance_success_format(
        self, account_id: str, currency: str, balance_amount: int, version: int
    ) -> None:
        """
        **Property 20f: Read handler de saldo retorna formato de sucesso correto.**

        Para qualquer Balance retornado pelo QueryHandler, a resposta deve
        seguir o formato de sucesso com os campos corretos.

        Validates: Requirement 16.1
        """
        mock_handler = MagicMock(spec=QueryHandler)
        mock_handler.handle_get_balance.return_value = Balance(
            account_id=account_id,
            currency=currency,
            balance_amount=balance_amount,
            version=version,
            last_update="2026-03-10T14:00:00Z",
        )

        event = _make_lambda_event(
            path_params={"account_id": account_id},
            query_params={"currency": currency},
        )
        response = handle_get_balance(event, {}, mock_handler)

        assert response["statusCode"] == 200
        body = _parse_response_body(response)
        _assert_success_format(body)

        data = body["data"]
        assert data["account_id"] == account_id
        assert data["currency"] == currency
        assert data["balance_amount"] == balance_amount
        assert data["version"] == version

    # -----------------------------------------------------------------------
    # Propriedade 20g: Read handler de extrato retorna formato correto
    # -----------------------------------------------------------------------

    @given(
        account_id=non_empty_strings,
        n_postings=st.integers(min_value=0, max_value=10),
        has_more=st.booleans(),
    )
    @settings(max_examples=30)
    def test_read_handler_statement_success_format(
        self, account_id: str, n_postings: int, has_more: bool
    ) -> None:
        """
        **Property 20g: Read handler de extrato retorna formato de sucesso correto.**

        Para qualquer StatementPage retornada pelo QueryHandler, a resposta
        deve seguir o formato de sucesso com postings, next_cursor e has_more.

        Validates: Requirements 16.1, 16.4
        """
        # Gera postings de exemplo
        postings = [
            Posting(
                account_id=account_id,
                money=Money(amount=1000, currency="BRL"),
                direction=Direction.DEBIT,
                index=i,
            )
            for i in range(n_postings)
        ]
        next_cursor = f"POSTING#2026-03-10T14:00:00Z#uuid-abc#{n_postings}" if has_more else None

        mock_handler = MagicMock(spec=QueryHandler)
        mock_handler.handle_get_statement.return_value = StatementPage(
            postings=postings,
            next_cursor=next_cursor,
            has_more=has_more,
        )

        event = _make_lambda_event(
            path_params={"account_id": account_id},
            query_params={"page_size": "20"},
        )
        response = handle_get_statement(event, {}, mock_handler)

        assert response["statusCode"] == 200
        body = _parse_response_body(response)
        _assert_success_format(body)

        data = body["data"]
        assert "postings" in data
        assert "next_cursor" in data
        assert "has_more" in data
        assert isinstance(data["postings"], list)
        assert len(data["postings"]) == n_postings
        assert data["has_more"] == has_more
        assert data["next_cursor"] == next_cursor

    # -----------------------------------------------------------------------
    # Propriedade 20h: Schema inválido sempre retorna HTTP 400 com formato de erro
    # -----------------------------------------------------------------------

    @given(
        missing_field=st.sampled_from(["external_id", "postings"]),
    )
    def test_write_handler_schema_error_returns_400_error_format(self, missing_field: str) -> None:
        """
        **Property 20h: Schema inválido retorna HTTP 400 com formato de erro.**

        Para qualquer payload com campo obrigatório ausente, o handler deve
        retornar HTTP 400 com formato de erro estruturado.

        Validates: Requirements 2.2, 16.2, 16.3
        """
        # Payload com campo obrigatório removido
        payload = {
            "external_id": "ext-001",
            "postings": [
                {"account_id": "acc_a", "amount": 1000, "currency": "BRL", "direction": "DEBIT"},
                {"account_id": "acc_b", "amount": 1000, "currency": "BRL", "direction": "CREDIT"},
            ],
        }
        del payload[missing_field]

        mock_handler = MagicMock(spec=CommandHandler)
        event = _make_lambda_event(body=payload)
        response = handle_create_entry(event, {}, mock_handler)

        assert response["statusCode"] == 400
        body = _parse_response_body(response)
        _assert_error_format(body)

        # CommandHandler não deve ser chamado quando schema é inválido
        mock_handler.handle_create_journal_entry.assert_not_called()

    # -----------------------------------------------------------------------
    # Propriedade 20i: Float em amount sempre retorna HTTP 400 com formato de erro
    # -----------------------------------------------------------------------

    @given(
        float_amount=st.floats(min_value=0.01, max_value=10000.0, allow_nan=False, allow_infinity=False),
    )
    def test_write_handler_float_amount_returns_400(self, float_amount: float) -> None:
        """
        **Property 20i: Float em amount sempre retorna HTTP 400.**

        Para qualquer valor float em amount, o schema validator deve rejeitar
        a requisição com HTTP 400 e código SCHEMA_VALIDATION_ERROR.

        Validates: Requirements 2.2, 16.2
        """
        payload = {
            "external_id": "ext-001",
            "postings": [
                {"account_id": "acc_a", "amount": float_amount, "currency": "BRL", "direction": "DEBIT"},
                {"account_id": "acc_b", "amount": int(float_amount * 100), "currency": "BRL", "direction": "CREDIT"},
            ],
        }

        mock_handler = MagicMock(spec=CommandHandler)
        event = _make_lambda_event(body=payload)
        response = handle_create_entry(event, {}, mock_handler)

        assert response["statusCode"] == 400
        body = _parse_response_body(response)
        _assert_error_format(body)
        assert body["error"]["code"] == "SCHEMA_VALIDATION_ERROR"

        # CommandHandler não deve ser chamado quando amount é float
        mock_handler.handle_create_journal_entry.assert_not_called()

    # -----------------------------------------------------------------------
    # Propriedade 20j: Resposta de sucesso é sempre JSON serializável
    # -----------------------------------------------------------------------

    @given(payload=valid_create_entry_payload())
    @settings(max_examples=20)
    def test_write_handler_response_is_json_serializable(self, payload: dict) -> None:
        """
        **Property 20j: Resposta do handler é sempre JSON serializável.**

        Para qualquer payload válido, a resposta do handler deve ter um body
        que pode ser desserializado como JSON sem erros.

        Validates: Requirement 16.1
        """
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_journal_entry.return_value = _make_journal_entry()

        event = _make_lambda_event(body=payload)
        response = handle_create_entry(event, {}, mock_handler)

        # Deve ser possível desserializar o body sem exceção
        try:
            body = json.loads(response["body"])
            assert isinstance(body, dict)
        except json.JSONDecodeError as exc:
            pytest.fail(f"Response body não é JSON válido: {exc}")

    # -----------------------------------------------------------------------
    # Propriedade 20k: Reversal handler retorna formato correto em sucesso
    # -----------------------------------------------------------------------

    @given(payload=valid_create_reversal_payload())
    @settings(max_examples=20)
    def test_reversal_handler_success_response_format(self, payload: dict) -> None:
        """
        **Property 20k: Reversal handler retorna formato de sucesso correto.**

        Para qualquer payload válido de POST /reversals, quando o CommandHandler
        retorna com sucesso, a resposta deve seguir o formato de sucesso.

        Validates: Requirement 16.1
        """
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_reversal.return_value = _make_journal_entry()

        event = _make_lambda_event(body=payload)
        response = handle_create_reversal(event, {}, mock_handler)

        assert response["statusCode"] == 201
        body = _parse_response_body(response)
        _assert_success_format(body)

    # -----------------------------------------------------------------------
    # Propriedade 20l: JournalEntryNotFound retorna HTTP 404 com formato de erro
    # -----------------------------------------------------------------------

    @given(payload=valid_create_reversal_payload())
    @settings(max_examples=20)
    def test_reversal_handler_not_found_returns_404(self, payload: dict) -> None:
        """
        **Property 20l: JournalEntryNotFound retorna HTTP 404 com formato de erro.**

        Quando o entry original não existe, o handler deve retornar HTTP 404
        com formato de erro estruturado.

        Validates: Requirement 16.2
        """
        mock_handler = MagicMock(spec=CommandHandler)
        mock_handler.handle_create_reversal.side_effect = JournalEntryNotFound(
            entry_id=payload["original_entry_id"]
        )

        event = _make_lambda_event(body=payload)
        response = handle_create_reversal(event, {}, mock_handler)

        assert response["statusCode"] == 404
        body = _parse_response_body(response)
        _assert_error_format(body)
        assert body["error"]["code"] == "JOURNAL_ENTRY_NOT_FOUND"
