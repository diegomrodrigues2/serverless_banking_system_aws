"""
Portas (interfaces) do domínio do Double-Entry Ledger.

Segue o padrão Ports & Adapters (Hexagonal Architecture).
O domínio depende apenas destas abstrações — nunca de DynamoDB diretamente.

Componentes:
- StatementPage: dataclass de paginação para consultas de extrato
- LedgerRepository: Protocol (interface) do repositório do ledger

Implementações concretas (adaptadores) que satisfazem este protocolo:
- DynamoDBLedgerRepository (infrastructure/dynamodb_repository.py)
- InMemoryLedgerRepository (tests/unit — para testes unitários)

Requisitos atendidos: 3.1, 4.1, 8.1, 8.2
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ledger.domain.aggregates import JournalEntry
from ledger.domain.value_objects import Balance, Posting


@dataclass
class StatementPage:
    """
    Resultado paginado de uma consulta de extrato (statement).

    Implementa paginação baseada em cursor para consultas de extrato,
    conforme o padrão CQRS do Read Path. O cursor é baseado no
    posting_sort_key do DynamoDB, permitindo paginação eficiente
    sem necessidade de offset (que seria ineficiente em DynamoDB).

    Campos:
        postings:    Lista de Postings da página atual, ordenados
                     cronologicamente pelo posting_sort_key.
        next_cursor: Cursor para a próxima página. None indica que
                     não há mais páginas disponíveis (última página).
        has_more:    Indica se existem mais postings além desta página.
                     True quando next_cursor não é None.

    Uso típico:
        page = repository.get_statement(account_id="acc_001", cursor=None, page_size=20)
        while page.has_more:
            process(page.postings)
            page = repository.get_statement(account_id="acc_001", cursor=page.next_cursor, page_size=20)
        process(page.postings)  # última página
    """

    postings: list[Posting] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class LedgerRepository(Protocol):
    """
    Porto (interface) do repositório do ledger.

    Define o contrato que todas as implementações de persistência devem
    satisfazer. O domínio depende apenas desta abstração — nunca de
    DynamoDB, SQL ou qualquer tecnologia de persistência específica.

    Seguindo o padrão Ports & Adapters (Hexagonal Architecture):
    - Este Protocol é o "porto" (interface do domínio)
    - DynamoDBLedgerRepository é o "adaptador" (implementação concreta)
    - InMemoryLedgerRepository é o adaptador de teste

    Invariantes do contrato:
    - save_journal_entry é atômico: ou persiste tudo ou nada
    - find_* retorna None quando o registro não existe (sem exceção)
    - get_balance retorna None quando a conta não tem saldo registrado
    - get_statement retorna StatementPage vazia quando não há postings

    Exceções que implementações devem levantar:
    - IdempotencyConflict: quando external_id já existe (save_journal_entry)
    - OptimisticLockConflict: quando version do Balance diverge (save_journal_entry)
    """

    def save_journal_entry(self, journal_entry: JournalEntry) -> None:
        """
        Persiste atomicamente um JournalEntry e todos os seus componentes.

        Operação atômica que grava em uma única transação:
        - O JournalEntry em si (registro contábil)
        - Todos os Postings do lançamento
        - Atualizações de Balance para cada conta afetada (com OCC)
        - O OutboxEvent associado (Transactional Outbox Pattern)
        - Registro de idempotência (external_id → entry_id)

        Implementações devem usar TransactWriteItems no DynamoDB para
        garantir atomicidade (Requisito 3.1).

        Args:
            journal_entry: Aggregate Root com todos os dados do lançamento.

        Raises:
            IdempotencyConflict: Se external_id já existe no sistema.
                                 Retorna o entry_id original sem criar novo lançamento.
            OptimisticLockConflict: Se o version do Balance diverge (escrita concorrente).
                                    O caller deve retentar com o version atualizado.
        """
        ...

    def find_journal_entry_by_id(self, entry_id: str) -> JournalEntry | None:
        """
        Busca um JournalEntry pelo seu identificador único (entry_id).

        Acesso direto via partition key do DynamoDB — O(1).
        Usado principalmente pelo Read Path e pelo fluxo de reversão
        (que precisa do lançamento original para gerar os postings inversos).

        Args:
            entry_id: UUID v4 do JournalEntry.

        Returns:
            JournalEntry se encontrado, None caso contrário.
        """
        ...

    def find_journal_entry_by_external_id(self, external_id: str) -> JournalEntry | None:
        """
        Busca um JournalEntry pela chave de idempotência (external_id).

        Usado pelo LedgerEngine para verificar idempotência antes de criar
        um novo lançamento. Se o external_id já existe, retorna o lançamento
        original sem criar duplicata (Requisito 4.1).

        Implementações devem usar o registro de idempotência (tabela ou GSI)
        para esta consulta, não um scan completo.

        Args:
            external_id: Chave de idempotência fornecida pelo caller.

        Returns:
            JournalEntry se external_id já existe, None caso contrário.
        """
        ...

    def get_balance(self, account_id: str, currency: str) -> Balance | None:
        """
        Consulta o saldo materializado de uma conta em uma moeda específica.

        Acesso O(1) via GetItem no DynamoDB (PK: ACCOUNT#{account_id},
        SK: BALANCE#{currency}). Retorna a projeção materializada atualizada
        pelo Write Path a cada lançamento.

        Consistência eventual: o saldo pode estar defasado em até ~1 segundo
        em relação ao último lançamento (Requisito 8.3).

        Args:
            account_id: Identificador da conta.
            currency:   Código ISO 4217 da moeda (ex: "BRL", "USD").

        Returns:
            Balance com saldo atual e version OCC, ou None se a conta
            não possui saldo registrado para a moeda informada.
        """
        ...

    def get_statement(
        self,
        account_id: str,
        cursor: str | None,
        page_size: int,
    ) -> StatementPage:
        """
        Consulta o extrato paginado de uma conta.

        Retorna os Postings da conta ordenados cronologicamente pelo
        posting_sort_key (formato: "POSTING#{timestamp}#{entry_id}#{index}").
        A paginação é baseada em cursor (não offset), o que é eficiente
        no DynamoDB e não sofre com o problema de "drift" de páginas.

        Implementações devem usar Query no DynamoDB com:
        - PK: ACCOUNT#{account_id}
        - SK begins_with: POSTING#
        - ExclusiveStartKey: cursor (se fornecido)
        - Limit: page_size

        Args:
            account_id: Identificador da conta.
            cursor:     Cursor da página anterior (posting_sort_key do último item).
                        None para a primeira página.
            page_size:  Número máximo de postings por página.

        Returns:
            StatementPage com postings da página atual, cursor para próxima
            página (ou None se for a última) e flag has_more.
        """
        ...
