"""
DTOs (Data Transfer Objects) da camada de aplicação do Double-Entry Ledger.

Atuam como Anti-Corruption Layer (ACL) entre a API externa e o domínio interno.
Isolam o domínio de mudanças no contrato da API e vice-versa.

Responsabilidades:
- Definir os contratos de request/response da API (Requisito 16.1, 16.2)
- Converter DTOs de request → Commands/Queries do domínio
- Converter objetos de domínio → DTOs de response

Estrutura dos contratos de API:
- Sucesso: {"status": "success", "data": {...}, "metadata": {...}}
- Erro:    {"error": {"code": "<ERROR_CODE>", "message": "<descrição>"}}

Módulo organizado em:
1. DTOs de Request (entrada da API)
2. DTOs de Response (saída da API)
3. Funções de conversão Request → Command/Query
4. Funções de conversão Domain → Response DTO

Requisitos atendidos: 16.1, 16.2
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ledger.application.commands import (
    CreateJournalEntryCommand,
    CreateReversalCommand,
    PostingInput,
)
from ledger.application.queries import GetBalanceQuery, GetStatementQuery
from ledger.domain.aggregates import JournalEntry
from ledger.domain.ports import StatementPage
from ledger.domain.value_objects import Balance, Posting


# ===========================================================================
# DTOs de Request — entrada da API
# ===========================================================================


@dataclass
class PostingRequestDTO:
    """
    DTO de entrada para um posting individual na requisição da API.

    Representa os dados brutos de um posting como recebidos pela API.
    A validação de tipos (int > 0) é feita pelo MinorUnitsValidator
    na ValidationChain — este DTO apenas transporta os dados.

    Campos:
        account_id: Identificador da conta afetada.
        amount:     Valor em minor units. Tipo raw (object) para que o
                    validador possa rejeitar float/bool/string.
        currency:   Código ISO 4217 (ex: "BRL", "USD").
        direction:  "DEBIT" ou "CREDIT".
    """

    account_id: str
    amount: object  # raw — validado pelo MinorUnitsValidator
    currency: str
    direction: str  # "DEBIT" | "CREDIT"


@dataclass
class CreateJournalEntryRequestDTO:
    """
    DTO de entrada para criação de lançamento contábil (POST /entries).

    Representa o payload JSON da requisição de criação de lançamento.
    Convertido para CreateJournalEntryCommand antes de ser passado ao engine.

    Campos:
        external_id:    Chave de idempotência fornecida pelo caller.
        postings:       Lista de postings que compõem o lançamento.
        tenant_id:      Identificador do tenant (Requisito 5.1).
        policy_context: Dados visíveis à DSL de policy (Requisito 8.2).
        metadata:       Dados adicionais opcionais (ex: order_id).
    """

    external_id: str
    postings: list[PostingRequestDTO]
    tenant_id: str = ""
    policy_context: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass
class CreateReversalRequestDTO:
    """
    DTO de entrada para criação de reversão (POST /reversals).

    Representa o payload JSON da requisição de reversão de lançamento.
    Convertido para CreateReversalCommand antes de ser passado ao engine.

    Campos:
        original_entry_id: entry_id do lançamento a ser revertido.
        external_id:       Chave de idempotência para a operação de reversão.
        metadata:          Dados adicionais opcionais (ex: reason).
    """

    original_entry_id: str
    external_id: str
    metadata: dict = field(default_factory=dict)


@dataclass
class GetBalanceRequestDTO:
    """
    DTO de entrada para consulta de saldo (GET /balances/{account_id}).

    Representa os parâmetros da requisição de consulta de saldo.
    Convertido para GetBalanceQuery antes de ser passado ao QueryHandler.

    Campos:
        account_id: Identificador da conta.
        currency:   Código ISO 4217 da moeda.
    """

    account_id: str
    currency: str


@dataclass
class GetStatementRequestDTO:
    """
    DTO de entrada para consulta de extrato (GET /statements/{account_id}).

    Representa os parâmetros da requisição de consulta de extrato paginado.
    Convertido para GetStatementQuery antes de ser passado ao QueryHandler.

    Campos:
        account_id: Identificador da conta.
        cursor:     Cursor de paginação (posting_sort_key do último item).
                    None para a primeira página.
        page_size:  Número máximo de postings por página. Padrão: 20.
    """

    account_id: str
    cursor: str | None = None
    page_size: int = 20


# ===========================================================================
# DTOs de Response — saída da API
# ===========================================================================


@dataclass
class PostingResponseDTO:
    """
    DTO de saída para um posting individual na resposta da API.

    Representa um Posting do domínio serializado para JSON.
    Expõe apenas os campos relevantes para o consumidor da API.
    """

    account_id: str
    amount: int       # minor units
    currency: str     # ISO 4217
    direction: str    # "DEBIT" | "CREDIT"
    index: int        # posição ordinal dentro do JournalEntry


@dataclass
class JournalEntryResponseDTO:
    """
    DTO de saída para um lançamento contábil na resposta da API.

    Representa um JournalEntry do domínio serializado para JSON.
    Usado nas respostas de POST /entries e POST /reversals.

    Campos:
        entry_id:    UUID v4 do lançamento criado.
        external_id: Chave de idempotência fornecida pelo caller.
        entry_type:  "STANDARD" | "REVERSAL".
        postings:    Lista de postings do lançamento.
        metadata:    Dados adicionais do lançamento.
        timestamp:   Timestamp de criação (ISO 8601).
    """

    entry_id: str
    external_id: str
    entry_type: str
    postings: list[PostingResponseDTO]
    metadata: dict
    timestamp: str


@dataclass
class BalanceResponseDTO:
    """
    DTO de saída para saldo de conta na resposta da API.

    Representa um Balance do domínio serializado para JSON.
    Usado na resposta de GET /balances/{account_id}.

    Campos:
        account_id:     Identificador da conta.
        currency:       Código ISO 4217 da moeda.
        balance_amount: Saldo em minor units (pode ser negativo em contas de plataforma).
        version:        Versão OCC do saldo (para informação; não exposto para escrita).
        last_update:    Timestamp da última atualização (ISO 8601).
    """

    account_id: str
    currency: str
    balance_amount: int
    version: int
    last_update: str


@dataclass
class StatementResponseDTO:
    """
    DTO de saída para extrato paginado na resposta da API.

    Representa uma StatementPage do domínio serializada para JSON.
    Usado na resposta de GET /statements/{account_id}.

    Campos:
        postings:    Lista de postings da página atual.
        next_cursor: Cursor para a próxima página. None se for a última.
        has_more:    True se existem mais postings além desta página.
    """

    postings: list[PostingResponseDTO]
    next_cursor: str | None
    has_more: bool


@dataclass
class SuccessResponseDTO:
    """
    DTO de resposta de sucesso no formato padrão da API.

    Formato: {"status": "success", "data": {...}, "metadata": {...}}

    Requisito 16.1: todas as respostas de sucesso seguem este formato.
    """

    data: Any
    metadata: dict = field(default_factory=dict)
    status: str = "success"

    def to_dict(self) -> dict:
        """Serializa para dict compatível com JSON."""
        return {
            "status": self.status,
            "data": self.data,
            "metadata": self.metadata,
        }


@dataclass
class ErrorResponseDTO:
    """
    DTO de resposta de erro no formato padrão da API.

    Formato: {"error": {"code": "<ERROR_CODE>", "message": "<descrição>"}}

    Requisito 16.2: todas as respostas de erro seguem este formato.
    """

    code: str
    message: str

    def to_dict(self) -> dict:
        """Serializa para dict compatível com JSON."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
            }
        }


# ===========================================================================
# Conversões: Request DTO → Command / Query
# ===========================================================================


def create_journal_entry_command_from_dto(
    dto: CreateJournalEntryRequestDTO,
) -> CreateJournalEntryCommand:
    """
    Converte CreateJournalEntryRequestDTO → CreateJournalEntryCommand.

    Mapeia cada PostingRequestDTO para PostingInput, preservando os tipos
    raw para que o MinorUnitsValidator possa rejeitar float/bool/string.
    Propaga tenant_id e policy_context para o comando.

    Args:
        dto: DTO de request recebido da API.

    Returns:
        Comando pronto para ser passado ao CommandHandler.
    """
    posting_inputs = [
        PostingInput(
            account_id=posting_dto.account_id,
            amount=posting_dto.amount,
            currency=posting_dto.currency,
            direction=posting_dto.direction,
        )
        for posting_dto in dto.postings
    ]
    return CreateJournalEntryCommand(
        external_id=dto.external_id,
        postings=posting_inputs,
        tenant_id=dto.tenant_id,
        policy_context=dto.policy_context,
        metadata=dto.metadata,
    )


def create_reversal_command_from_dto(
    dto: CreateReversalRequestDTO,
) -> CreateReversalCommand:
    """
    Converte CreateReversalRequestDTO → CreateReversalCommand.

    Args:
        dto: DTO de request recebido da API.

    Returns:
        Comando pronto para ser passado ao CommandHandler.
    """
    return CreateReversalCommand(
        original_entry_id=dto.original_entry_id,
        external_id=dto.external_id,
        metadata=dto.metadata,
    )


def get_balance_query_from_dto(dto: GetBalanceRequestDTO) -> GetBalanceQuery:
    """
    Converte GetBalanceRequestDTO → GetBalanceQuery.

    Args:
        dto: DTO de request recebido da API.

    Returns:
        Query pronta para ser passada ao QueryHandler.
    """
    return GetBalanceQuery(
        account_id=dto.account_id,
        currency=dto.currency,
    )


def get_statement_query_from_dto(dto: GetStatementRequestDTO) -> GetStatementQuery:
    """
    Converte GetStatementRequestDTO → GetStatementQuery.

    Args:
        dto: DTO de request recebido da API.

    Returns:
        Query pronta para ser passada ao QueryHandler.
    """
    return GetStatementQuery(
        account_id=dto.account_id,
        cursor=dto.cursor,
        page_size=dto.page_size,
    )


# ===========================================================================
# Conversões: Domain → Response DTO
# ===========================================================================


def journal_entry_to_response_dto(entry: JournalEntry) -> JournalEntryResponseDTO:
    """
    Converte JournalEntry (domínio) → JournalEntryResponseDTO.

    Serializa o aggregate root para o formato de resposta da API,
    convertendo cada Posting para PostingResponseDTO.

    Args:
        entry: JournalEntry do domínio.

    Returns:
        DTO de response pronto para serialização JSON.
    """
    return JournalEntryResponseDTO(
        entry_id=entry.entry_id,
        external_id=entry.external_id,
        entry_type=entry.entry_type.value,
        postings=[_posting_to_response_dto(p) for p in entry.postings],
        metadata=entry.metadata,
        timestamp=entry.timestamp,
    )


def balance_to_response_dto(balance: Balance) -> BalanceResponseDTO:
    """
    Converte Balance (domínio) → BalanceResponseDTO.

    Args:
        balance: Balance do domínio com saldo materializado.

    Returns:
        DTO de response pronto para serialização JSON.
    """
    return BalanceResponseDTO(
        account_id=balance.account_id,
        currency=balance.currency,
        balance_amount=balance.balance_amount,
        version=balance.version,
        last_update=balance.last_update,
    )


def statement_page_to_response_dto(page: StatementPage) -> StatementResponseDTO:
    """
    Converte StatementPage (domínio) → StatementResponseDTO.

    Args:
        page: StatementPage com postings paginados.

    Returns:
        DTO de response pronto para serialização JSON.
    """
    return StatementResponseDTO(
        postings=[_posting_to_response_dto(p) for p in page.postings],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


def _posting_to_response_dto(posting: Posting) -> PostingResponseDTO:
    """
    Converte Posting (Value Object do domínio) → PostingResponseDTO.

    Função auxiliar privada usada pelas conversões de JournalEntry e StatementPage.

    Args:
        posting: Posting do domínio.

    Returns:
        DTO de response com campos do posting.
    """
    return PostingResponseDTO(
        account_id=posting.account_id,
        amount=posting.money.amount,
        currency=posting.money.currency,
        direction=posting.direction.value,
        index=posting.index,
    )
