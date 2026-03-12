"""
Handlers da camada de aplicação do Double-Entry Ledger.

Implementa o padrão CQRS separando o caminho de escrita (CommandHandler)
do caminho de leitura (QueryHandler):

- CommandHandler: delega operações de escrita para o LedgerEngine (domínio).
  Recebe CreateJournalEntryCommand e CreateReversalCommand.

- QueryHandler: delega operações de leitura diretamente para o LedgerRepository
  (Read Path), sem passar pelo LedgerEngine. Recebe GetBalanceQuery e
  GetStatementQuery.

Esta separação garante que o Read Path não passe pelo Write Path, permitindo
otimizações independentes e consistência eventual no lado de leitura.

Requisitos atendidos: 3.1, 8.1, 8.2
"""
from __future__ import annotations

from ledger.application.commands import CreateJournalEntryCommand, CreateReversalCommand
from ledger.application.queries import GetBalanceQuery, GetStatementQuery
from ledger.domain.aggregates import JournalEntry
from ledger.domain.ports import LedgerRepository, StatementPage
from ledger.domain.services import LedgerEngine
from ledger.domain.value_objects import Balance


class CommandHandler:
    """
    Handler de comandos de escrita — delega para o LedgerEngine.

    Atua como ponto de entrada da camada de aplicação para operações de escrita.
    Não contém lógica de negócio própria — toda a orquestração de validação,
    criação e persistência é responsabilidade do LedgerEngine (GoF Facade).

    Injeção de dependência via construtor permite substituição do engine
    por implementações in-memory em testes unitários.
    """

    def __init__(self, engine: LedgerEngine) -> None:
        """
        Inicializa o CommandHandler com o LedgerEngine.

        Args:
            engine: Instância do LedgerEngine que orquestra o Write Path.
        """
        self._engine = engine

    def handle_create_journal_entry(
        self, command: CreateJournalEntryCommand
    ) -> JournalEntry:
        """
        Processa o comando de criação de lançamento contábil padrão.

        Delega integralmente para LedgerEngine.create_journal_entry, que executa:
        1. Verificação de idempotência via external_id
        2. Validação via ValidationChain (zero-sum, minor units, limites)
        3. Criação do aggregate via JournalEntryFactory
        4. Persistência atômica via LedgerRepository (TransactWriteItems)

        Args:
            command: Comando com external_id, postings e metadata.

        Returns:
            JournalEntry criado e persistido com sucesso.

        Raises:
            IdempotencyConflict:      Se external_id já existe (retorna entry original).
            ZeroSumViolation:         Se postings não somam zero por moeda.
            InvalidAmountType:        Se qualquer amount não é int > 0.
            TransactionLimitExceeded: Se transação excede 100 itens.
            TransactionSizeExceeded:  Se transação excede 4MB.
            OptimisticLockConflict:   Se versão do Balance diverge.
        """
        return self._engine.create_journal_entry(command)

    def handle_create_reversal(
        self, command: CreateReversalCommand
    ) -> JournalEntry:
        """
        Processa o comando de criação de reversão de lançamento.

        Delega integralmente para LedgerEngine.create_reversal, que executa:
        1. Busca do lançamento original por original_entry_id
        2. Criação do JournalEntry de reversão com postings invertidos
        3. Persistência atômica via LedgerRepository

        Args:
            command: Comando com original_entry_id, external_id e metadata.

        Returns:
            Novo JournalEntry do tipo REVERSAL com postings invertidos.

        Raises:
            JournalEntryNotFound:   Se original_entry_id não existe.
            OptimisticLockConflict: Se versão do Balance diverge.
            IdempotencyConflict:    Se external_id da reversão já existe.
        """
        return self._engine.create_reversal(command)


class QueryHandler:
    """
    Handler de queries de leitura — delega diretamente para o LedgerRepository.

    Atua como ponto de entrada da camada de aplicação para operações de leitura.
    Acessa o repositório diretamente (sem passar pelo LedgerEngine) para garantir
    que o Read Path seja independente do Write Path (CQRS).

    O Read Path opera com consistência eventual — saldos e extratos podem estar
    defasados em até ~1 segundo em relação ao último lançamento (Requisito 8.3).
    """

    def __init__(self, repository: LedgerRepository) -> None:
        """
        Inicializa o QueryHandler com o LedgerRepository.

        Args:
            repository: Implementação do LedgerRepository (DynamoDB ou in-memory).
        """
        self._repository = repository

    def handle_get_balance(self, query: GetBalanceQuery) -> Balance | None:
        """
        Processa a query de consulta de saldo materializado.

        Retorna a projeção materializada do saldo da conta na moeda especificada.
        Acesso O(1) via GetItem no DynamoDB (Requisito 8.1).

        Args:
            query: Query com account_id e currency.

        Returns:
            Balance com saldo atual e version OCC, ou None se a conta
            não possui saldo registrado para a moeda informada.
        """
        return self._repository.get_balance(
            account_id=query.account_id,
            currency=query.currency,
        )

    def handle_get_statement(self, query: GetStatementQuery) -> StatementPage:
        """
        Processa a query de consulta de extrato paginado.

        Retorna os Postings da conta ordenados cronologicamente, com paginação
        baseada em cursor (posting_sort_key). Cada página contém no máximo
        query.page_size postings (Requisito 8.2, 8.5).

        Args:
            query: Query com account_id, cursor opcional e page_size.

        Returns:
            StatementPage com postings da página atual, cursor para próxima
            página (ou None se for a última) e flag has_more.
        """
        return self._repository.get_statement(
            account_id=query.account_id,
            cursor=query.cursor,
            page_size=query.page_size,
        )
