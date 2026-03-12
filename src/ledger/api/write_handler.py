"""
Write Handler da camada de API do Double-Entry Ledger.

Lambda handlers para operações de escrita (Write Path):
- POST /entries:   cria um novo lançamento contábil (JournalEntry padrão)
- POST /reversals: cria uma reversão de lançamento existente

Fluxo de cada handler:
1. Desserializa o body JSON da requisição Lambda
2. Valida o schema via SchemaValidator (tipos, campos obrigatórios)
3. Constrói o DTO de request
4. Converte DTO → Command via funções de conversão da camada de aplicação
5. Delega para CommandHandler → LedgerEngine
6. Converte o resultado (JournalEntry) → DTO de response
7. Retorna resposta HTTP estruturada

Tratamento de erros:
- Erros de schema (SchemaValidationResult.is_valid == False) → HTTP 400
- DomainError → HTTP status do erro (400, 404, 409, 200 para idempotência)
- Exceções inesperadas → HTTP 500 com log de erro

Formato de resposta (Requisito 16.1, 16.2):
- Sucesso: {"status": "success", "data": {...}, "metadata": {...}}
- Erro:    {"error": {"code": "<CODE>", "message": "<descrição>"}}

Requisitos atendidos: 1.2, 2.2, 4.2, 5.2, 15.1, 16.1, 16.2
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from ledger.api.schema_validator import (
    validate_create_entry_payload,
    validate_create_reversal_payload,
)
from ledger.application.commands import CreateJournalEntryCommand, CreateReversalCommand
from ledger.application.dtos import (
    CreateJournalEntryRequestDTO,
    CreateReversalRequestDTO,
    ErrorResponseDTO,
    PostingRequestDTO,
    SuccessResponseDTO,
    create_journal_entry_command_from_dto,
    create_reversal_command_from_dto,
    journal_entry_to_response_dto,
)
from ledger.application.handlers import CommandHandler
from ledger.domain.errors import DomainError, IdempotencyConflict

# Logger estruturado para o Write Handler
# Emite JSON com entry_id, operation e result (Requisito 15.1)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handler: POST /entries
# ---------------------------------------------------------------------------


def handle_create_entry(event: dict, context: Any, command_handler: CommandHandler) -> dict:
    """
    Lambda handler para POST /entries.

    Cria um novo lançamento contábil (JournalEntry padrão) a partir do
    payload JSON da requisição.

    Fluxo:
        1. Desserializa body JSON
        2. Valida schema (campos obrigatórios, tipos — rejeita float em amount)
        3. Constrói CreateJournalEntryRequestDTO
        4. Converte para CreateJournalEntryCommand
        5. Delega para CommandHandler → LedgerEngine
        6. Retorna HTTP 201 com JournalEntry criado

    Casos especiais:
        - IdempotencyConflict: retorna HTTP 200 com entry original (não é erro)
        - DomainError: retorna HTTP com status do erro (400, 409, etc.)
        - Exceção inesperada: retorna HTTP 500

    Args:
        event:           Evento Lambda (API Gateway proxy integration).
        context:         Contexto Lambda (não utilizado diretamente).
        command_handler: Handler de comandos injetado (facilita testes).

    Returns:
        Dict no formato API Gateway proxy response com statusCode e body JSON.
    """
    request_id = _extract_request_id(event)
    operation = "create_journal_entry"

    # Desserializa o body JSON da requisição
    payload, parse_error = _parse_body(event)
    if parse_error:
        logger.warning(
            "Schema inválido em %s",
            operation,
            extra={"operation": operation, "request_id": request_id, "error": parse_error},
        )
        return _error_response(400, "INVALID_JSON", parse_error)

    # Valida o schema do payload (campos obrigatórios, tipos)
    validation_result = validate_create_entry_payload(payload)
    if not validation_result.is_valid:
        error_message = "; ".join(validation_result.errors)
        logger.warning(
            "Schema inválido em %s: %s",
            operation,
            error_message,
            extra={"operation": operation, "request_id": request_id, "errors": validation_result.errors},
        )
        return _error_response(400, "SCHEMA_VALIDATION_ERROR", error_message)

    # Constrói o DTO de request a partir do payload validado
    request_dto = _build_create_entry_dto(payload)

    # Converte DTO → Command e delega para o CommandHandler
    command = create_journal_entry_command_from_dto(request_dto)

    try:
        entry = command_handler.handle_create_journal_entry(command)

        # Log estruturado de sucesso (Requisito 15.1)
        logger.info(
            "Lançamento criado com sucesso",
            extra={
                "entry_id": entry.entry_id,
                "operation": operation,
                "result": "success",
                "external_id": entry.external_id,
            },
        )

        entry_dto = journal_entry_to_response_dto(entry)
        response_data = _journal_entry_dto_to_dict(entry_dto)
        return _success_response(
            status_code=201,
            data=response_data,
            metadata={"request_id": request_id},
        )

    except IdempotencyConflict as exc:
        # Idempotência: retorna HTTP 200 com entry original (não é erro — Requisito 4.2)
        logger.info(
            "Requisição idempotente detectada em %s",
            operation,
            extra={
                "operation": operation,
                "result": "idempotent",
                "external_id": command.external_id,
                "existing_entry_id": exc.existing_entry_id,
            },
        )
        return _success_response(
            status_code=200,
            data={"entry_id": exc.existing_entry_id, "idempotent": True},
            metadata={"request_id": request_id},
        )

    except DomainError as exc:
        # Erros de domínio conhecidos — log e resposta estruturada (Requisito 15.3)
        logger.warning(
            "Erro de domínio em %s: %s",
            operation,
            exc.code,
            extra={
                "operation": operation,
                "result": "domain_error",
                "error_code": exc.code,
                "error_message": exc.message,
            },
        )
        return _error_response(exc.http_status, exc.code, exc.message)

    except Exception as exc:
        # Exceção inesperada — log com detalhes para diagnóstico (Requisito 15.3)
        logger.error(
            "Erro inesperado em %s",
            operation,
            extra={
                "operation": operation,
                "result": "unexpected_error",
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
        return _error_response(500, "INTERNAL_ERROR", "Erro interno do servidor")


# ---------------------------------------------------------------------------
# Handler: POST /reversals
# ---------------------------------------------------------------------------


def handle_create_reversal(event: dict, context: Any, command_handler: CommandHandler) -> dict:
    """
    Lambda handler para POST /reversals.

    Cria uma reversão de um lançamento contábil existente. A reversão é a
    única forma de correção permitida no subledger (imutabilidade — Requisito 9.1).

    Fluxo:
        1. Desserializa body JSON
        2. Valida schema (original_entry_id, external_id obrigatórios)
        3. Constrói CreateReversalRequestDTO
        4. Converte para CreateReversalCommand
        5. Delega para CommandHandler → LedgerEngine
        6. Retorna HTTP 201 com JournalEntry de reversão criado

    Args:
        event:           Evento Lambda (API Gateway proxy integration).
        context:         Contexto Lambda (não utilizado diretamente).
        command_handler: Handler de comandos injetado (facilita testes).

    Returns:
        Dict no formato API Gateway proxy response com statusCode e body JSON.
    """
    request_id = _extract_request_id(event)
    operation = "create_reversal"

    # Desserializa o body JSON da requisição
    payload, parse_error = _parse_body(event)
    if parse_error:
        logger.warning(
            "Schema inválido em %s",
            operation,
            extra={"operation": operation, "request_id": request_id, "error": parse_error},
        )
        return _error_response(400, "INVALID_JSON", parse_error)

    # Valida o schema do payload
    validation_result = validate_create_reversal_payload(payload)
    if not validation_result.is_valid:
        error_message = "; ".join(validation_result.errors)
        logger.warning(
            "Schema inválido em %s: %s",
            operation,
            error_message,
            extra={"operation": operation, "request_id": request_id, "errors": validation_result.errors},
        )
        return _error_response(400, "SCHEMA_VALIDATION_ERROR", error_message)

    # Constrói o DTO de request a partir do payload validado
    request_dto = CreateReversalRequestDTO(
        original_entry_id=payload["original_entry_id"],
        external_id=payload["external_id"],
        metadata=payload.get("metadata", {}),
    )

    # Converte DTO → Command e delega para o CommandHandler
    command = create_reversal_command_from_dto(request_dto)

    try:
        entry = command_handler.handle_create_reversal(command)

        # Log estruturado de sucesso (Requisito 15.1)
        logger.info(
            "Reversão criada com sucesso",
            extra={
                "entry_id": entry.entry_id,
                "operation": operation,
                "result": "success",
                "original_entry_id": command.original_entry_id,
            },
        )

        entry_dto = journal_entry_to_response_dto(entry)
        response_data = _journal_entry_dto_to_dict(entry_dto)
        return _success_response(
            status_code=201,
            data=response_data,
            metadata={"request_id": request_id},
        )

    except IdempotencyConflict as exc:
        # Idempotência na reversão: retorna HTTP 200 com entry original
        logger.info(
            "Requisição idempotente detectada em %s",
            operation,
            extra={
                "operation": operation,
                "result": "idempotent",
                "external_id": command.external_id,
                "existing_entry_id": exc.existing_entry_id,
            },
        )
        return _success_response(
            status_code=200,
            data={"entry_id": exc.existing_entry_id, "idempotent": True},
            metadata={"request_id": request_id},
        )

    except DomainError as exc:
        # Erros de domínio conhecidos (JournalEntryNotFound, OptimisticLockConflict, etc.)
        logger.warning(
            "Erro de domínio em %s: %s",
            operation,
            exc.code,
            extra={
                "operation": operation,
                "result": "domain_error",
                "error_code": exc.code,
                "error_message": exc.message,
            },
        )
        return _error_response(exc.http_status, exc.code, exc.message)

    except Exception as exc:
        logger.error(
            "Erro inesperado em %s",
            operation,
            extra={
                "operation": operation,
                "result": "unexpected_error",
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
        return _error_response(500, "INTERNAL_ERROR", "Erro interno do servidor")


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------


def _parse_body(event: dict) -> tuple[Any, str | None]:
    """
    Desserializa o body JSON do evento Lambda.

    Suporta body como string (API Gateway) ou dict (invocação direta).

    Args:
        event: Evento Lambda.

    Returns:
        Tupla (payload_dict, error_message). Se parse falhar, payload é None
        e error_message contém a descrição do erro.
    """
    body = event.get("body")

    if body is None:
        return None, "Body da requisição está ausente"

    # API Gateway envia o body como string JSON
    if isinstance(body, str):
        try:
            return json.loads(body), None
        except json.JSONDecodeError as exc:
            return None, f"JSON inválido: {exc.msg}"

    # Invocação direta (testes) pode enviar body como dict
    if isinstance(body, dict):
        return body, None

    return None, f"Body deve ser JSON, recebido: {type(body).__name__}"


def _build_create_entry_dto(payload: dict) -> CreateJournalEntryRequestDTO:
    """
    Constrói CreateJournalEntryRequestDTO a partir do payload validado.

    Preserva o tipo raw do amount em cada posting para que o
    MinorUnitsValidator no domínio possa rejeitar tipos inválidos.
    Extrai tenant_id e policy_context quando presentes no payload.

    Args:
        payload: Dict do payload JSON já validado pelo schema validator.

    Returns:
        DTO de request pronto para conversão em Command.
    """
    posting_dtos = [
        PostingRequestDTO(
            account_id=p["account_id"],
            amount=p["amount"],       # tipo raw preservado intencionalmente
            currency=p["currency"],
            direction=p["direction"],
        )
        for p in payload["postings"]
    ]
    return CreateJournalEntryRequestDTO(
        external_id=payload["external_id"],
        postings=posting_dtos,
        tenant_id=payload.get("tenant_id", ""),
        policy_context=payload.get("policy_context", {}),
        metadata=payload.get("metadata", {}),
    )


def _journal_entry_dto_to_dict(entry_dto: Any) -> dict:
    """
    Serializa JournalEntryResponseDTO para dict compatível com JSON.

    Converte o DTO para um dict plano, incluindo a lista de postings
    serializada como lista de dicts.

    Args:
        entry_dto: JournalEntryResponseDTO a ser serializado.

    Returns:
        Dict pronto para serialização JSON.
    """
    return {
        "entry_id": entry_dto.entry_id,
        "external_id": entry_dto.external_id,
        "entry_type": entry_dto.entry_type,
        "postings": [
            {
                "account_id": p.account_id,
                "amount": p.amount,
                "currency": p.currency,
                "direction": p.direction,
                "index": p.index,
            }
            for p in entry_dto.postings
        ],
        "metadata": entry_dto.metadata,
        "timestamp": entry_dto.timestamp,
    }


def _success_response(status_code: int, data: Any, metadata: dict | None = None) -> dict:
    """
    Constrói resposta HTTP de sucesso no formato padrão da API (Requisito 16.1).

    Formato: {"status": "success", "data": {...}, "metadata": {...}}

    Args:
        status_code: HTTP status code (201, 200, etc.).
        data:        Dados da resposta.
        metadata:    Metadados opcionais (request_id, etc.).

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
        status_code: HTTP status code (400, 404, 409, 500, etc.).
        code:        Código de erro estruturado (ex: ZERO_SUM_VIOLATION).
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

    Tenta extrair do requestContext do API Gateway. Se não disponível,
    gera um UUID para garantir rastreabilidade.

    Args:
        event: Evento Lambda.

    Returns:
        String com o request ID.
    """
    request_context = event.get("requestContext", {})
    return request_context.get("requestId", str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Entrypoint Lambda — roteamento por routeKey
# ---------------------------------------------------------------------------

import os
import boto3
from ledger.infrastructure.dynamodb_repository import DynamoDBLedgerRepository
from ledger.domain.services import LedgerEngine
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.validators import ValidationChain, ZeroSumValidator, MinorUnitsValidator, TransactionLimitValidator


def _build_command_handler() -> CommandHandler:
    """
    Constrói o CommandHandler com dependências reais (DynamoDB).

    Lê DYNAMODB_TABLE_NAME da variável de ambiente injetada pelo Terraform.
    O cliente boto3 é criado uma vez por container Lambda (warm start).

    Se VALIDATION_ENGINE_ENABLED estiver definida como "true", injeta o
    PolicyValidationFacade na ValidationChain após os validadores estruturais.
    Isso permite ativar/desativar o motor de policy sem alterar código.
    """
    table_name = os.environ["DYNAMODB_TABLE_NAME"]
    dynamodb_client = boto3.client("dynamodb")
    repository = DynamoDBLedgerRepository(dynamodb_client=dynamodb_client, table_name=table_name)

    # Validadores estruturais — sempre presentes, executados primeiro.
    structural_validators: list = [
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
    ]

    # Injeta PolicyValidationFacade após validadores estruturais quando habilitado.
    # A facade é o último validador na cadeia (Requisito 1.3, 1.4, 7.4).
    validators = list(structural_validators)
    if os.environ.get("VALIDATION_ENGINE_ENABLED", "").lower() == "true":
        facade = _build_policy_validation_facade()
        if facade is not None:
            validators.append(facade)

    validation_chain = ValidationChain(validators=validators)
    factory = JournalEntryFactory()
    engine = LedgerEngine(repository=repository, validation_chain=validation_chain, factory=factory)
    return CommandHandler(engine=engine)


def _build_policy_validation_facade() -> object | None:
    """
    Constrói o PolicyValidationFacade com dependências reais.

    Retorna None se as dependências do validation engine não estiverem
    disponíveis (ex: módulo não instalado, variáveis de ambiente ausentes).
    Isso permite deploy gradual sem quebrar o ledger existente.

    Returns:
        PolicyValidationFacade configurado, ou None se indisponível.
    """
    try:
        from validation_engine.application.context_builder import (
            DefaultCanonicalValidationContextBuilder,
        )
        from validation_engine.application.facade import PolicyValidationFacade
        from validation_engine.application.runtime_registry import (
            DefaultPolicyRuntimeRegistry,
        )
        from validation_engine.domain.evaluator import RuleEvaluator
        from validation_engine.infrastructure.bundle_loader import S3BundleLoader
        from validation_engine.infrastructure.decision_trail_emitter import (
            FirehoseDecisionTrailEmitter,
        )
        from validation_engine.infrastructure.lkg_store import FileSystemLKGStore
        from validation_engine.infrastructure.manifest_resolver import (
            AppConfigManifestResolver,
        )
        from validation_engine.infrastructure.snapshot_loader import S3SnapshotLoader

        # Lê configuração do validation engine das variáveis de ambiente.
        bundle_bucket = os.environ.get("VALIDATION_ENGINE_BUNDLE_BUCKET", "")
        snapshot_bucket = os.environ.get("VALIDATION_ENGINE_SNAPSHOT_BUCKET", "")
        firehose_stream = os.environ.get("VALIDATION_ENGINE_FIREHOSE_STREAM", "")
        appconfig_app = os.environ.get("VALIDATION_ENGINE_APPCONFIG_APP", "")
        appconfig_env = os.environ.get("VALIDATION_ENGINE_APPCONFIG_ENV", "")
        appconfig_profile = os.environ.get("VALIDATION_ENGINE_APPCONFIG_PROFILE", "")

        s3_client = boto3.client("s3")
        firehose_client = boto3.client("firehose")
        appconfig_client = boto3.client("appconfig")

        bundle_loader = S3BundleLoader(s3_client=s3_client, bucket_name=bundle_bucket)
        snapshot_loader = S3SnapshotLoader(s3_client=s3_client, bucket_name=snapshot_bucket)
        manifest_resolver = AppConfigManifestResolver(
            client=appconfig_client,
            application_id=appconfig_app,
            environment_id=appconfig_env,
            configuration_profile_id=appconfig_profile,
        )
        lkg_store = FileSystemLKGStore()

        registry = DefaultPolicyRuntimeRegistry(
            manifest_resolver=manifest_resolver,
            bundle_loader=bundle_loader,
            snapshot_loader=snapshot_loader,
            lkg_store=lkg_store,
        )

        context_builder = DefaultCanonicalValidationContextBuilder()
        evaluator = RuleEvaluator()
        trail_emitter = FirehoseDecisionTrailEmitter(
            firehose_client=firehose_client,
            stream_name=firehose_stream,
        )

        return PolicyValidationFacade(
            context_builder=context_builder,
            runtime_registry=registry,
            evaluator=evaluator,
            trail_emitter=trail_emitter,
        )
    except (ImportError, KeyError, Exception) as exc:
        logger.warning(
            "PolicyValidationFacade não disponível: %s. "
            "Validation engine desabilitado para este container.",
            str(exc),
        )
        return None


# Instância reutilizada entre invocações (warm start)
_command_handler: CommandHandler | None = None


def handler(event: dict, context: Any) -> dict:
    """
    Entrypoint da Write Lambda.

    Roteia para o handler correto com base no routeKey do API Gateway v2:
      - POST /entries   → handle_create_entry
      - POST /reversals → handle_create_reversal

    Args:
        event:   Evento Lambda (API Gateway HTTP API v2 proxy format).
        context: Contexto Lambda.

    Returns:
        Dict no formato API Gateway proxy response.
    """
    global _command_handler
    if _command_handler is None:
        _command_handler = _build_command_handler()

    route_key = event.get("routeKey", "")

    if route_key == "POST /entries":
        return handle_create_entry(event, context, _command_handler)
    elif route_key == "POST /reversals":
        return handle_create_reversal(event, context, _command_handler)
    else:
        return _error_response(404, "ROUTE_NOT_FOUND", f"Rota não encontrada: {route_key}")
