"""
Testes de integração de componentes — wiring end-to-end do Double-Entry Ledger.

Verifica que todos os componentes estão corretamente conectados e que o fluxo
completo funciona sem dependência de infraestrutura AWS:

    API handler → DTO → Command → LedgerEngine → ValidationChain
                                               → JournalEntryFactory
                                               → InMemoryLedgerRepository

Fluxos testados:
1. Criação de lançamento padrão (POST /entries) — caminho feliz
2. Idempotência end-to-end — mesmo external_id retorna entry original
3. Rejeição de lançamento desbalanceado — ZeroSumViolation propagada até HTTP 400
4. Rejeição de amount float — InvalidAmountType propagada até HTTP 400
5. Reversão end-to-end — postings invertidos, soma combinada zero
6. Reversão de entry inexistente — JournalEntryNotFound propagada até HTTP 404
7. Consulta de saldo após lançamento — Balance materializado corretamente
8. Consulta de extrato paginado — postings ordenados e paginados

Requisitos validados: 1.1, 4.1, 9.2
"""
from __future__ import annotations

import json
import uuid

import pytest

from ledger.api.read_handler import handle_get_balance, handle_get_statement
from ledger.api.write_handler import handle_create_entry, handle_create_reversal
from ledger.application.handlers import CommandHandler, QueryHandler
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.services import LedgerEngine
from ledger.domain.validators import (
    MinorUnitsValidator,
    TransactionLimitValidator,
    ValidationChain,
    ZeroSumValidator,
)
from tests.ledger.unit.in_memory_repository import InMemoryLedgerRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repository() -> InMemoryLedgerRepository:
    """Repositório in-memory limpo para cada teste."""
    return InMemoryLedgerRepository()


@pytest.fixture
def engine(repository: InMemoryLedgerRepository) -> LedgerEngine:
    """LedgerEngine configurado com todos os validadores e factory."""
    validation_chain = ValidationChain([
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
    ])
    factory = JournalEntryFactory()
    return LedgerEngine(
        repository=repository,
        validation_chain=validation_chain,
        factory=factory,
    )


@pytest.fixture
def command_handler(engine: LedgerEngine) -> CommandHandler:
    """CommandHandler conectado ao LedgerEngine."""
    return CommandHandler(engine=engine)


@pytest.fixture
def query_handler(repository: InMemoryLedgerRepository) -> QueryHandler:
    """QueryHandler conectado diretamente ao repositório (Read Path)."""
    return QueryHandler(repository=repository)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry_event(
    external_id: str,
    postings: list[dict],
    metadata: dict | None = None,
) -> dict:
    """
    Constrói um evento Lambda simulando POST /entries.

    Args:
        external_id: Chave de idempotência.
        postings:    Lista de dicts com account_id, amount, currency, direction.
        metadata:    Metadados opcionais.

    Returns:
        Evento Lambda no formato API Gateway proxy integration.
    """
    body = {
        "external_id": external_id,
        "postings": postings,
        "metadata": metadata or {},
    }
    return {"body": json.dumps(body)}


def _make_reversal_event(
    original_entry_id: str,
    external_id: str,
    metadata: dict | None = None,
) -> dict:
    """
    Constrói um evento Lambda simulando POST /reversals.

    Args:
        original_entry_id: entry_id do lançamento a ser revertido.
        external_id:       Chave de idempotência da reversão.
        metadata:          Metadados opcionais.

    Returns:
        Evento Lambda no formato API Gateway proxy integration.
    """
    body = {
        "original_entry_id": original_entry_id,
        "external_id": external_id,
        "metadata": metadata or {},
    }
    return {"body": json.dumps(body)}


def _make_balance_event(account_id: str, currency: str) -> dict:
    """Constrói um evento Lambda simulando GET /balances/{account_id}."""
    return {
        "pathParameters": {"account_id": account_id},
        "queryStringParameters": {"currency": currency},
    }


def _make_statement_event(
    account_id: str,
    cursor: str | None = None,
    page_size: int | None = None,
) -> dict:
    """Constrói um evento Lambda simulando GET /statements/{account_id}."""
    query_params: dict = {}
    if cursor:
        query_params["cursor"] = cursor
    if page_size is not None:
        query_params["page_size"] = str(page_size)
    return {
        "pathParameters": {"account_id": account_id},
        "queryStringParameters": query_params or None,
    }


def _parse_response_body(response: dict) -> dict:
    """Desserializa o body JSON da resposta Lambda."""
    return json.loads(response["body"])


# ---------------------------------------------------------------------------
# Teste 1: Criação de lançamento padrão — caminho feliz
# ---------------------------------------------------------------------------


def test_create_entry_happy_path(
    command_handler: CommandHandler,
    repository: InMemoryLedgerRepository,
) -> None:
    """
    Verifica o fluxo completo de criação de lançamento via API handler.

    Fluxo: POST /entries → schema validation → DTO → Command → Engine
           → ValidationChain → Factory → InMemoryRepository

    Validates: Requirements 1.1, 4.1
    """
    external_id = str(uuid.uuid4())
    event = _make_entry_event(
        external_id=external_id,
        postings=[
            {"account_id": "acc_available_001", "amount": 1000, "currency": "BRL", "direction": "DEBIT"},
            {"account_id": "acc_fees_platform", "amount": 1000, "currency": "BRL", "direction": "CREDIT"},
        ],
    )

    response = handle_create_entry(event, context=None, command_handler=command_handler)

    # Verifica HTTP 201 Created
    assert response["statusCode"] == 201, (
        f"Esperado HTTP 201, recebido: {response['statusCode']}. Body: {response['body']}"
    )

    body = _parse_response_body(response)
    assert body["status"] == "success"
    assert "entry_id" in body["data"]
    assert body["data"]["external_id"] == external_id
    assert body["data"]["entry_type"] == "STANDARD"
    assert len(body["data"]["postings"]) == 2

    # Verifica que o entry foi persistido no repositório
    entry_id = body["data"]["entry_id"]
    persisted = repository.find_journal_entry_by_id(entry_id)
    assert persisted is not None, "Entry deve estar persistido no repositório"
    assert persisted.external_id == external_id
    assert persisted.validate_zero_sum(), "Entry persistido deve ser zero-sum"


# ---------------------------------------------------------------------------
# Teste 2: Idempotência end-to-end
# ---------------------------------------------------------------------------


def test_idempotency_returns_original_entry(
    command_handler: CommandHandler,
) -> None:
    """
    Verifica que submissões duplicadas com o mesmo external_id retornam
    o entry original com HTTP 200 (não 201 e não erro).

    Validates: Requirements 4.1
    """
    external_id = str(uuid.uuid4())
    postings = [
        {"account_id": "acc_a", "amount": 500, "currency": "USD", "direction": "DEBIT"},
        {"account_id": "acc_b", "amount": 500, "currency": "USD", "direction": "CREDIT"},
    ]

    event = _make_entry_event(external_id=external_id, postings=postings)

    # Primeira submissão — deve criar o entry (HTTP 201)
    first_response = handle_create_entry(event, context=None, command_handler=command_handler)
    assert first_response["statusCode"] == 201
    first_body = _parse_response_body(first_response)
    original_entry_id = first_body["data"]["entry_id"]

    # Segunda submissão com mesmo external_id — deve retornar HTTP 200 com entry original
    second_response = handle_create_entry(event, context=None, command_handler=command_handler)
    assert second_response["statusCode"] == 200, (
        f"Segunda submissão deve retornar HTTP 200 (idempotência), "
        f"recebido: {second_response['statusCode']}"
    )

    second_body = _parse_response_body(second_response)
    assert second_body["status"] == "success"
    assert second_body["data"]["entry_id"] == original_entry_id, (
        f"entry_id da segunda submissão deve ser igual ao original. "
        f"Original: {original_entry_id}, Recebido: {second_body['data']['entry_id']}"
    )
    assert second_body["data"].get("idempotent") is True


# ---------------------------------------------------------------------------
# Teste 3: Rejeição de lançamento desbalanceado
# ---------------------------------------------------------------------------


def test_unbalanced_entry_returns_400(command_handler: CommandHandler) -> None:
    """
    Verifica que lançamentos com postings desbalanceados são rejeitados
    com HTTP 400 e código ZERO_SUM_VIOLATION.

    Validates: Requirements 1.1, 1.2
    """
    event = _make_entry_event(
        external_id=str(uuid.uuid4()),
        postings=[
            # Apenas DEBIT — soma != 0 (violação de zero-sum)
            {"account_id": "acc_a", "amount": 1000, "currency": "BRL", "direction": "DEBIT"},
            {"account_id": "acc_b", "amount": 500, "currency": "BRL", "direction": "CREDIT"},
        ],
    )

    response = handle_create_entry(event, context=None, command_handler=command_handler)

    assert response["statusCode"] == 400, (
        f"Lançamento desbalanceado deve retornar HTTP 400, recebido: {response['statusCode']}"
    )

    body = _parse_response_body(response)
    assert body["error"]["code"] == "ZERO_SUM_VIOLATION", (
        f"Código de erro esperado: ZERO_SUM_VIOLATION, recebido: {body['error']['code']}"
    )


# ---------------------------------------------------------------------------
# Teste 4: Rejeição de amount float
# ---------------------------------------------------------------------------


def test_float_amount_returns_400(command_handler: CommandHandler) -> None:
    """
    Verifica que amounts do tipo float são rejeitados com HTTP 400
    e código INVALID_AMOUNT_TYPE ou SCHEMA_VALIDATION_ERROR.

    Validates: Requirements 2.1, 2.2
    """
    event = _make_entry_event(
        external_id=str(uuid.uuid4()),
        postings=[
            # amount float — deve ser rejeitado
            {"account_id": "acc_a", "amount": 10.50, "currency": "BRL", "direction": "DEBIT"},
            {"account_id": "acc_b", "amount": 10.50, "currency": "BRL", "direction": "CREDIT"},
        ],
    )

    response = handle_create_entry(event, context=None, command_handler=command_handler)

    assert response["statusCode"] == 400, (
        f"Amount float deve retornar HTTP 400, recebido: {response['statusCode']}"
    )

    body = _parse_response_body(response)
    # O erro pode vir do schema validator (SCHEMA_VALIDATION_ERROR) ou do domínio (INVALID_AMOUNT_TYPE)
    assert "error" in body, f"Resposta deve conter campo 'error'. Body: {body}"
    assert body["error"]["code"] in ("SCHEMA_VALIDATION_ERROR", "INVALID_AMOUNT_TYPE"), (
        f"Código de erro esperado: SCHEMA_VALIDATION_ERROR ou INVALID_AMOUNT_TYPE, "
        f"recebido: {body['error']['code']}"
    )


# ---------------------------------------------------------------------------
# Teste 5: Reversão end-to-end
# ---------------------------------------------------------------------------


def test_reversal_end_to_end(
    command_handler: CommandHandler,
    repository: InMemoryLedgerRepository,
) -> None:
    """
    Verifica o fluxo completo de reversão:
    1. Cria lançamento original
    2. Cria reversão via POST /reversals
    3. Verifica que a soma combinada (original + reversal) é zero por moeda
    4. Verifica que o metadata da reversão contém original_entry_id

    Validates: Requirements 9.2
    """
    # Passo 1: Cria o lançamento original
    original_external_id = str(uuid.uuid4())
    create_event = _make_entry_event(
        external_id=original_external_id,
        postings=[
            {"account_id": "acc_available", "amount": 2000, "currency": "BRL", "direction": "DEBIT"},
            {"account_id": "acc_hold",      "amount": 2000, "currency": "BRL", "direction": "CREDIT"},
        ],
    )
    create_response = handle_create_entry(create_event, context=None, command_handler=command_handler)
    assert create_response["statusCode"] == 201
    original_entry_id = _parse_response_body(create_response)["data"]["entry_id"]

    # Passo 2: Cria a reversão
    reversal_external_id = str(uuid.uuid4())
    reversal_event = _make_reversal_event(
        original_entry_id=original_entry_id,
        external_id=reversal_external_id,
        metadata={"reason": "test_reversal"},
    )
    reversal_response = handle_create_reversal(reversal_event, context=None, command_handler=command_handler)

    assert reversal_response["statusCode"] == 201, (
        f"Reversão deve retornar HTTP 201, recebido: {reversal_response['statusCode']}. "
        f"Body: {reversal_response['body']}"
    )

    reversal_body = _parse_response_body(reversal_response)
    assert reversal_body["status"] == "success"
    assert reversal_body["data"]["entry_type"] == "REVERSAL"

    # Passo 3: Verifica soma combinada zero por moeda
    reversal_entry_id = reversal_body["data"]["entry_id"]
    original = repository.find_journal_entry_by_id(original_entry_id)
    reversal = repository.find_journal_entry_by_id(reversal_entry_id)

    assert original is not None
    assert reversal is not None

    # Calcula soma combinada por moeda
    combined_sums: dict[str, int] = {}
    for posting in original.postings:
        currency = posting.money.currency
        combined_sums[currency] = combined_sums.get(currency, 0) + posting.signed_amount
    for posting in reversal.postings:
        currency = posting.money.currency
        combined_sums[currency] = combined_sums.get(currency, 0) + posting.signed_amount

    for currency, total in combined_sums.items():
        assert total == 0, (
            f"Soma combinada (original + reversal) deve ser zero para moeda {currency}. "
            f"Total: {total}"
        )

    # Passo 4: Verifica metadata da reversão
    assert reversal.metadata.get("original_entry_id") == original_entry_id, (
        f"Metadata da reversão deve conter original_entry_id={original_entry_id}. "
        f"Metadata: {reversal.metadata}"
    )


# ---------------------------------------------------------------------------
# Teste 6: Reversão de entry inexistente
# ---------------------------------------------------------------------------


def test_reversal_of_nonexistent_entry_returns_404(
    command_handler: CommandHandler,
) -> None:
    """
    Verifica que tentar reverter um entry inexistente retorna HTTP 404
    com código JOURNAL_ENTRY_NOT_FOUND.

    Validates: Requirements 9.2
    """
    reversal_event = _make_reversal_event(
        original_entry_id=str(uuid.uuid4()),  # ID inexistente
        external_id=str(uuid.uuid4()),
    )

    response = handle_create_reversal(reversal_event, context=None, command_handler=command_handler)

    assert response["statusCode"] == 404, (
        f"Reversão de entry inexistente deve retornar HTTP 404, recebido: {response['statusCode']}"
    )

    body = _parse_response_body(response)
    assert body["error"]["code"] == "JOURNAL_ENTRY_NOT_FOUND"


# ---------------------------------------------------------------------------
# Teste 7: Consulta de saldo após lançamento
# ---------------------------------------------------------------------------


def test_balance_reflects_postings(
    command_handler: CommandHandler,
    query_handler: QueryHandler,
) -> None:
    """
    Verifica que o saldo materializado reflete corretamente os postings
    após um lançamento bem-sucedido.

    Fluxo: POST /entries → InMemoryRepository → GET /balances/{account_id}

    Validates: Requirements 1.1
    """
    account_id = "acc_available_balance_test"
    amount = 3000  # R$ 30,00

    # Cria lançamento com DEBIT na conta de teste
    event = _make_entry_event(
        external_id=str(uuid.uuid4()),
        postings=[
            {"account_id": account_id, "amount": amount, "currency": "BRL", "direction": "DEBIT"},
            {"account_id": "acc_other", "amount": amount, "currency": "BRL", "direction": "CREDIT"},
        ],
    )
    create_response = handle_create_entry(event, context=None, command_handler=command_handler)
    assert create_response["statusCode"] == 201

    # Consulta o saldo via Read Handler
    balance_event = _make_balance_event(account_id=account_id, currency="BRL")
    balance_response = handle_get_balance(balance_event, context=None, query_handler=query_handler)

    assert balance_response["statusCode"] == 200
    balance_body = _parse_response_body(balance_response)
    assert balance_body["status"] == "success"
    assert balance_body["data"] is not None, "Saldo deve existir após lançamento"

    # DEBIT contribui com +amount ao saldo
    assert balance_body["data"]["balance_amount"] == amount, (
        f"Saldo esperado: {amount}, recebido: {balance_body['data']['balance_amount']}"
    )
    assert balance_body["data"]["currency"] == "BRL"
    assert balance_body["data"]["version"] == 1  # primeira atualização


# ---------------------------------------------------------------------------
# Teste 8: Consulta de extrato paginado
# ---------------------------------------------------------------------------


def test_statement_pagination(
    command_handler: CommandHandler,
    query_handler: QueryHandler,
) -> None:
    """
    Verifica que o extrato retorna postings paginados e ordenados cronologicamente.

    Cria 3 lançamentos na mesma conta e verifica que o extrato retorna
    os postings com paginação correta (page_size=2 → 2 páginas).

    Validates: Requirements 1.1
    """
    account_id = "acc_statement_test"

    # Cria 3 lançamentos na mesma conta
    for i in range(3):
        event = _make_entry_event(
            external_id=str(uuid.uuid4()),
            postings=[
                {"account_id": account_id, "amount": 100 * (i + 1), "currency": "BRL", "direction": "DEBIT"},
                {"account_id": "acc_other", "amount": 100 * (i + 1), "currency": "BRL", "direction": "CREDIT"},
            ],
        )
        response = handle_create_entry(event, context=None, command_handler=command_handler)
        assert response["statusCode"] == 201

    # Primeira página (page_size=2)
    first_page_event = _make_statement_event(account_id=account_id, page_size=2)
    first_page_response = handle_get_statement(first_page_event, context=None, query_handler=query_handler)

    assert first_page_response["statusCode"] == 200
    first_page_body = _parse_response_body(first_page_response)
    assert first_page_body["status"] == "success"
    assert len(first_page_body["data"]["postings"]) == 2, (
        f"Primeira página deve ter 2 postings, recebido: {len(first_page_body['data']['postings'])}"
    )
    assert first_page_body["data"]["has_more"] is True
    assert first_page_body["data"]["next_cursor"] is not None

    # Segunda página usando o cursor da primeira
    cursor = first_page_body["data"]["next_cursor"]
    second_page_event = _make_statement_event(account_id=account_id, cursor=cursor, page_size=2)
    second_page_response = handle_get_statement(second_page_event, context=None, query_handler=query_handler)

    assert second_page_response["statusCode"] == 200
    second_page_body = _parse_response_body(second_page_response)
    assert len(second_page_body["data"]["postings"]) == 1, (
        f"Segunda página deve ter 1 posting, recebido: {len(second_page_body['data']['postings'])}"
    )
    assert second_page_body["data"]["has_more"] is False
    assert second_page_body["data"]["next_cursor"] is None
