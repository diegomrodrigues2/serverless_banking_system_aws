"""
DynamoDBLedgerRepository — Adaptador DynamoDB do Double-Entry Ledger.

Implementa o padrão GoF Adapter, adaptando a interface LedgerRepository
(porta do domínio) para operações concretas do DynamoDB.

Operações principais:
- save_journal_entry: TransactWriteItems atômica com JournalEntry + Postings
  + Balance updates (OCC) + OutboxEvent + Idempotency record
- find_journal_entry_by_id: GetItem por entry_id (O(1))
- find_journal_entry_by_external_id: GetItem no registro de idempotência
- get_balance: GetItem por account_id + currency (O(1))
- get_statement: Query com paginação por cursor (posting_sort_key)

Requisitos atendidos: 3.1, 3.2, 3.3, 4.3, 5.1, 5.3, 8.1, 8.2, 8.5
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from ledger.domain.aggregates import JournalEntry
from ledger.domain.errors import IdempotencyConflict, JournalEntryNotFound, OptimisticLockConflict
from ledger.domain.ports import StatementPage
from ledger.domain.value_objects import Balance, Posting
from ledger.infrastructure.dynamodb_mapper import (
    PK_ACCOUNT,
    PK_IDEMPOTENCY,
    PK_JOURNAL,
    PK_OUTBOX,
    SK_BALANCE,
    SK_POSTING,
    balance_update_expression,
    build_posting_sort_key,
    dynamo_item_to_balance,
    dynamo_item_to_journal_entry,
    dynamo_item_to_outbox_event,
    dynamo_item_to_posting,
    extract_entry_id_from_idempotency_item,
    idempotency_record_to_dynamo_item,
    journal_entry_to_dynamo_item,
    outbox_event_to_dynamo_item,
    posting_to_dynamo_item,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

logger = logging.getLogger(__name__)


class DynamoDBLedgerRepository:
    """
    GoF Adapter — adapta LedgerRepository para DynamoDB.

    Toda a lógica de persistência está encapsulada aqui. O domínio
    nunca importa boto3 diretamente — depende apenas do protocolo
    LedgerRepository definido em ports.py.

    Injeção de dependência via construtor permite substituição do
    client DynamoDB por mocks em testes de integração.
    """

    def __init__(self, dynamodb_client: Any, table_name: str) -> None:
        """
        Inicializa o repositório com o client DynamoDB e o nome da tabela.

        Args:
            dynamodb_client: Client boto3 DynamoDB (ou mock compatível).
            table_name:      Nome da tabela DynamoDB single-table.
        """
        self._client = dynamodb_client
        self._table_name = table_name

    # -----------------------------------------------------------------------
    # Write Path — save_journal_entry (TransactWriteItems)
    # -----------------------------------------------------------------------

    def save_journal_entry(self, journal_entry: JournalEntry) -> None:
        """
        Persiste atomicamente um JournalEntry e todos os seus componentes.

        Composição da TransactWriteItems (Requisito 3.1):
            1x JournalEntry (Put)
            1x Idempotency record (Put com ConditionExpression attribute_not_exists)
            Nx Posting (Put) — um por posting
            Mx Balance update (Update com ConditionExpression version = :expected_version)
            1x OutboxEvent (Put)
            ─────────────────
            Total: 3 + N + M itens (deve ser ≤ 100 — validado pelo TransactionLimitValidator)

        Args:
            journal_entry: Aggregate Root com todos os dados do lançamento.

        Raises:
            IdempotencyConflict:    Se external_id já existe (ConditionExpression falhou
                                    no registro de idempotência).
            OptimisticLockConflict: Se version do Balance diverge (ConditionExpression
                                    falhou na atualização de saldo).
        """
        transact_items = self._build_transact_items(journal_entry)

        try:
            self._client.transact_write_items(TransactItems=transact_items)
            logger.info(
                "journal_entry salvo com sucesso",
                extra={
                    "entry_id": journal_entry.entry_id,
                    "external_id": journal_entry.external_id,
                    "operation": "save_journal_entry",
                    "result": "success",
                    "posting_count": len(journal_entry.postings),
                },
            )
        except ClientError as exc:
            self._handle_transact_write_error(exc, journal_entry)

    def _build_transact_items(self, entry: JournalEntry) -> list[dict[str, Any]]:
        """
        Constrói a lista de itens para a TransactWriteItems.

        Ordem dos itens:
        1. JournalEntry (Put)
        2. Idempotency record (Put condicional — attribute_not_exists)
        3. Postings (Put — um por posting)
        4. Balance updates (Update condicional — OCC por version)
        5. OutboxEvent (Put)

        Para cada conta afetada, lê o Balance atual antes de montar a transação
        a fim de obter o expected_version correto para a ConditionExpression OCC.
        Contas sem Balance prévio usam expected_version=0 (attribute_not_exists
        cobre esse caso na ConditionExpression do balance_update_expression).

        Args:
            entry: JournalEntry com todos os dados do lançamento.

        Returns:
            Lista de dicts no formato esperado pelo TransactWriteItems da AWS.
        """
        items: list[dict[str, Any]] = []

        # 1. JournalEntry
        items.append({
            "Put": {
                "TableName": self._table_name,
                "Item": journal_entry_to_dynamo_item(entry),
            }
        })

        # 2. Idempotency record — ConditionExpression garante unicidade do external_id
        items.append({
            "Put": {
                "TableName": self._table_name,
                "Item": idempotency_record_to_dynamo_item(entry.external_id, entry.entry_id),
                # Rejeita se já existe um registro com o mesmo external_id (Requisito 4.3)
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        })

        # 3. Postings — um item por posting
        for posting in entry.postings:
            items.append({
                "Put": {
                    "TableName": self._table_name,
                    "Item": posting_to_dynamo_item(posting, entry.entry_id, entry.timestamp),
                }
            })

        # 4. Balance updates — agrega signed_amounts por conta+moeda e aplica OCC.
        # Lê o version atual de cada Balance antes de montar a transação para garantir
        # que a ConditionExpression use o expected_version correto (Requisito 5.1).
        balance_deltas = _compute_balance_deltas(entry)
        current_versions = self._fetch_current_balance_versions(balance_deltas.keys())

        for (account_id, currency), (signed_amount, _) in balance_deltas.items():
            expected_version = current_versions.get((account_id, currency), 0)
            update_params = balance_update_expression(
                account_id=account_id,
                currency=currency,
                signed_amount=signed_amount,
                expected_version=expected_version,
            )
            items.append({
                "Update": {
                    "TableName": self._table_name,
                    **update_params,
                }
            })

        # 5. OutboxEvent (Transactional Outbox Pattern — Requisito 7.1)
        items.append({
            "Put": {
                "TableName": self._table_name,
                "Item": outbox_event_to_dynamo_item(entry.outbox_event),
            }
        })

        return items

    def _fetch_current_balance_versions(
        self,
        account_currency_pairs: Any,
    ) -> dict[tuple[str, str], int]:
        """
        Lê o version atual de cada Balance afetado pelo lançamento.

        Usa BatchGetItem para buscar todos os Balances em uma única chamada,
        minimizando a latência antes de montar a TransactWriteItems.

        Contas sem Balance prévio não aparecem na resposta — o caller usa
        version=0 como padrão, que é coberto pela ConditionExpression
        `attribute_not_exists(PK) OR version = :expected_version`.

        Args:
            account_currency_pairs: Iterable de (account_id, currency).

        Returns:
            Dict de (account_id, currency) → version atual.
            Pares sem Balance existente são omitidos (caller usa 0 como padrão).
        """
        pairs = list(account_currency_pairs)
        if not pairs:
            return {}

        # Monta as chaves para BatchGetItem
        keys = [
            {
                "PK": {"S": f"{PK_ACCOUNT}#{account_id}"},
                "SK": {"S": f"{SK_BALANCE}#{currency}"},
            }
            for account_id, currency in pairs
        ]

        response = self._client.batch_get_item(
            RequestItems={
                self._table_name: {
                    "Keys": keys,
                    # Leitura consistente para garantir version atualizado
                    "ConsistentRead": True,
                }
            }
        )

        versions: dict[tuple[str, str], int] = {}
        for item in response.get("Responses", {}).get(self._table_name, []):
            account_id = item["account_id"]["S"]
            currency = item["currency"]["S"]
            version = int(item["version"]["N"])
            versions[(account_id, currency)] = version

        return versions

    def _handle_transact_write_error(
        self, exc: ClientError, entry: JournalEntry
    ) -> None:
        """
        Interpreta erros do TransactWriteItems e levanta DomainErrors apropriados.

        O DynamoDB retorna TransactionCanceledException com uma lista de
        CancellationReasons quando uma ou mais ConditionExpressions falham.
        Cada razão corresponde a um item na lista de TransactItems (mesma ordem).

        Índices dos itens na TransactWriteItems:
            0: JournalEntry
            1: Idempotency record (ConditionExpression attribute_not_exists)
            2..N+1: Postings
            N+2..N+M+1: Balance updates (ConditionExpression version = :expected_version)
            N+M+2: OutboxEvent

        Args:
            exc:   ClientError do boto3 com detalhes do erro.
            entry: JournalEntry que falhou ao ser persistido.

        Raises:
            IdempotencyConflict:    Se o registro de idempotência já existe.
            OptimisticLockConflict: Se a versão do Balance diverge.
            ClientError:            Para outros erros não tratados.
        """
        error_code = exc.response.get("Error", {}).get("Code", "")

        if error_code != "TransactionCanceledException":
            # Erro de infraestrutura não relacionado a condições de negócio
            logger.error(
                "erro inesperado no TransactWriteItems",
                extra={
                    "entry_id": entry.entry_id,
                    "operation": "save_journal_entry",
                    "result": "error",
                    "error_code": error_code,
                },
            )
            raise exc

        # Analisa as razões de cancelamento para identificar qual condição falhou
        reasons = exc.response.get("CancellationReasons", [])
        n_postings = len(entry.postings)

        # Índice 1 = Idempotency record (attribute_not_exists)
        idempotency_reason = reasons[1] if len(reasons) > 1 else {}
        if idempotency_reason.get("Code") == "ConditionalCheckFailed":
            logger.warning(
                "idempotência detectada — external_id já existe",
                extra={
                    "entry_id": entry.entry_id,
                    "external_id": entry.external_id,
                    "operation": "save_journal_entry",
                    "result": "idempotency_conflict",
                },
            )
            raise IdempotencyConflict(
                external_id=entry.external_id,
                existing_entry_id=entry.entry_id,
            )

        # Índices 2..N+1 = Postings (sem condição — não devem falhar)
        # Índices N+2..N+M+1 = Balance updates (version = :expected_version)
        balance_start_index = 2 + n_postings
        for i, reason in enumerate(reasons[balance_start_index:], start=balance_start_index):
            if reason.get("Code") == "ConditionalCheckFailed":
                # Identifica qual conta teve conflito de versão
                # O índice relativo ao início dos balance updates indica a conta
                balance_index = i - balance_start_index
                balance_deltas = _compute_balance_deltas(entry)
                account_keys = list(balance_deltas.keys())
                if balance_index < len(account_keys):
                    account_id, _ = account_keys[balance_index]
                    expected_version = balance_deltas[account_keys[balance_index]][1]
                else:
                    account_id = "unknown"
                    expected_version = -1

                logger.warning(
                    "conflito OCC no Balance",
                    extra={
                        "entry_id": entry.entry_id,
                        "account_id": account_id,
                        "operation": "save_journal_entry",
                        "result": "optimistic_lock_conflict",
                    },
                )
                raise OptimisticLockConflict(
                    account_id=account_id,
                    expected_version=expected_version,
                )

        # Nenhuma razão específica identificada — re-levanta o erro original
        logger.error(
            "TransactWriteItems cancelada por razão desconhecida",
            extra={
                "entry_id": entry.entry_id,
                "operation": "save_journal_entry",
                "result": "error",
                "cancellation_reasons": reasons,
            },
        )
        raise exc

    # -----------------------------------------------------------------------
    # Read Path — find_journal_entry_by_id
    # -----------------------------------------------------------------------

    def find_journal_entry_by_id(self, entry_id: str) -> JournalEntry | None:
        """
        Busca um JournalEntry pelo entry_id (partition key).

        Acesso O(1) via GetItem. Reconstrói o JournalEntry buscando também
        os Postings via Query no GSI-EntryPostings e o OutboxEvent via GetItem.

        Args:
            entry_id: UUID v4 do JournalEntry.

        Returns:
            JournalEntry reconstruído, ou None se não encontrado.
        """
        # Busca o item principal do JournalEntry
        response = self._client.get_item(
            TableName=self._table_name,
            Key={
                "PK": {"S": f"{PK_JOURNAL}#{entry_id}"},
                "SK": {"S": f"{PK_JOURNAL}#{entry_id}"},
            },
        )
        item = response.get("Item")
        if not item:
            return None

        # Busca os postings via Query (PK = JOURNAL#{entry_id}, SK begins_with POSTING#)
        # Nota: os postings são armazenados com PK = ACCOUNT#{account_id}, mas o GSI
        # permite busca por entry_id. Aqui usamos Query direto na tabela principal
        # filtrando por entry_id via FilterExpression (scan dos postings do entry).
        # Para produção, o GSI-EntryPostings seria mais eficiente.
        postings = self._fetch_postings_for_entry(entry_id)

        # Busca o OutboxEvent
        outbox_response = self._client.get_item(
            TableName=self._table_name,
            Key={
                "PK": {"S": f"{PK_OUTBOX}#{entry_id}"},
                "SK": {"S": f"{PK_OUTBOX}#{entry_id}"},
            },
        )
        outbox_item = outbox_response.get("Item")
        if not outbox_item:
            # OutboxEvent pode ter expirado via TTL — cria um placeholder
            from ledger.domain.value_objects import OutboxEvent
            outbox_event = OutboxEvent(
                event_id=f"OUTBOX#{entry_id}",
                entry_id=entry_id,
                event_type="TransactionCreated",
                payload={},
                expires_at=0,
            )
        else:
            outbox_event = dynamo_item_to_outbox_event(outbox_item)

        return dynamo_item_to_journal_entry(item, postings, outbox_event)

    def _fetch_postings_for_entry(self, entry_id: str) -> list[Posting]:
        """
        Busca todos os Postings de um JournalEntry via Query com FilterExpression.

        Estratégia: Query na tabela principal com PK = JOURNAL#{entry_id} não funciona
        porque postings têm PK = ACCOUNT#{account_id}. Usamos Query com FilterExpression
        no entry_id. Para produção, o GSI-EntryPostings seria mais eficiente.

        Args:
            entry_id: UUID do JournalEntry pai.

        Returns:
            Lista de Postings ordenados por posting_index.
        """
        # Query usando FilterExpression para encontrar postings pelo entry_id
        # Em produção, usar GSI-EntryPostings para eficiência
        response = self._client.query(
            TableName=self._table_name,
            IndexName="GSI-EntryPostings",
            KeyConditionExpression="entry_id_gsi = :entry_id",
            ExpressionAttributeValues={
                ":entry_id": {"S": f"{PK_JOURNAL}#{entry_id}"},
            },
        )

        postings = [
            dynamo_item_to_posting(item)
            for item in response.get("Items", [])
            if item.get("SK", {}).get("S", "").startswith(SK_POSTING)
        ]

        # Ordena por posting_index para garantir ordem consistente
        postings.sort(key=lambda p: p.index)
        return postings

    # -----------------------------------------------------------------------
    # Read Path — find_journal_entry_by_external_id
    # -----------------------------------------------------------------------

    def find_journal_entry_by_external_id(self, external_id: str) -> JournalEntry | None:
        """
        Busca um JournalEntry pela chave de idempotência (external_id).

        Consulta o registro de idempotência para obter o entry_id,
        depois delega para find_journal_entry_by_id.

        Args:
            external_id: Chave de idempotência fornecida pelo caller.

        Returns:
            JournalEntry se external_id já existe, None caso contrário.
        """
        response = self._client.get_item(
            TableName=self._table_name,
            Key={
                "PK": {"S": f"{PK_IDEMPOTENCY}#{external_id}"},
                "SK": {"S": f"{PK_IDEMPOTENCY}#{external_id}"},
            },
        )
        item = response.get("Item")
        if not item:
            return None

        entry_id = extract_entry_id_from_idempotency_item(item)
        return self.find_journal_entry_by_id(entry_id)

    # -----------------------------------------------------------------------
    # Read Path — get_balance (O(1))
    # -----------------------------------------------------------------------

    def get_balance(self, account_id: str, currency: str) -> Balance | None:
        """
        Consulta o saldo materializado de uma conta em uma moeda específica.

        Acesso O(1) via GetItem (Requisito 8.1).

        Args:
            account_id: Identificador da conta.
            currency:   Código ISO 4217 da moeda.

        Returns:
            Balance com saldo atual e version OCC, ou None se não existe.
        """
        response = self._client.get_item(
            TableName=self._table_name,
            Key={
                "PK": {"S": f"{PK_ACCOUNT}#{account_id}"},
                "SK": {"S": f"{SK_BALANCE}#{currency}"},
            },
        )
        item = response.get("Item")
        if not item:
            return None

        return dynamo_item_to_balance(item)

    # -----------------------------------------------------------------------
    # Read Path — get_statement (paginação por cursor)
    # -----------------------------------------------------------------------

    def get_statement(
        self,
        account_id: str,
        cursor: str | None,
        page_size: int,
    ) -> StatementPage:
        """
        Consulta o extrato paginado de uma conta.

        Usa Query com SK begins_with POSTING# para retornar postings em
        ordem cronológica (posting_sort_key garante ordenação lexicográfica).
        Paginação baseada em cursor (ExclusiveStartKey) — eficiente no DynamoDB.

        Args:
            account_id: Identificador da conta.
            cursor:     Cursor da página anterior (posting_sort_key do último item).
                        None para a primeira página.
            page_size:  Número máximo de postings por página.

        Returns:
            StatementPage com postings, next_cursor e has_more.
        """
        query_params: dict[str, Any] = {
            "TableName": self._table_name,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk_prefix)",
            "ExpressionAttributeValues": {
                ":pk": {"S": f"{PK_ACCOUNT}#{account_id}"},
                ":sk_prefix": {"S": f"{SK_POSTING}#"},
            },
            # Busca page_size + 1 para detectar se há mais páginas
            "Limit": page_size + 1,
            # Ordenação cronológica ascendente (padrão do DynamoDB)
            "ScanIndexForward": True,
        }

        # Paginação baseada em cursor — ExclusiveStartKey é o último item da página anterior
        if cursor:
            query_params["ExclusiveStartKey"] = {
                "PK": {"S": f"{PK_ACCOUNT}#{account_id}"},
                "SK": {"S": cursor},
            }

        response = self._client.query(**query_params)
        items = response.get("Items", [])

        # Detecta se há mais páginas além da atual
        has_more = len(items) > page_size
        # Retorna apenas page_size itens (o extra foi usado apenas para detectar has_more)
        page_items = items[:page_size]

        postings = [dynamo_item_to_posting(item) for item in page_items]

        # O cursor para a próxima página é o SK do último item retornado
        next_cursor: str | None = None
        if has_more and page_items:
            next_cursor = page_items[-1]["SK"]["S"]

        return StatementPage(
            postings=postings,
            next_cursor=next_cursor,
            has_more=has_more,
        )


# ---------------------------------------------------------------------------
# Funções auxiliares privadas
# ---------------------------------------------------------------------------


def _compute_balance_deltas(
    entry: JournalEntry,
) -> dict[tuple[str, str], tuple[int, int]]:
    """
    Agrega os signed_amounts dos postings por (account_id, currency).

    Retorna um dict mapeando (account_id, currency) para (signed_amount_total, expected_version).
    O expected_version é 0 para novos saldos (sem versão prévia conhecida).

    Em produção, o expected_version deve ser obtido do Balance atual via get_balance()
    antes de iniciar a transação. Aqui usamos 0 como padrão — a ConditionExpression
    `attribute_not_exists(PK) OR version = :expected_version` cobre o caso de novo saldo.

    Args:
        entry: JournalEntry com todos os postings.

    Returns:
        Dict de (account_id, currency) → (signed_amount_total, expected_version).
    """
    # Acumula signed_amounts por (account_id, currency)
    deltas: dict[tuple[str, str], int] = defaultdict(int)
    for posting in entry.postings:
        key = (posting.account_id, posting.money.currency)
        deltas[key] += posting.signed_amount

    # Retorna com expected_version=0 (novo saldo ou versão desconhecida)
    # Em produção, o LedgerEngine deve buscar o Balance atual e passar a versão correta
    return {key: (delta, 0) for key, delta in deltas.items()}
