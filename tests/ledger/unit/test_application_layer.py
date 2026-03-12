"""
Testes unitários para a camada de aplicação do Double-Entry Ledger.

Cobre:
- CommandHandler: delegação para LedgerEngine (create_journal_entry, create_reversal)
- QueryHandler: delegação para LedgerRepository (get_balance, get_statement)
- DTOs: conversão Request DTO → Command/Query e Domain → Response DTO
- Contratos de resposta da API (SuccessResponseDTO, ErrorResponseDTO)

Requisitos validados: 6.1, 8.1, 16.1, 16.2
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from ledger.application.commands import (
    CreateJournalEntryCommand,
    CreateReversalCommand,
    PostingInput,
)
from ledger.application.dtos import (
    BalanceResponseDTO,
    CreateJournalEntryRequestDTO,
    CreateReversalRequestDTO,
    ErrorResponseDTO,
    GetBalanceRequestDTO,
    GetStatementRequestDTO,
    JournalEntryResponseDTO,
    PostingRequestDTO,
    PostingResponseDTO,
    StatementResponseDTO,
    SuccessResponseDTO,
    balance_to_response_dto,
    create_journal_entry_command_from_dto,
    create_reversal_command_from_dto,
    get_balance_query_from_dto,
    get_statement_query_from_dto,
    journal_entry_to_response_dto,
    statement_page_to_response_dto,
)
from ledger.application.handlers import CommandHandler, QueryHandler
from ledger.application.queries import GetBalanceQuery, GetStatementQuery
from ledger.domain.aggregates import JournalEntry
from ledger.domain.errors import (
    IdempotencyConflict,
    JournalEntryNotFound,
    ZeroSumViolation,
)
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.ports import StatementPage
from ledger.domain.services import LedgerEngine
from ledger.domain.validators import (
    MinorUnitsValidator,
    TransactionLimitValidator,
    ValidationChain,
    ZeroSumValidator,
)
from ledger.domain.value_objects import Balance, Direction, EntryType, Money, Posting


# ---------------------------------------------------------------------------
# InMemoryLedgerRepository — repositório in-memory para testes unitários
# ---------------------------------------------------------------------------


class InMemoryLedgerRepository:
    """Repositório in-memory que satisfaz o protocolo LedgerRepository."""

    def __init__(self) -> None:
        self._entries: dict[str, JournalEntry] = {}
        self._by_external_id: dict[str, JournalEntry] = {}
        self._balances: dict[tuple[str, str], Balance] = {}

    def save_journal_entry(self, journal_entry: JournalEntry) -> None:
        self._entries[journal_entry.entry_id] = journal_entry
        self._by_external_id[journal_entry.external_id] = journal_entry

    def find_journal_entry_by_id(self, entry_id: str) -> JournalEntry | None:
        return self._entries.get(entry_id)

    def find_journal_entry_by_external_id(self, external_id: str) -> JournalEntry | None:
        return self._by_external_id.get(external_id)

    def get_balance(self, account_id: str, currency: str) -> Balance | None:
        return self._balances.get((account_id, currency))

    def get_statement(self, account_id: str, cursor, page_size: int) -> StatementPage:
        # Retorna postings da conta filtrados por account_id
        all_postings = [
            p
            for entry in self._entries.values()
            for p in entry.postings
            if p.account_id == account_id
        ]
        return StatementPage(postings=all_postings, next_cursor=None, has_more=False)

    def set_balance(self, balance: Balance) -> None:
        """Helper para configurar saldo nos testes."""
        self._balances[(balance.account_id, balance.currency)] = balance


# ---------------------------------------------------------------------------
# Helpers de construção
# ---------------------------------------------------------------------------


def _make_balanced_command(
    external_id: str | None = None,
    amount: int = 1000,
    currency: str = "BRL",
) -> CreateJournalEntryCommand:
    """Cria um CreateJournalEntryCommand balanceado para testes."""
    return CreateJournalEntryCommand(
        external_id=external_id or str(uuid.uuid4()),
        postings=[
            PostingInput(account_id="acc_available", amount=amount, currency=currency, direction="DEBIT"),
            PostingInput(account_id="acc_hold", amount=amount, currency=currency, direction="CREDIT"),
        ],
        metadata={},
    )


def _build_engine(
    repo: InMemoryLedgerRepository | None = None,
) -> tuple[LedgerEngine, InMemoryLedgerRepository]:
    """Constrói LedgerEngine com repositório in-memory."""
    repository = repo or InMemoryLedgerRepository()
    chain = ValidationChain([
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
    ])
    factory = JournalEntryFactory()
    engine = LedgerEngine(repository=repository, validation_chain=chain, factory=factory)
    return engine, repository


def _make_journal_entry(
    amount: int = 1000,
    currency: str = "BRL",
    external_id: str | None = None,
) -> JournalEntry:
    """Cria um JournalEntry via factory para uso nos testes."""
    factory = JournalEntryFactory()
    command = _make_balanced_command(external_id=external_id, amount=amount, currency=currency)
    return factory.create_standard(command)


# ===========================================================================
# 1. CommandHandler
# ===========================================================================


@pytest.mark.unit
class TestCommandHandlerCreateJournalEntry:
    """
    Testa o CommandHandler.handle_create_journal_entry.

    Verifica que o handler delega corretamente para o LedgerEngine.
    """

    def test_handle_create_journal_entry_delegates_to_engine(self) -> None:
        """CommandHandler deve delegar para LedgerEngine e retornar o JournalEntry."""
        engine, repo = _build_engine()
        handler = CommandHandler(engine=engine)
        command = _make_balanced_command()

        entry = handler.handle_create_journal_entry(command)

        assert entry is not None
        assert entry.external_id == command.external_id
        assert entry.entry_type == EntryType.STANDARD

    def test_handle_create_journal_entry_persists_entry(self) -> None:
        """Após handle, o JournalEntry deve estar persistido no repositório."""
        engine, repo = _build_engine()
        handler = CommandHandler(engine=engine)
        command = _make_balanced_command()

        entry = handler.handle_create_journal_entry(command)

        # Verifica que o entry foi persistido
        persisted = repo.find_journal_entry_by_id(entry.entry_id)
        assert persisted is not None
        assert persisted.entry_id == entry.entry_id

    def test_handle_create_journal_entry_propagates_domain_errors(self) -> None:
        """Erros de domínio do engine devem ser propagados pelo handler."""
        engine, repo = _build_engine()
        handler = CommandHandler(engine=engine)

        # Comando desbalanceado → ZeroSumViolation
        unbalanced_command = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(account_id="acc_a", amount=1000, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_b", amount=500, currency="BRL", direction="CREDIT"),
            ],
            metadata={},
        )

        with pytest.raises(ZeroSumViolation):
            handler.handle_create_journal_entry(unbalanced_command)

    def test_handle_create_journal_entry_idempotency_raises_conflict(self) -> None:
        """Submissão duplicada com mesmo external_id deve levantar IdempotencyConflict."""
        engine, repo = _build_engine()
        handler = CommandHandler(engine=engine)
        external_id = str(uuid.uuid4())
        command = _make_balanced_command(external_id=external_id)

        # Primeira submissão — sucesso
        handler.handle_create_journal_entry(command)

        # Segunda submissão com mesmo external_id — IdempotencyConflict
        with pytest.raises(IdempotencyConflict) as exc_info:
            handler.handle_create_journal_entry(command)

        assert exc_info.value.existing_entry_id is not None


@pytest.mark.unit
class TestCommandHandlerCreateReversal:
    """
    Testa o CommandHandler.handle_create_reversal.

    Verifica que o handler delega corretamente para o LedgerEngine.
    """

    def test_handle_create_reversal_delegates_to_engine(self) -> None:
        """CommandHandler deve delegar reversão para LedgerEngine."""
        engine, repo = _build_engine()
        handler = CommandHandler(engine=engine)

        # Cria lançamento original
        original_command = _make_balanced_command()
        original_entry = handler.handle_create_journal_entry(original_command)

        # Cria reversão
        reversal_command = CreateReversalCommand(
            original_entry_id=original_entry.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={"reason": "test_reversal"},
        )
        reversal = handler.handle_create_reversal(reversal_command)

        assert reversal is not None
        assert reversal.entry_type == EntryType.REVERSAL
        assert reversal.metadata.get("original_entry_id") == original_entry.entry_id

    def test_handle_create_reversal_not_found_raises_error(self) -> None:
        """Reversão de entry inexistente deve levantar JournalEntryNotFound."""
        engine, repo = _build_engine()
        handler = CommandHandler(engine=engine)

        reversal_command = CreateReversalCommand(
            original_entry_id=str(uuid.uuid4()),  # ID inexistente
            external_id=str(uuid.uuid4()),
            metadata={},
        )

        with pytest.raises(JournalEntryNotFound):
            handler.handle_create_reversal(reversal_command)


# ===========================================================================
# 2. QueryHandler
# ===========================================================================


@pytest.mark.unit
class TestQueryHandlerGetBalance:
    """
    Testa o QueryHandler.handle_get_balance.

    Verifica que o handler delega para o repositório e retorna o saldo.
    """

    def test_handle_get_balance_returns_balance_when_exists(self) -> None:
        """Deve retornar Balance quando a conta tem saldo registrado."""
        repo = InMemoryLedgerRepository()
        balance = Balance(
            account_id="acc_available_001",
            currency="BRL",
            balance_amount=5000,
            version=3,
            last_update="2026-03-10T14:00:00Z",
        )
        repo.set_balance(balance)

        engine, _ = _build_engine(repo)
        handler = QueryHandler(repository=repo)
        query = GetBalanceQuery(account_id="acc_available_001", currency="BRL")

        result = handler.handle_get_balance(query)

        assert result is not None
        assert result.account_id == "acc_available_001"
        assert result.currency == "BRL"
        assert result.balance_amount == 5000
        assert result.version == 3

    def test_handle_get_balance_returns_none_when_not_found(self) -> None:
        """Deve retornar None quando a conta não tem saldo registrado."""
        repo = InMemoryLedgerRepository()
        handler = QueryHandler(repository=repo)
        query = GetBalanceQuery(account_id="acc_nonexistent", currency="BRL")

        result = handler.handle_get_balance(query)

        assert result is None

    def test_handle_get_balance_delegates_to_repository(self) -> None:
        """QueryHandler deve delegar para o repositório sem lógica adicional."""
        mock_repo = MagicMock()
        expected_balance = Balance(
            account_id="acc_001",
            currency="USD",
            balance_amount=10000,
            version=1,
            last_update="2026-03-10T00:00:00Z",
        )
        mock_repo.get_balance.return_value = expected_balance

        handler = QueryHandler(repository=mock_repo)
        query = GetBalanceQuery(account_id="acc_001", currency="USD")

        result = handler.handle_get_balance(query)

        mock_repo.get_balance.assert_called_once_with(account_id="acc_001", currency="USD")
        assert result == expected_balance


@pytest.mark.unit
class TestQueryHandlerGetStatement:
    """
    Testa o QueryHandler.handle_get_statement.

    Verifica que o handler delega para o repositório com paginação correta.
    """

    def test_handle_get_statement_returns_statement_page(self) -> None:
        """Deve retornar StatementPage com postings da conta."""
        repo = InMemoryLedgerRepository()
        engine, _ = _build_engine(repo)

        # Cria um lançamento para gerar postings
        command = _make_balanced_command()
        engine.create_journal_entry(command)

        handler = QueryHandler(repository=repo)
        query = GetStatementQuery(account_id="acc_available", cursor=None, page_size=10)

        result = handler.handle_get_statement(query)

        assert isinstance(result, StatementPage)
        assert len(result.postings) >= 1

    def test_handle_get_statement_empty_when_no_postings(self) -> None:
        """Deve retornar StatementPage vazia quando não há postings."""
        repo = InMemoryLedgerRepository()
        handler = QueryHandler(repository=repo)
        query = GetStatementQuery(account_id="acc_nonexistent", cursor=None, page_size=10)

        result = handler.handle_get_statement(query)

        assert isinstance(result, StatementPage)
        assert result.postings == []
        assert result.has_more is False

    def test_handle_get_statement_delegates_to_repository(self) -> None:
        """QueryHandler deve delegar para o repositório com os parâmetros corretos."""
        mock_repo = MagicMock()
        expected_page = StatementPage(postings=[], next_cursor=None, has_more=False)
        mock_repo.get_statement.return_value = expected_page

        handler = QueryHandler(repository=mock_repo)
        query = GetStatementQuery(account_id="acc_001", cursor="cursor_abc", page_size=5)

        result = handler.handle_get_statement(query)

        mock_repo.get_statement.assert_called_once_with(
            account_id="acc_001",
            cursor="cursor_abc",
            page_size=5,
        )
        assert result == expected_page


# ===========================================================================
# 3. Conversões Request DTO → Command / Query
# ===========================================================================


@pytest.mark.unit
class TestRequestDTOToCommandConversions:
    """
    Testa as funções de conversão de Request DTOs para Commands e Queries.
    """

    def test_create_journal_entry_command_from_dto_maps_all_fields(self) -> None:
        """Todos os campos do DTO devem ser mapeados para o Command."""
        dto = CreateJournalEntryRequestDTO(
            external_id="ext-001",
            postings=[
                PostingRequestDTO(account_id="acc_a", amount=1000, currency="BRL", direction="DEBIT"),
                PostingRequestDTO(account_id="acc_b", amount=1000, currency="BRL", direction="CREDIT"),
            ],
            metadata={"order_id": "order-001"},
        )

        command = create_journal_entry_command_from_dto(dto)

        assert command.external_id == "ext-001"
        assert len(command.postings) == 2
        assert command.postings[0].account_id == "acc_a"
        assert command.postings[0].amount == 1000
        assert command.postings[0].currency == "BRL"
        assert command.postings[0].direction == "DEBIT"
        assert command.postings[1].direction == "CREDIT"
        assert command.metadata == {"order_id": "order-001"}

    def test_create_journal_entry_command_preserves_raw_amount_type(self) -> None:
        """
        O amount raw (incluindo float) deve ser preservado no Command para que
        o MinorUnitsValidator possa rejeitar tipos inválidos.
        """
        dto = CreateJournalEntryRequestDTO(
            external_id="ext-002",
            postings=[
                PostingRequestDTO(account_id="acc_a", amount=10.50, currency="BRL", direction="DEBIT"),
            ],
            metadata={},
        )

        command = create_journal_entry_command_from_dto(dto)

        # O amount float deve ser preservado (não convertido) para validação posterior
        assert command.postings[0].amount == 10.50
        assert isinstance(command.postings[0].amount, float)

    def test_create_reversal_command_from_dto_maps_all_fields(self) -> None:
        """Todos os campos do DTO de reversão devem ser mapeados para o Command."""
        dto = CreateReversalRequestDTO(
            original_entry_id="entry-uuid-001",
            external_id="reversal-ext-001",
            metadata={"reason": "customer_refund"},
        )

        command = create_reversal_command_from_dto(dto)

        assert command.original_entry_id == "entry-uuid-001"
        assert command.external_id == "reversal-ext-001"
        assert command.metadata == {"reason": "customer_refund"}

    def test_get_balance_query_from_dto_maps_fields(self) -> None:
        """DTO de consulta de saldo deve ser mapeado para GetBalanceQuery."""
        dto = GetBalanceRequestDTO(account_id="acc_available_001", currency="BRL")

        query = get_balance_query_from_dto(dto)

        assert query.account_id == "acc_available_001"
        assert query.currency == "BRL"

    def test_get_statement_query_from_dto_maps_all_fields(self) -> None:
        """DTO de consulta de extrato deve ser mapeado para GetStatementQuery."""
        dto = GetStatementRequestDTO(
            account_id="acc_available_001",
            cursor="POSTING#2026-03-10T14:00:00Z#uuid-abc#0",
            page_size=10,
        )

        query = get_statement_query_from_dto(dto)

        assert query.account_id == "acc_available_001"
        assert query.cursor == "POSTING#2026-03-10T14:00:00Z#uuid-abc#0"
        assert query.page_size == 10

    def test_get_statement_query_from_dto_defaults(self) -> None:
        """GetStatementQuery deve ter cursor=None e page_size=20 por padrão."""
        dto = GetStatementRequestDTO(account_id="acc_001")

        query = get_statement_query_from_dto(dto)

        assert query.cursor is None
        assert query.page_size == 20


# ===========================================================================
# 4. Conversões Domain → Response DTO
# ===========================================================================


@pytest.mark.unit
class TestDomainToResponseDTOConversions:
    """
    Testa as funções de conversão de objetos de domínio para Response DTOs.
    """

    def test_journal_entry_to_response_dto_maps_all_fields(self) -> None:
        """JournalEntry do domínio deve ser convertido para JournalEntryResponseDTO."""
        entry = _make_journal_entry(amount=1050, currency="BRL")

        dto = journal_entry_to_response_dto(entry)

        assert isinstance(dto, JournalEntryResponseDTO)
        assert dto.entry_id == entry.entry_id
        assert dto.external_id == entry.external_id
        assert dto.entry_type == "STANDARD"
        assert len(dto.postings) == 2
        assert dto.timestamp == entry.timestamp

    def test_journal_entry_to_response_dto_converts_postings(self) -> None:
        """Postings do JournalEntry devem ser convertidos para PostingResponseDTO."""
        entry = _make_journal_entry(amount=1050, currency="BRL")

        dto = journal_entry_to_response_dto(entry)

        debit_posting = dto.postings[0]
        assert isinstance(debit_posting, PostingResponseDTO)
        assert debit_posting.account_id == "acc_available"
        assert debit_posting.amount == 1050
        assert debit_posting.currency == "BRL"
        assert debit_posting.direction == "DEBIT"
        assert debit_posting.index == 0

        credit_posting = dto.postings[1]
        assert credit_posting.direction == "CREDIT"
        assert credit_posting.index == 1

    def test_journal_entry_to_response_dto_entry_type_is_string(self) -> None:
        """entry_type no DTO deve ser string, não enum."""
        entry = _make_journal_entry()

        dto = journal_entry_to_response_dto(entry)

        assert isinstance(dto.entry_type, str)
        assert dto.entry_type == "STANDARD"

    def test_balance_to_response_dto_maps_all_fields(self) -> None:
        """Balance do domínio deve ser convertido para BalanceResponseDTO."""
        balance = Balance(
            account_id="acc_available_001",
            currency="BRL",
            balance_amount=5000,
            version=3,
            last_update="2026-03-10T14:00:00Z",
        )

        dto = balance_to_response_dto(balance)

        assert isinstance(dto, BalanceResponseDTO)
        assert dto.account_id == "acc_available_001"
        assert dto.currency == "BRL"
        assert dto.balance_amount == 5000
        assert dto.version == 3
        assert dto.last_update == "2026-03-10T14:00:00Z"

    def test_statement_page_to_response_dto_maps_postings(self) -> None:
        """StatementPage do domínio deve ser convertida para StatementResponseDTO."""
        posting = Posting(
            account_id="acc_available_001",
            money=Money(amount=1000, currency="BRL"),
            direction=Direction.DEBIT,
            index=0,
        )
        page = StatementPage(
            postings=[posting],
            next_cursor="POSTING#2026-03-10T14:00:00Z#uuid-abc#0",
            has_more=True,
        )

        dto = statement_page_to_response_dto(page)

        assert isinstance(dto, StatementResponseDTO)
        assert len(dto.postings) == 1
        assert dto.postings[0].account_id == "acc_available_001"
        assert dto.postings[0].amount == 1000
        assert dto.postings[0].direction == "DEBIT"
        assert dto.next_cursor == "POSTING#2026-03-10T14:00:00Z#uuid-abc#0"
        assert dto.has_more is True

    def test_statement_page_to_response_dto_empty_page(self) -> None:
        """StatementPage vazia deve ser convertida para StatementResponseDTO vazia."""
        page = StatementPage(postings=[], next_cursor=None, has_more=False)

        dto = statement_page_to_response_dto(page)

        assert dto.postings == []
        assert dto.next_cursor is None
        assert dto.has_more is False


# ===========================================================================
# 5. Contratos de resposta da API (SuccessResponseDTO, ErrorResponseDTO)
# ===========================================================================


@pytest.mark.unit
class TestAPIResponseContracts:
    """
    Testa os contratos de resposta da API (Requisito 16.1, 16.2).
    """

    def test_success_response_dto_format(self) -> None:
        """SuccessResponseDTO deve serializar no formato {"status": "success", "data": {...}, "metadata": {...}}."""
        entry = _make_journal_entry()
        entry_dto = journal_entry_to_response_dto(entry)

        response = SuccessResponseDTO(
            data={"entry_id": entry_dto.entry_id},
            metadata={"request_id": "req-001"},
        )
        result = response.to_dict()

        assert result["status"] == "success"
        assert "data" in result
        assert "metadata" in result
        assert result["data"]["entry_id"] == entry_dto.entry_id
        assert result["metadata"]["request_id"] == "req-001"

    def test_success_response_dto_default_metadata(self) -> None:
        """SuccessResponseDTO deve ter metadata vazio por padrão."""
        response = SuccessResponseDTO(data={"key": "value"})
        result = response.to_dict()

        assert result["metadata"] == {}

    def test_error_response_dto_format(self) -> None:
        """ErrorResponseDTO deve serializar no formato {"error": {"code": ..., "message": ...}}."""
        response = ErrorResponseDTO(
            code="ZERO_SUM_VIOLATION",
            message="Postings não somam zero para moeda BRL: total=500",
        )
        result = response.to_dict()

        assert "error" in result
        assert result["error"]["code"] == "ZERO_SUM_VIOLATION"
        assert "Postings não somam zero" in result["error"]["message"]

    def test_error_response_dto_no_extra_fields(self) -> None:
        """ErrorResponseDTO não deve expor campos além de 'error'."""
        response = ErrorResponseDTO(code="SOME_ERROR", message="Some message")
        result = response.to_dict()

        assert list(result.keys()) == ["error"]
        assert list(result["error"].keys()) == ["code", "message"]

    def test_success_response_dto_no_error_field(self) -> None:
        """SuccessResponseDTO não deve conter campo 'error'."""
        response = SuccessResponseDTO(data={})
        result = response.to_dict()

        assert "error" not in result


# ===========================================================================
# 6. Integração CommandHandler + QueryHandler (fluxo completo)
# ===========================================================================


@pytest.mark.unit
class TestApplicationLayerIntegration:
    """
    Testa o fluxo completo da camada de aplicação com CommandHandler e QueryHandler.

    Verifica que os handlers trabalham corretamente em conjunto com o repositório.
    """

    def test_create_entry_and_query_statement(self) -> None:
        """
        Cria um lançamento via CommandHandler e consulta o extrato via QueryHandler.

        Verifica que o posting criado aparece no extrato da conta.
        """
        repo = InMemoryLedgerRepository()
        engine, _ = _build_engine(repo)
        command_handler = CommandHandler(engine=engine)
        query_handler = QueryHandler(repository=repo)

        # Cria lançamento
        command = _make_balanced_command(amount=2000, currency="BRL")
        entry = command_handler.handle_create_journal_entry(command)

        # Consulta extrato da conta Available
        query = GetStatementQuery(account_id="acc_available", cursor=None, page_size=10)
        page = query_handler.handle_get_statement(query)

        assert len(page.postings) >= 1
        # O posting de DEBIT em acc_available deve aparecer no extrato
        debit_postings = [p for p in page.postings if p.direction == Direction.DEBIT]
        assert len(debit_postings) >= 1
        assert debit_postings[0].money.amount == 2000

    def test_create_reversal_and_verify_metadata(self) -> None:
        """
        Cria um lançamento e sua reversão, verificando que o metadata da reversão
        referencia o lançamento original (Requisito 9.3).
        """
        repo = InMemoryLedgerRepository()
        engine, _ = _build_engine(repo)
        command_handler = CommandHandler(engine=engine)

        # Cria lançamento original
        original_command = _make_balanced_command()
        original_entry = command_handler.handle_create_journal_entry(original_command)

        # Cria reversão
        reversal_command = CreateReversalCommand(
            original_entry_id=original_entry.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={"reason": "test"},
        )
        reversal_entry = command_handler.handle_create_reversal(reversal_command)

        # Converte para DTO e verifica metadata
        reversal_dto = journal_entry_to_response_dto(reversal_entry)
        assert reversal_dto.entry_type == "REVERSAL"
        assert reversal_dto.metadata.get("original_entry_id") == original_entry.entry_id
