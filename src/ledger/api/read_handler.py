"""
Read Handler da camada de API do Double-Entry Ledger.

Lambda handlers para operações de leitura (Read Path / CQRS):
- GET /balances/{account_id}:  consulta saldo materializado de uma conta
- GET /statements/{account_id}: consulta extrato paginado de uma conta

O Read Path opera com consistência eventual — saldos e extratos podem estar
defasados em até ~1 segundo em relação ao último lançamento (Requisito 8.3).
Consultas de saldo são O(1) via GetItem no DynamoDB (Requisito 8.1).

Fluxo de cada handler:
1. Extrai parâmetros do evento Lambda (path parameters, query string)
2. Constrói o DTO de request
3. Converte DTO → Query via funções de conversão da camada de aplicação
4. Delega para QueryHandler → LedgerRepository
5. Converte o resultado → DTO de response
6. Retorna resposta HTTP estruturada

Formato de resposta (Requisito 16.1, 16.2):
- Sucesso: {"status": "success", "data": {...}, "metadata": {...}}
- Erro:    {"error": {"code": "<CODE>", "message": "<descrição>"}}

Paginação baseada em cursor (Requisito 16.4):
- Primeira página: sem parâmetro cursor
- Páginas seguintes: cursor=<posting_sort_key do último item>
- Resposta inclui next_cursor e has_more para navegação

Requisitos atendidos: 8.1, 8.2, 8.5, 16.1, 16.4
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from ledger.application.dtos import (
    ErrorResponseDTO,
    GetBalanceRequestDTO,
    GetStatementRequestDTO,
    SuccessResponseDTO,
    balance_to_response_dto,
    get_balance_query_from_dto,
    get_statement_query_from_dto,
    statement_page_to_response_dto,
)
from ledger.application.handlers import QueryHandler
from ledger.domain.errors import DomainError

# Logger estruturado para o Read Handler
logger = logging.getLogger(__name__)

# Tamanho máximo de página para evitar respostas excessivamente grandes
_MAX_PAGE_SIZE = 100
# Tamanho padrão de página quando não especificado pelo cliente
_DEFAULT_PAGE_SIZE = 20


# ---------------------------------------------------------------------------
# Handler: GET /balances/{account_id}
# ---------------------------------------------------------------------------


def handle_get_balance(event: dict, context: Any, query_handler: QueryHandler) -> dict:
    """
    Lambda handler para GET /balances/{account_id}.

    Consulta o saldo materializado de uma conta em uma moeda específica.
    Acesso O(1) via GetItem no DynamoDB (Requisito 8.1).

    Parâmetros da requisição:
        Path:        account_id (obrigatório)
        QueryString: currency (obrigatório — ex: ?currency=BRL)

    Respostas:
        200: Saldo encontrado com dados do Balance
        200: Saldo não encontrado (conta sem movimentação na moeda) — data: null
        400: Parâmetros inválidos (account_id ou currency ausentes)
        500: Erro interno

    Args:
        event:         Evento Lambda (API Gateway proxy integration).
        context:       Contexto Lambda (não utilizado diretamente).
        query_handler: Handler de queries injetado (facilita testes).

    Returns:
        Dict no formato API Gateway proxy response com statusCode e body JSON.
    """
    request_id = _extract_request_id(event)
    operation = "get_balance"

    # Extrai account_id do path parameter
    account_id = _extract_path_param(event, "account_id")
    if not account_id:
        return _error_response(400, "MISSING_PARAMETER", "Parâmetro obrigatório ausente: 'account_id'")

    # Extrai currency do query string
    currency = _extract_query_param(event, "currency")
    if not currency:
        return _error_response(
            400, "MISSING_PARAMETER", "Parâmetro obrigatório ausente: 'currency' (ex: ?currency=BRL)"
        )

    # Valida formato da currency (3 chars ISO 4217)
    if len(currency) != 3:
        return _error_response(
            400,
            "INVALID_PARAMETER",
            f"'currency' deve ter exatamente 3 caracteres ISO 4217, recebido: '{currency}'",
        )

    # Constrói DTO e query
    request_dto = GetBalanceRequestDTO(account_id=account_id, currency=currency.upper())
    query = get_balance_query_from_dto(request_dto)

    try:
        balance = query_handler.handle_get_balance(query)

        logger.info(
            "Consulta de saldo realizada",
            extra={
                "operation": operation,
                "account_id": account_id,
                "currency": currency,
                "found": balance is not None,
            },
        )

        if balance is None:
            # Conta sem saldo registrado para a moeda — retorna null (não é erro)
            return _success_response(
                status_code=200,
                data=None,
                metadata={"request_id": request_id, "account_id": account_id, "currency": currency},
            )

        balance_dto = balance_to_response_dto(balance)
        response_data = {
            "account_id": balance_dto.account_id,
            "currency": balance_dto.currency,
            "balance_amount": balance_dto.balance_amount,
            "version": balance_dto.version,
            "last_update": balance_dto.last_update,
        }
        return _success_response(
            status_code=200,
            data=response_data,
            metadata={"request_id": request_id},
        )

    except DomainError as exc:
        logger.warning(
            "Erro de domínio em %s: %s",
            operation,
            exc.code,
            extra={"operation": operation, "error_code": exc.code},
        )
        return _error_response(exc.http_status, exc.code, exc.message)

    except Exception as exc:
        logger.error(
            "Erro inesperado em %s",
            operation,
            extra={"operation": operation, "error": str(exc), "error_type": type(exc).__name__},
            exc_info=True,
        )
        return _error_response(500, "INTERNAL_ERROR", "Erro interno do servidor")


# ---------------------------------------------------------------------------
# Handler: GET /statements/{account_id}
# ---------------------------------------------------------------------------


def handle_get_statement(event: dict, context: Any, query_handler: QueryHandler) -> dict:
    """
    Lambda handler para GET /statements/{account_id}.

    Consulta o extrato paginado de uma conta, ordenado cronologicamente
    pelo posting_sort_key. Paginação baseada em cursor (Requisito 8.2, 8.5, 16.4).

    Parâmetros da requisição:
        Path:        account_id (obrigatório)
        QueryString: cursor    (opcional — posting_sort_key do último item da página anterior)
                     page_size (opcional — padrão 20, máximo 100)

    Respostas:
        200: Extrato com lista de postings, next_cursor e has_more
        400: Parâmetros inválidos
        500: Erro interno

    Paginação:
        - Primeira página: GET /statements/{account_id}
        - Próxima página:  GET /statements/{account_id}?cursor=<next_cursor>
        - Última página:   has_more == false, next_cursor == null

    Args:
        event:         Evento Lambda (API Gateway proxy integration).
        context:       Contexto Lambda (não utilizado diretamente).
        query_handler: Handler de queries injetado (facilita testes).

    Returns:
        Dict no formato API Gateway proxy response com statusCode e body JSON.
    """
    request_id = _extract_request_id(event)
    operation = "get_statement"

    # Extrai account_id do path parameter
    account_id = _extract_path_param(event, "account_id")
    if not account_id:
        return _error_response(400, "MISSING_PARAMETER", "Parâmetro obrigatório ausente: 'account_id'")

    # Extrai cursor (opcional — None para primeira página)
    cursor = _extract_query_param(event, "cursor")

    # Valida formato do cursor — deve começar com POSTING# (SK do DynamoDB)
    # Cursor inválido (ex: variável Postman não resolvida) é tratado como ausente
    if cursor and not cursor.startswith("POSTING#"):
        cursor = None

    # Extrai e valida page_size (opcional — padrão 20, máximo 100)
    page_size, page_size_error = _parse_page_size(event)
    if page_size_error:
        return _error_response(400, "INVALID_PARAMETER", page_size_error)

    # Constrói DTO e query
    request_dto = GetStatementRequestDTO(
        account_id=account_id,
        cursor=cursor or None,
        page_size=page_size,
    )
    query = get_statement_query_from_dto(request_dto)

    try:
        statement_page = query_handler.handle_get_statement(query)

        logger.info(
            "Consulta de extrato realizada",
            extra={
                "operation": operation,
                "account_id": account_id,
                "page_size": page_size,
                "postings_returned": len(statement_page.postings),
                "has_more": statement_page.has_more,
            },
        )

        statement_dto = statement_page_to_response_dto(statement_page)
        response_data = {
            "postings": [
                {
                    "account_id": p.account_id,
                    "amount": p.amount,
                    "currency": p.currency,
                    "direction": p.direction,
                    "index": p.index,
                }
                for p in statement_dto.postings
            ],
            "next_cursor": statement_dto.next_cursor,
            "has_more": statement_dto.has_more,
        }
        return _success_response(
            status_code=200,
            data=response_data,
            metadata={
                "request_id": request_id,
                "account_id": account_id,
                "page_size": page_size,
            },
        )

    except DomainError as exc:
        logger.warning(
            "Erro de domínio em %s: %s",
            operation,
            exc.code,
            extra={"operation": operation, "error_code": exc.code},
        )
        return _error_response(exc.http_status, exc.code, exc.message)

    except Exception as exc:
        logger.error(
            "Erro inesperado em %s",
            operation,
            extra={"operation": operation, "error": str(exc), "error_type": type(exc).__name__},
            exc_info=True,
        )
        return _error_response(500, "INTERNAL_ERROR", "Erro interno do servidor")


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------


def _extract_path_param(event: dict, param_name: str) -> str | None:
    """
    Extrai um path parameter do evento Lambda.

    API Gateway injeta path parameters em event["pathParameters"].
    Retorna None se o parâmetro não existe ou está vazio.

    Args:
        event:      Evento Lambda.
        param_name: Nome do parâmetro de path.

    Returns:
        Valor do parâmetro ou None.
    """
    path_params = event.get("pathParameters") or {}
    value = path_params.get(param_name)
    return value if value and value.strip() else None


def _extract_query_param(event: dict, param_name: str) -> str | None:
    """
    Extrai um query string parameter do evento Lambda.

    API Gateway injeta query string parameters em event["queryStringParameters"].
    Retorna None se o parâmetro não existe ou está vazio.

    Args:
        event:      Evento Lambda.
        param_name: Nome do parâmetro de query string.

    Returns:
        Valor do parâmetro ou None.
    """
    query_params = event.get("queryStringParameters") or {}
    value = query_params.get(param_name)
    return value if value and value.strip() else None


def _parse_page_size(event: dict) -> tuple[int, str | None]:
    """
    Extrai e valida o parâmetro page_size do query string.

    Regras:
    - Se ausente: usa _DEFAULT_PAGE_SIZE (20)
    - Se presente: deve ser inteiro positivo entre 1 e _MAX_PAGE_SIZE (100)

    Args:
        event: Evento Lambda.

    Returns:
        Tupla (page_size, error_message). Se válido, error_message é None.
    """
    raw_page_size = _extract_query_param(event, "page_size")

    if raw_page_size is None:
        return _DEFAULT_PAGE_SIZE, None

    try:
        page_size = int(raw_page_size)
    except ValueError:
        return _DEFAULT_PAGE_SIZE, (
            f"'page_size' deve ser um inteiro, recebido: '{raw_page_size}'"
        )

    if page_size < 1:
        return _DEFAULT_PAGE_SIZE, f"'page_size' deve ser >= 1, recebido: {page_size}"

    if page_size > _MAX_PAGE_SIZE:
        return _DEFAULT_PAGE_SIZE, (
            f"'page_size' deve ser <= {_MAX_PAGE_SIZE}, recebido: {page_size}"
        )

    return page_size, None


def _success_response(status_code: int, data: Any, metadata: dict | None = None) -> dict:
    """
    Constrói resposta HTTP de sucesso no formato padrão da API (Requisito 16.1).

    Formato: {"status": "success", "data": {...}, "metadata": {...}}

    Args:
        status_code: HTTP status code.
        data:        Dados da resposta (pode ser None para recursos não encontrados).
        metadata:    Metadados opcionais.

    Returns:
        Dict no formato API Gateway proxy response.
    """
    response = SuccessResponseDTO(data=data, metadata=metadata or {})
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(response.to_dict()),
    }


def _error_response(status_code: int, code: str, message: str) -> dict:
    """
    Constrói resposta HTTP de erro no formato padrão da API (Requisito 16.2).

    Formato: {"error": {"code": "<CODE>", "message": "<descrição>"}}

    Args:
        status_code: HTTP status code.
        code:        Código de erro estruturado.
        message:     Descrição legível do erro.

    Returns:
        Dict no formato API Gateway proxy response.
    """
    response = ErrorResponseDTO(code=code, message=message)
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(response.to_dict()),
    }


def _extract_request_id(event: dict) -> str:
    """
    Extrai o request ID do evento Lambda para correlação de logs.

    Args:
        event: Evento Lambda.

    Returns:
        String com o request ID (do API Gateway ou UUID gerado).
    """
    request_context = event.get("requestContext", {})
    return request_context.get("requestId", str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Entrypoint Lambda — roteamento por routeKey
# ---------------------------------------------------------------------------

import os
import boto3
from ledger.infrastructure.dynamodb_repository import DynamoDBLedgerRepository


def _build_query_handler() -> QueryHandler:
    """
    Constrói o QueryHandler com dependências reais (DynamoDB).

    Lê DYNAMODB_TABLE_NAME da variável de ambiente injetada pelo Terraform.
    O cliente boto3 é criado uma vez por container Lambda (warm start).
    """
    table_name = os.environ["DYNAMODB_TABLE_NAME"]
    dynamodb_client = boto3.client("dynamodb")
    repository = DynamoDBLedgerRepository(dynamodb_client=dynamodb_client, table_name=table_name)
    return QueryHandler(repository=repository)


# Instância reutilizada entre invocações (warm start)
_query_handler: QueryHandler | None = None


def handler(event: dict, context: Any) -> dict:
    """
    Entrypoint da Read Lambda.

    Roteia para o handler correto com base no routeKey do API Gateway v2:
      - GET /balances/{account_id}   → handle_get_balance
      - GET /statements/{account_id} → handle_get_statement

    Args:
        event:   Evento Lambda (API Gateway HTTP API v2 proxy format).
        context: Contexto Lambda.

    Returns:
        Dict no formato API Gateway proxy response.
    """
    global _query_handler
    if _query_handler is None:
        _query_handler = _build_query_handler()

    route_key = event.get("routeKey", "")

    if route_key.startswith("GET /balances/"):
        return handle_get_balance(event, context, _query_handler)
    elif route_key.startswith("GET /statements/"):
        return handle_get_statement(event, context, _query_handler)
    else:
        return _error_response(404, "ROUTE_NOT_FOUND", f"Rota não encontrada: {route_key}")
