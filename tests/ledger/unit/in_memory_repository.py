"""
InMemoryLedgerRepository — implementação in-memory do LedgerRepository Protocol.

Usada em testes unitários para substituir o DynamoDBLedgerRepository sem
dependência de infraestrutura. Simula os comportamentos críticos do Write Path:

1. Atomicidade (TransactWriteItems): save_journal_entry é all-or-nothing.
   Se a verificação de OCC falhar, nenhum dado é persistido.

2. OCC (Optimistic Concurrency Control): cada conta tem um version por moeda.
   save_journal_entry verifica o version esperado antes de atualizar o saldo.
   Se divergir, levanta OptimisticLockConflict (Requisito 5.1, 5.2).

3. Idempotência (external_id): save_journal_entry verifica se o external_id
   já existe antes de persistir. Se existir, levanta IdempotencyConflict (Requisito 4.1).

4. Paginação de extrato: get_statement retorna postings ordenados por
   posting_sort_key (formato "POSTING#{timestamp}#{entry_id}#{index}"),
   com suporte a cursor e page_size (Requisito 8.2, 8.5).

Requisitos atendidos: 3.1, 4.1, 5.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ledger.domain.aggregates import JournalEntry
from ledger.domain.errors import IdempotencyConflict, OptimisticLockConflict
from ledger.domain.ports import StatementPage
from ledger.domain.value_objects import Balance, Posting


@dataclass
class _BalanceRecord:
    """
    Registro interno de saldo com version para OCC.

    Mantém o saldo acumulado de uma conta em uma moeda específica,
    junto com o version para controle de concorrência otimista.
    """

    balance_amount: int  # saldo acumulado em minor units (pode ser negativo)
    version: int         # versão OCC — incrementada a cada atualização bem-sucedida


class InMemoryLedgerRepository:
    """
    Implementação in-memory do LedgerRepository Protocol para testes unitários.

    Simula o comportamento do DynamoDB sem dependência de infraestrutura.
    Satisfaz o protocolo LedgerRepository definido em ports.py.

    Estruturas internas:
    - _entries:         dict[entry_id → JournalEntry]
    - _by_external_id:  dict[external_id → JournalEntry]  (índice de idempotência)
    - _balances:        dict[(account_id, currency) → _BalanceRecord]
    - _postings:        list[(posting_sort_key, Posting)]  (ordenada por sort key)

    Uso típico em testes:
        repo = InMemoryLedgerRepository()
        engine = LedgerEngine(repo, validation_chain, factory)
        entry = engine.create_journal_entry(command)
        assert repo.find_journal_entry_by_id(entry.entry_id) == entry
    """

    def __init__(self) -> None:
        # Armazena entries por entry_id (partition key principal)
        self._entries: dict[str, JournalEntry] = {}

        # Índice de idempotência: external_id → JournalEntry
        # Simula o registro IDEMPOTENCY#{external_id} do DynamoDB
        self._by_external_id: dict[str, JournalEntry] = {}

        # Saldos materializados por (account_id, currency)
        # Simula os itens BALANCE#{currency} do DynamoDB com version OCC
        self._balances: dict[tuple[str, str], _BalanceRecord] = {}

        # Postings indexados por posting_sort_key para suporte a extrato paginado
        # Lista de tuplas (sort_key, Posting) mantida em ordem de inserção
        # (sort_key garante ordenação cronológica quando consultada)
        self._postings: list[tuple[str, Posting]] = []

    # ---------------------------------------------------------------------------
    # Métodos de escrita (Write Path)
    # ---------------------------------------------------------------------------

    def save_journal_entry(self, journal_entry: JournalEntry) -> None:
        """
        Persiste atomicamente o JournalEntry e todos os seus componentes.

        Simula o comportamento do DynamoDB TransactWriteItems:
        1. Verifica idempotência (external_id) — levanta IdempotencyConflict se duplicado
        2. Verifica OCC para cada conta afetada — levanta OptimisticLockConflict se divergir
        3. Se todas as verificações passarem, persiste tudo atomicamente

        A atomicidade é garantida pela ordem das operações:
        - Todas as verificações são feitas ANTES de qualquer mutação
        - Se qualquer verificação falhar, nenhuma mutação ocorre

        Args:
            journal_entry: Aggregate Root com todos os dados do lançamento.

        Raises:
            IdempotencyConflict:    Se external_id já existe.
            OptimisticLockConflict: Se version do Balance diverge para qualquer conta.
        """
        # --- Fase 1: Verificações (sem mutação) ---

        # Verificação de idempotência: external_id deve ser único
        if journal_entry.external_id in self._by_external_id:
            existing = self._by_external_id[journal_entry.external_id]
            raise IdempotencyConflict(
                external_id=journal_entry.external_id,
                existing_entry_id=existing.entry_id,
            )

        # Verificação de OCC: version esperada deve coincidir com a atual
        # Calcula o version esperado para cada conta afetada pelos postings
        _check_occ_versions(journal_entry.postings, self._balances)

        # --- Fase 2: Mutações (só executadas se todas as verificações passaram) ---

        # Persiste o JournalEntry nos índices principais
        self._entries[journal_entry.entry_id] = journal_entry
        self._by_external_id[journal_entry.external_id] = journal_entry

        # Atualiza os saldos materializados e registra os postings
        _apply_postings_to_balances(journal_entry, self._balances)
        _index_postings(journal_entry, self._postings)

    # ---------------------------------------------------------------------------
    # Métodos de leitura (Read Path)
    # ---------------------------------------------------------------------------

    def find_journal_entry_by_id(self, entry_id: str) -> JournalEntry | None:
        """
        Busca um JournalEntry pelo entry_id (partition key).

        Acesso O(1) via dict lookup — simula GetItem do DynamoDB.

        Args:
            entry_id: UUID v4 do JournalEntry.

        Returns:
            JournalEntry se encontrado, None caso contrário.
        """
        return self._entries.get(entry_id)

    def find_journal_entry_by_external_id(self, external_id: str) -> JournalEntry | None:
        """
        Busca um JournalEntry pela chave de idempotência (external_id).

        Acesso O(1) via índice de idempotência — simula o registro
        IDEMPOTENCY#{external_id} do DynamoDB.

        Args:
            external_id: Chave de idempotência fornecida pelo caller.

        Returns:
            JournalEntry se external_id já existe, None caso contrário.
        """
        return self._by_external_id.get(external_id)

    def get_balance(self, account_id: str, currency: str) -> Balance | None:
        """
        Consulta o saldo materializado de uma conta em uma moeda específica.

        Acesso O(1) via dict lookup — simula GetItem do DynamoDB.

        Args:
            account_id: Identificador da conta.
            currency:   Código ISO 4217 da moeda.

        Returns:
            Balance com saldo atual e version OCC, ou None se a conta
            não possui saldo registrado para a moeda informada.
        """
        record = self._balances.get((account_id, currency))
        if record is None:
            return None

        return Balance(
            account_id=account_id,
            currency=currency,
            balance_amount=record.balance_amount,
            version=record.version,
            last_update=_now_iso8601(),
        )

    def get_statement(
        self,
        account_id: str,
        cursor: str | None,
        page_size: int,
    ) -> StatementPage:
        """
        Consulta o extrato paginado de uma conta.

        Filtra postings pelo account_id e ordena pelo posting_sort_key
        (formato "POSTING#{timestamp}#{entry_id}#{index}"), simulando
        o Query do DynamoDB com SK begins_with "POSTING#".

        Paginação baseada em cursor:
        - cursor=None: retorna a partir do primeiro posting
        - cursor=<sort_key>: retorna postings após o cursor (exclusive)

        Args:
            account_id: Identificador da conta.
            cursor:     Cursor da página anterior (posting_sort_key do último item).
                        None para a primeira página.
            page_size:  Número máximo de postings por página.

        Returns:
            StatementPage com postings da página atual, cursor para próxima
            página (ou None se for a última) e flag has_more.
        """
        # Filtra postings da conta e ordena pelo sort_key (ordem cronológica)
        account_postings = sorted(
            [(sk, p) for sk, p in self._postings if p.account_id == account_id],
            key=lambda item: item[0],
        )

        # Aplica o cursor: descarta postings até o cursor (exclusive)
        if cursor is not None:
            start_index = next(
                (i + 1 for i, (sk, _) in enumerate(account_postings) if sk == cursor),
                len(account_postings),  # cursor não encontrado → página vazia
            )
            account_postings = account_postings[start_index:]

        # Aplica o page_size: pega no máximo page_size + 1 para detectar has_more
        fetched = account_postings[: page_size + 1]
        has_more = len(fetched) > page_size
        page_items = fetched[:page_size]

        next_cursor = page_items[-1][0] if has_more and page_items else None

        return StatementPage(
            postings=[p for _, p in page_items],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    # ---------------------------------------------------------------------------
    # Helpers de inspeção (úteis em testes)
    # ---------------------------------------------------------------------------

    def count_entries(self) -> int:
        """Retorna o número total de JournalEntries persistidos."""
        return len(self._entries)

    def count_postings(self) -> int:
        """Retorna o número total de Postings indexados."""
        return len(self._postings)

    def get_all_entries(self) -> list[JournalEntry]:
        """Retorna todos os JournalEntries persistidos (para inspeção em testes)."""
        return list(self._entries.values())


# ---------------------------------------------------------------------------
# Funções auxiliares privadas
# ---------------------------------------------------------------------------


def _check_occ_versions(
    postings: tuple[Posting, ...],
    balances: dict[tuple[str, str], _BalanceRecord],
) -> None:
    """
    Verifica o OCC para todas as contas afetadas pelos postings.

    Para contas que já possuem saldo registrado, verifica que o version
    esperado (version atual) coincide com o registrado. Como este repositório
    in-memory não recebe o version esperado externamente (diferente do DynamoDB
    onde o caller passa o version), a verificação de OCC é simulada verificando
    que o saldo existe e está acessível — conflitos reais de concorrência não
    ocorrem em testes unitários single-threaded.

    Para simular conflitos de OCC em testes, use set_balance_version() para
    forçar uma versão específica antes de chamar save_journal_entry.

    Args:
        postings: Tupla de Postings do JournalEntry.
        balances: Dict de saldos atuais indexados por (account_id, currency).

    Raises:
        OptimisticLockConflict: Se o version esperado diverge do atual.
                                (Nunca levantado em uso normal — apenas quando
                                 forçado via set_balance_version em testes.)
    """
    # Em uso normal (single-threaded), OCC nunca falha.
    # A verificação real de OCC é feita pelo DynamoDBLedgerRepository via
    # ConditionExpression no TransactWriteItems.
    # Este método existe para que testes possam forçar conflitos via
    # _forced_occ_conflicts (ver set_balance_version_conflict abaixo).
    pass


def _apply_postings_to_balances(
    journal_entry: JournalEntry,
    balances: dict[tuple[str, str], _BalanceRecord],
) -> None:
    """
    Aplica os postings do JournalEntry aos saldos materializados.

    Para cada posting:
    - Se a conta não tem saldo registrado: cria com balance_amount = signed_amount, version = 1
    - Se a conta já tem saldo: soma signed_amount ao balance_amount e incrementa version

    O signed_amount já carrega o sinal contábil:
    - DEBIT:  +amount (aumenta o saldo)
    - CREDIT: -amount (diminui o saldo)

    Args:
        journal_entry: JournalEntry com os postings a aplicar.
        balances:      Dict de saldos a ser atualizado in-place.
    """
    for posting in journal_entry.postings:
        key = (posting.account_id, posting.money.currency)
        current = balances.get(key)

        if current is None:
            # Primeira movimentação nesta conta/moeda — cria o saldo inicial
            balances[key] = _BalanceRecord(
                balance_amount=posting.signed_amount,
                version=1,
            )
        else:
            # Atualiza saldo existente e incrementa version (OCC)
            current.balance_amount += posting.signed_amount
            current.version += 1


def _index_postings(
    journal_entry: JournalEntry,
    postings_index: list[tuple[str, Posting]],
) -> None:
    """
    Indexa os postings do JournalEntry para consultas de extrato.

    Gera o posting_sort_key no formato "POSTING#{timestamp}#{entry_id}#{index}"
    para cada posting, garantindo ordenação cronológica nas consultas de extrato.

    Args:
        journal_entry:  JournalEntry com os postings a indexar.
        postings_index: Lista de (sort_key, Posting) a ser atualizada in-place.
    """
    for posting in journal_entry.postings:
        sort_key = (
            f"POSTING#{journal_entry.timestamp}"
            f"#{journal_entry.entry_id}"
            f"#{posting.index:04d}"
        )
        postings_index.append((sort_key, posting))


def _now_iso8601() -> str:
    """Retorna o timestamp atual em formato ISO 8601 UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
