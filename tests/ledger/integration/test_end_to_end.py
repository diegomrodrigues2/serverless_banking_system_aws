"""
Testes end-to-end do Double-Entry Ledger com DynamoDB Local.

Cobre o fluxo completo da arquitetura sem deixar nenhuma parte sem cobertura:
1. API handlers (write + read) → DynamoDB real
2. Publisher Lambda processando eventos do DynamoDB Stream
3. Audit Pipeline (AuditTransformer) processando stream events → Firehose mock
4. Ciclo de vida completo: create → balance → statement → reverse → verify
5. Erros propagados corretamente de ponta a ponta

Pré-requisito: DynamoDB Local rodando em localhost:8000.
  docker-compose up -d
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from ledger.api.write_handler import handle_create_entry, handle_create_reversal
from ledger.api.read_handler import handle_get_balance, handle_get_statement
from ledger.application.commands import CreateJournalEntryCommand, PostingInput
from ledger.application.handlers import CommandHandler, QueryHandler
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.services import LedgerEngine
from ledger.domain.validators import (
    MinorUnitsValidator,
    TransactionLimitValidator,
    ValidationChain,
    ZeroSumValidator,
)
from ledger.infrastructure.dynamodb_repository import DynamoDBLedgerRepository
from ledger.infrastructure.publisher import OutboxEventPublisher
from ledger.infrastructure.audit_exporter import AuditTransformer, AuditRecord
from ledger.infrastructure.dynamodb_mapper import (
    PK_JOURNAL,
    PK_OUTBOX,
    PK_ACCOUNT,
    SK_POSTING,
    journal_entry_to_dynamo_item,
    posting_to_dynamo_item,
    outbox_event_to_dynamo_item,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def command_handler(ledger_engine):
    """CommandHandler conectado ao LedgerEngine com DynamoDB real."""
    return CommandHandler(engine=ledger_engine)


@pytest.fixture
def query_handler(repository):
    """QueryHandler conectado ao DynamoDBLedgerRepository real."""
    return QueryHandler(repository=repository)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_event(body: dict, route_key: str = "") -> dict:
    """Constrói evento Lambda simulando API Gateway."""
    return {
        "body": json.dumps(body),
        "routeKey": route_key,
        "requestContext": {"requestId": str(uuid.uuid4())},
    }


def _balance_event(account_id: str, currency: str) -> dict:
    return {
        "pathParameters": {"account_id": account_id},
        "queryStringParameters": {"currency": currency},
        "requestContext": {"requestId": str(uuid.uuid4())},
    }


def _statement_event(account_id: str, cursor: str | None = None, page_size: int = 20) -> dict:
    qsp: dict = {}
    if cursor:
        qsp["cursor"] = cursor
    qsp["page_size"] = str(page_size)
    return {
        "pathParameters": {"account_id": account_id},
        "queryStringParameters": qsp or None,
        "requestContext": {"requestId": str(uuid.uuid4())},
    }


def _parse(response: dict) -> dict:
    return json.loads(response["body"])


def _make_entry_body(
    external_id: str | None = None,
    debit_account: str = "acc_available_e2e",
    credit_account: str = "acc_hold_e2e",
    amount: int = 5000,
    currency: str = "BRL",
    metadata: dict | None = None,
) -> dict:
    return {
        "external_id": external_id or str(uuid.uuid4()),
        "postings": [
            {"account_id": debit_account, "amount": amount, "currency": currency, "direction": "DEBIT"},
            {"account_id": credit_account, "amount": amount, "currency": currency, "direction": "CREDIT"},
        ],
        "metadata": metadata or {},
    }


# ===========================================================================
# 1. API Write → DynamoDB → API Read (ciclo completo)
# ===========================================================================


class TestFullLifecycleAPIToDynamo:
    """
    Testa o ciclo de vida completo via API handlers contra DynamoDB real:
    POST /entries → GET /balances → GET /statements → POST /reversals → verify
    """

    def test_create_entry_via_api_persists_and_reads_correctly(
        self, command_handler, query_handler, dynamodb_table
    ):
        """
        Fluxo completo:
        1. POST /entries cria lançamento no DynamoDB
        2. GET /balances retorna saldo correto
        3. GET /statements retorna postings corretos
        """
        debit_acc = f"acc_avail_{uuid.uuid4().hex[:8]}"
        credit_acc = f"acc_hold_{uuid.uuid4().hex[:8]}"
        amount = 7500
        ext_id = str(uuid.uuid4())

        # 1. Cria lançamento via API handler
        body = _make_entry_body(
            external_id=ext_id,
            debit_account=debit_acc,
            credit_account=credit_acc,
            amount=amount,
        )
        create_resp = handle_create_entry(
            _api_event(body), context=None, command_handler=command_handler
        )
        assert create_resp["statusCode"] == 201
        create_data = _parse(create_resp)["data"]
        entry_id = create_data["entry_id"]
        assert create_data["external_id"] == ext_id
        assert create_data["entry_type"] == "STANDARD"
        assert len(create_data["postings"]) == 2

        # 2. Consulta saldo da conta debitada
        bal_resp = handle_get_balance(
            _balance_event(debit_acc, "BRL"), context=None, query_handler=query_handler
        )
        assert bal_resp["statusCode"] == 200
        bal_data = _parse(bal_resp)["data"]
        assert bal_data["balance_amount"] == amount  # DEBIT = +amount
        assert bal_data["version"] == 1

        # 3. Consulta saldo da conta creditada
        bal_resp2 = handle_get_balance(
            _balance_event(credit_acc, "BRL"), context=None, query_handler=query_handler
        )
        assert bal_resp2["statusCode"] == 200
        bal_data2 = _parse(bal_resp2)["data"]
        assert bal_data2["balance_amount"] == -amount  # CREDIT = -amount

        # 4. Consulta extrato da conta debitada
        stmt_resp = handle_get_statement(
            _statement_event(debit_acc), context=None, query_handler=query_handler
        )
        assert stmt_resp["statusCode"] == 200
        stmt_data = _parse(stmt_resp)["data"]
        assert len(stmt_data["postings"]) == 1
        assert stmt_data["postings"][0]["amount"] == amount
        assert stmt_data["postings"][0]["direction"] == "DEBIT"

    def test_full_lifecycle_create_read_reverse_verify(
        self, command_handler, query_handler, dynamodb_table
    ):
        """
        Ciclo completo: create → read → reverse → verify saldos zerados.
        """
        debit_acc = f"acc_lifecycle_d_{uuid.uuid4().hex[:8]}"
        credit_acc = f"acc_lifecycle_c_{uuid.uuid4().hex[:8]}"
        amount = 10000

        # Cria lançamento
        body = _make_entry_body(
            debit_account=debit_acc, credit_account=credit_acc, amount=amount
        )
        resp = handle_create_entry(
            _api_event(body), context=None, command_handler=command_handler
        )
        assert resp["statusCode"] == 201
        entry_id = _parse(resp)["data"]["entry_id"]

        # Verifica saldos após criação
        bal_d = _parse(handle_get_balance(
            _balance_event(debit_acc, "BRL"), None, query_handler
        ))["data"]
        assert bal_d["balance_amount"] == amount

        # Cria reversão via API
        reversal_body = {
            "original_entry_id": entry_id,
            "external_id": str(uuid.uuid4()),
            "metadata": {"reason": "e2e_test"},
        }
        rev_resp = handle_create_reversal(
            _api_event(reversal_body), context=None, command_handler=command_handler
        )
        assert rev_resp["statusCode"] == 201
        rev_data = _parse(rev_resp)["data"]
        assert rev_data["entry_type"] == "REVERSAL"
        assert rev_data["metadata"]["original_entry_id"] == entry_id

        # Verifica saldos zerados após reversão
        bal_d_after = _parse(handle_get_balance(
            _balance_event(debit_acc, "BRL"), None, query_handler
        ))["data"]
        bal_c_after = _parse(handle_get_balance(
            _balance_event(credit_acc, "BRL"), None, query_handler
        ))["data"]
        assert bal_d_after["balance_amount"] == 0, "Saldo debit deve zerar após reversão"
        assert bal_c_after["balance_amount"] == 0, "Saldo credit deve zerar após reversão"

        # Verifica extrato tem 2 postings (original + reversal)
        stmt = _parse(handle_get_statement(
            _statement_event(debit_acc), None, query_handler
        ))["data"]
        assert len(stmt["postings"]) == 2

    def test_multiple_entries_accumulate_balance_correctly(
        self, command_handler, query_handler, dynamodb_table
    ):
        """Múltiplos lançamentos acumulam saldo corretamente."""
        acc = f"acc_multi_{uuid.uuid4().hex[:8]}"
        clearing = f"acc_clr_{uuid.uuid4().hex[:8]}"

        for i in range(3):
            body = _make_entry_body(
                debit_account=acc, credit_account=clearing, amount=1000 * (i + 1)
            )
            resp = handle_create_entry(
                _api_event(body), None, command_handler
            )
            assert resp["statusCode"] == 201

        bal = _parse(handle_get_balance(
            _balance_event(acc, "BRL"), None, query_handler
        ))["data"]
        # 1000 + 2000 + 3000 = 6000
        assert bal["balance_amount"] == 6000
        assert bal["version"] == 3

    def test_statement_pagination_against_dynamo(
        self, command_handler, query_handler, dynamodb_table
    ):
        """Paginação de extrato funciona corretamente contra DynamoDB real."""
        acc = f"acc_page_{uuid.uuid4().hex[:8]}"
        clearing = f"acc_clr_page_{uuid.uuid4().hex[:8]}"

        for _ in range(5):
            body = _make_entry_body(debit_account=acc, credit_account=clearing, amount=500)
            handle_create_entry(_api_event(body), None, command_handler)

        # Página 1 (size=2)
        p1 = _parse(handle_get_statement(
            _statement_event(acc, page_size=2), None, query_handler
        ))["data"]
        assert len(p1["postings"]) == 2
        assert p1["has_more"] is True

        # Página 2
        p2 = _parse(handle_get_statement(
            _statement_event(acc, cursor=p1["next_cursor"], page_size=2), None, query_handler
        ))["data"]
        assert len(p2["postings"]) == 2
        assert p2["has_more"] is True

        # Página 3 (última)
        p3 = _parse(handle_get_statement(
            _statement_event(acc, cursor=p2["next_cursor"], page_size=2), None, query_handler
        ))["data"]
        assert len(p3["postings"]) == 1
        assert p3["has_more"] is False


# ===========================================================================
# 2. API Error Propagation end-to-end contra DynamoDB
# ===========================================================================


class TestAPIErrorPropagationE2E:
    """Erros de domínio propagados corretamente da API até o DynamoDB."""

    def test_zero_sum_violation_via_api(self, command_handler, dynamodb_table):
        """Postings desbalanceados retornam 400 ZERO_SUM_VIOLATION."""
        body = {
            "external_id": str(uuid.uuid4()),
            "postings": [
                {"account_id": "a", "amount": 1000, "currency": "BRL", "direction": "DEBIT"},
                {"account_id": "b", "amount": 500, "currency": "BRL", "direction": "CREDIT"},
            ],
        }
        resp = handle_create_entry(_api_event(body), None, command_handler)
        assert resp["statusCode"] == 400
        assert _parse(resp)["error"]["code"] == "ZERO_SUM_VIOLATION"

    def test_float_amount_rejected_via_api(self, command_handler, dynamodb_table):
        """Amount float rejeitado na camada de schema validation."""
        body = {
            "external_id": str(uuid.uuid4()),
            "postings": [
                {"account_id": "a", "amount": 10.5, "currency": "BRL", "direction": "DEBIT"},
                {"account_id": "b", "amount": 10.5, "currency": "BRL", "direction": "CREDIT"},
            ],
        }
        resp = handle_create_entry(_api_event(body), None, command_handler)
        assert resp["statusCode"] == 400
        err = _parse(resp)["error"]
        assert err["code"] in ("SCHEMA_VALIDATION_ERROR", "INVALID_AMOUNT_TYPE")

    def test_idempotency_via_api_returns_200(self, command_handler, dynamodb_table):
        """Submissão duplicada retorna 200 com entry_id original."""
        ext_id = str(uuid.uuid4())
        body = _make_entry_body(external_id=ext_id)

        r1 = handle_create_entry(_api_event(body), None, command_handler)
        assert r1["statusCode"] == 201
        original_id = _parse(r1)["data"]["entry_id"]

        r2 = handle_create_entry(_api_event(body), None, command_handler)
        assert r2["statusCode"] == 200
        data2 = _parse(r2)["data"]
        assert data2["entry_id"] == original_id
        assert data2["idempotent"] is True

    def test_reversal_of_nonexistent_entry_returns_404(self, command_handler, dynamodb_table):
        """Reversão de entry inexistente retorna 404."""
        body = {
            "original_entry_id": str(uuid.uuid4()),
            "external_id": str(uuid.uuid4()),
        }
        resp = handle_create_reversal(_api_event(body), None, command_handler)
        assert resp["statusCode"] == 404
        assert _parse(resp)["error"]["code"] == "JOURNAL_ENTRY_NOT_FOUND"

    def test_invalid_json_returns_400(self, command_handler, dynamodb_table):
        """JSON inválido retorna 400."""
        event = {"body": "not-json{", "requestContext": {"requestId": "test"}}
        resp = handle_create_entry(event, None, command_handler)
        assert resp["statusCode"] == 400
        assert _parse(resp)["error"]["code"] == "INVALID_JSON"

    def test_missing_fields_returns_400(self, command_handler, dynamodb_table):
        """Campos obrigatórios ausentes retornam 400."""
        body = {"postings": []}  # falta external_id, postings vazio
        resp = handle_create_entry(_api_event(body), None, command_handler)
        assert resp["statusCode"] == 400

    def test_balance_missing_currency_returns_400(self, query_handler, dynamodb_table):
        """GET /balances sem currency retorna 400."""
        event = {
            "pathParameters": {"account_id": "acc_test"},
            "queryStringParameters": {},
            "requestContext": {"requestId": "test"},
        }
        resp = handle_get_balance(event, None, query_handler)
        assert resp["statusCode"] == 400

    def test_balance_nonexistent_account_returns_null(self, query_handler, dynamodb_table):
        """GET /balances para conta sem saldo retorna data: null."""
        resp = handle_get_balance(
            _balance_event("nonexistent_acc", "BRL"), None, query_handler
        )
        assert resp["statusCode"] == 200
        assert _parse(resp)["data"] is None


# ===========================================================================
# 3. Publisher Lambda — processamento de eventos do DynamoDB Stream
# ===========================================================================


class TestPublisherLambdaE2E:
    """
    Testa o Publisher Lambda processando eventos do DynamoDB Stream.
    Usa mock do EventBridge e SQS, mas dados reais do DynamoDB.
    """

    def test_publisher_processes_outbox_event_from_dynamo(
        self, ledger_engine, dynamodb_client, dynamodb_table
    ):
        """
        Após criar um JournalEntry no DynamoDB, simula o evento do Stream
        e verifica que o Publisher publica no EventBridge corretamente.
        """
        # Cria lançamento real no DynamoDB
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_pub_d", 2000, "BRL", "DEBIT"),
                PostingInput("acc_pub_c", 2000, "BRL", "CREDIT"),
            ],
            metadata={},
        )
        entry = ledger_engine.create_journal_entry(cmd)

        # Lê o OutboxEvent real do DynamoDB
        outbox_resp = dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key={
                "PK": {"S": f"OUTBOX#{entry.entry_id}"},
                "SK": {"S": f"OUTBOX#{entry.entry_id}"},
            },
        )
        outbox_item = outbox_resp["Item"]
        assert outbox_item is not None, "OutboxEvent deve existir no DynamoDB"

        # Simula o evento do DynamoDB Stream (formato Lambda event)
        stream_record = {
            "eventName": "INSERT",
            "dynamodb": {"NewImage": outbox_item},
        }

        # Mock do EventBridge e SQS
        mock_eb = MagicMock()
        mock_sqs = MagicMock()
        publisher = OutboxEventPublisher(
            eventbridge_client=mock_eb,
            sqs_client=mock_sqs,
            event_bus_name="test-bus",
            dlq_url="",
        )

        result = publisher.process_stream_records([stream_record])

        assert result["published"] == 1
        assert result["failed"] == 0
        assert result["filtered"] == 0

        # Verifica que PutEvents foi chamado com os dados corretos
        mock_eb.put_events.assert_called_once()
        call_args = mock_eb.put_events.call_args
        entries = call_args[1]["Entries"] if "Entries" in call_args[1] else call_args[0][0]
        assert entries[0]["DetailType"] == "TransactionCreated"
        assert entries[0]["Source"] == "ledger.subledger"

    def test_publisher_filters_non_outbox_records(
        self, ledger_engine, dynamodb_client, dynamodb_table
    ):
        """Publisher ignora registros que não são OUTBOX#."""
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_filt_d", 1000, "BRL", "DEBIT"),
                PostingInput("acc_filt_c", 1000, "BRL", "CREDIT"),
            ],
            metadata={},
        )
        entry = ledger_engine.create_journal_entry(cmd)

        # Lê o JournalEntry (não é OUTBOX#)
        journal_resp = dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key={
                "PK": {"S": f"JOURNAL#{entry.entry_id}"},
                "SK": {"S": f"JOURNAL#{entry.entry_id}"},
            },
        )

        stream_record = {
            "eventName": "INSERT",
            "dynamodb": {"NewImage": journal_resp["Item"]},
        }

        mock_eb = MagicMock()
        mock_sqs = MagicMock()
        publisher = OutboxEventPublisher(mock_eb, mock_sqs, "test-bus", "")

        result = publisher.process_stream_records([stream_record])
        assert result["published"] == 0
        assert result["filtered"] == 1
        mock_eb.put_events.assert_not_called()

    def test_publisher_sends_to_dlq_on_eventbridge_failure(
        self, ledger_engine, dynamodb_client, dynamodb_table
    ):
        """Publisher encaminha para DLQ quando EventBridge falha."""
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_dlq_d", 1000, "BRL", "DEBIT"),
                PostingInput("acc_dlq_c", 1000, "BRL", "CREDIT"),
            ],
            metadata={},
        )
        entry = ledger_engine.create_journal_entry(cmd)

        outbox_resp = dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key={
                "PK": {"S": f"OUTBOX#{entry.entry_id}"},
                "SK": {"S": f"OUTBOX#{entry.entry_id}"},
            },
        )

        stream_record = {
            "eventName": "INSERT",
            "dynamodb": {"NewImage": outbox_resp["Item"]},
        }

        mock_eb = MagicMock()
        mock_eb.put_events.side_effect = Exception("EventBridge unavailable")
        mock_sqs = MagicMock()

        publisher = OutboxEventPublisher(mock_eb, mock_sqs, "test-bus", "https://sqs.test/dlq")
        result = publisher.process_stream_records([stream_record])

        assert result["failed"] == 1
        mock_sqs.send_message.assert_called_once()

    def test_publisher_handles_reversal_event_type(
        self, ledger_engine, dynamodb_client, dynamodb_table
    ):
        """Publisher publica TransactionReversed para reversões."""
        # Cria original
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_rev_pub_d", 3000, "BRL", "DEBIT"),
                PostingInput("acc_rev_pub_c", 3000, "BRL", "CREDIT"),
            ],
            metadata={},
        )
        original = ledger_engine.create_journal_entry(cmd)

        # Cria reversão
        from ledger.application.commands import CreateReversalCommand
        rev_cmd = CreateReversalCommand(
            original_entry_id=original.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={},
        )
        reversal = ledger_engine.create_reversal(rev_cmd)

        # Lê OutboxEvent da reversão
        outbox_resp = dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key={
                "PK": {"S": f"OUTBOX#{reversal.entry_id}"},
                "SK": {"S": f"OUTBOX#{reversal.entry_id}"},
            },
        )

        stream_record = {
            "eventName": "INSERT",
            "dynamodb": {"NewImage": outbox_resp["Item"]},
        }

        mock_eb = MagicMock()
        mock_sqs = MagicMock()
        publisher = OutboxEventPublisher(mock_eb, mock_sqs, "test-bus", "")

        result = publisher.process_stream_records([stream_record])
        assert result["published"] == 1

        call_args = mock_eb.put_events.call_args
        entries = call_args[1]["Entries"]
        assert entries[0]["DetailType"] == "TransactionReversed"


# ===========================================================================
# 4. Audit Pipeline — AuditTransformer processando dados reais do DynamoDB
# ===========================================================================


class TestAuditPipelineE2E:
    """
    Testa o Audit Pipeline processando registros reais do DynamoDB.
    Usa mock do Firehose, mas dados reais do DynamoDB.
    """

    def test_audit_transformer_processes_journal_and_postings(
        self, ledger_engine, dynamodb_client, dynamodb_table
    ):
        """
        Após criar um JournalEntry, simula os eventos do Stream para
        JOURNAL# e POSTING# e verifica que o AuditTransformer envia
        registros corretos para o Firehose.
        """
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_audit_d", 4000, "BRL", "DEBIT"),
                PostingInput("acc_audit_c", 4000, "BRL", "CREDIT"),
            ],
            metadata={"tenant_id": "tenant_001"},
        )
        entry = ledger_engine.create_journal_entry(cmd)

        # Lê itens reais do DynamoDB para simular o Stream
        journal_resp = dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key={
                "PK": {"S": f"JOURNAL#{entry.entry_id}"},
                "SK": {"S": f"JOURNAL#{entry.entry_id}"},
            },
        )

        # Busca postings via GSI
        postings_resp = dynamodb_client.query(
            TableName=dynamodb_table,
            IndexName="GSI-EntryPostings",
            KeyConditionExpression="entry_id_gsi = :eid",
            ExpressionAttributeValues={
                ":eid": {"S": f"JOURNAL#{entry.entry_id}"},
            },
        )

        # Lê o OutboxEvent (deve ser filtrado pelo audit transformer)
        outbox_resp = dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key={
                "PK": {"S": f"OUTBOX#{entry.entry_id}"},
                "SK": {"S": f"OUTBOX#{entry.entry_id}"},
            },
        )

        # Monta stream records simulando DynamoDB Streams
        stream_records = []
        # JournalEntry
        stream_records.append({
            "eventName": "INSERT",
            "dynamodb": {"NewImage": journal_resp["Item"]},
        })
        # Postings
        for item in postings_resp.get("Items", []):
            stream_records.append({
                "eventName": "INSERT",
                "dynamodb": {"NewImage": item},
            })
        # OutboxEvent (deve ser filtrado — não é JOURNAL# nem POSTING#)
        stream_records.append({
            "eventName": "INSERT",
            "dynamodb": {"NewImage": outbox_resp["Item"]},
        })

        # Mock do Firehose
        mock_firehose = MagicMock()
        mock_firehose.put_record_batch.return_value = {"FailedPutCount": 0}

        transformer = AuditTransformer(
            firehose_stream_name="test-audit-stream",
            firehose_client=mock_firehose,
        )

        sent_count = transformer.process_stream_records(stream_records)

        # 1 JOURNAL + 2 POSTING = 3 registros (OUTBOX filtrado)
        assert sent_count == 3, f"Esperado 3 registros enviados, obtido: {sent_count}"
        mock_firehose.put_record_batch.assert_called_once()

        # Verifica conteúdo dos registros enviados
        call_args = mock_firehose.put_record_batch.call_args
        records = call_args[1]["Records"]
        assert len(records) == 3

        # Deserializa e verifica os AuditRecords
        audit_records = [json.loads(r["Data"].decode("utf-8")) for r in records]

        journal_records = [r for r in audit_records if r["record_type"] == "JOURNAL_ENTRY"]
        posting_records = [r for r in audit_records if r["record_type"] == "POSTING"]

        assert len(journal_records) == 1
        assert len(posting_records) == 2

        # Verifica campos do JOURNAL_ENTRY
        jr = journal_records[0]
        assert jr["entry_id"] == entry.entry_id
        assert jr["entry_type"] == "STANDARD"
        assert jr["tenant_id"] == "tenant_001"
        assert jr["year"] and jr["month"] and jr["day"]  # campos de particionamento

        # Verifica campos dos POSTINGs
        for pr in posting_records:
            assert pr["entry_id"] == entry.entry_id
            assert pr["amount"] == 4000
            assert pr["currency"] == "BRL"
            assert pr["direction"] in ("DEBIT", "CREDIT")

    def test_audit_transformer_filters_balance_records(
        self, ledger_engine, dynamodb_client, dynamodb_table
    ):
        """AuditTransformer descarta registros BALANCE# corretamente."""
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_bal_filt_d", 1000, "BRL", "DEBIT"),
                PostingInput("acc_bal_filt_c", 1000, "BRL", "CREDIT"),
            ],
            metadata={},
        )
        ledger_engine.create_journal_entry(cmd)

        # Lê o Balance (deve ser filtrado)
        balance_resp = dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key={
                "PK": {"S": "ACCOUNT#acc_bal_filt_d"},
                "SK": {"S": "BALANCE#BRL"},
            },
        )

        stream_records = [{
            "eventName": "INSERT",
            "dynamodb": {"NewImage": balance_resp["Item"]},
        }]

        mock_firehose = MagicMock()
        mock_firehose.put_record_batch.return_value = {"FailedPutCount": 0}

        transformer = AuditTransformer("test-stream", mock_firehose)
        sent = transformer.process_stream_records(stream_records)

        assert sent == 0, "BALANCE# deve ser filtrado pelo AuditTransformer"
        mock_firehose.put_record_batch.assert_not_called()

    def test_audit_transformer_handles_firehose_partial_failure(
        self, ledger_engine, dynamodb_client, dynamodb_table
    ):
        """AuditTransformer reporta falhas parciais do Firehose."""
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_pf_d", 1000, "BRL", "DEBIT"),
                PostingInput("acc_pf_c", 1000, "BRL", "CREDIT"),
            ],
            metadata={"tenant_id": "t1"},
        )
        entry = ledger_engine.create_journal_entry(cmd)

        journal_resp = dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key={
                "PK": {"S": f"JOURNAL#{entry.entry_id}"},
                "SK": {"S": f"JOURNAL#{entry.entry_id}"},
            },
        )

        stream_records = [{
            "eventName": "INSERT",
            "dynamodb": {"NewImage": journal_resp["Item"]},
        }]

        mock_firehose = MagicMock()
        # Simula 1 falha de 1 registro
        mock_firehose.put_record_batch.return_value = {"FailedPutCount": 1}

        transformer = AuditTransformer("test-stream", mock_firehose)
        sent = transformer.process_stream_records(stream_records)

        assert sent == 0, "Com 1 falha de 1 registro, sent deve ser 0"


# ===========================================================================
# 5. Audit Handler Lambda — ponto de entrada da Lambda
# ===========================================================================


class TestAuditHandlerLambdaE2E:
    """
    Testa o handler Lambda do Audit Pipeline com dados reais do DynamoDB.
    Usa AuditTransformer diretamente (o handler é um thin wrapper).
    """

    def test_audit_handler_invocation_with_real_data(
        self, ledger_engine, dynamodb_client, dynamodb_table
    ):
        """
        Simula invocação do Audit Pipeline com evento real do Stream.
        Testa o AuditTransformer diretamente (o handler Lambda é apenas
        um wrapper que instancia boto3 clients e delega).
        """
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_ah_d", 2000, "BRL", "DEBIT"),
                PostingInput("acc_ah_c", 2000, "BRL", "CREDIT"),
            ],
            metadata={"tenant_id": "tenant_test"},
        )
        entry = ledger_engine.create_journal_entry(cmd)

        # Lê JournalEntry do DynamoDB
        journal_resp = dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key={
                "PK": {"S": f"JOURNAL#{entry.entry_id}"},
                "SK": {"S": f"JOURNAL#{entry.entry_id}"},
            },
        )

        # Simula evento Lambda do DynamoDB Stream
        records = [
            {
                "eventName": "INSERT",
                "dynamodb": {"NewImage": journal_resp["Item"]},
            }
        ]

        # Testa via AuditTransformer diretamente (o handler é thin wrapper)
        mock_firehose = MagicMock()
        mock_firehose.put_record_batch.return_value = {"FailedPutCount": 0}

        transformer = AuditTransformer(
            firehose_stream_name="test-audit-stream",
            firehose_client=mock_firehose,
        )
        sent = transformer.process_stream_records(records)

        assert sent == 1
        mock_firehose.put_record_batch.assert_called_once()

        # Verifica que o registro enviado é um JOURNAL_ENTRY
        call_args = mock_firehose.put_record_batch.call_args
        firehose_records = call_args[1]["Records"]
        audit_record = json.loads(firehose_records[0]["Data"].decode("utf-8"))
        assert audit_record["record_type"] == "JOURNAL_ENTRY"
        assert audit_record["entry_id"] == entry.entry_id


# ===========================================================================
# 6. Publisher Handler Lambda — ponto de entrada da Lambda
# ===========================================================================


class TestPublisherHandlerLambdaE2E:
    """
    Testa o Publisher Lambda com dados reais do DynamoDB.
    Usa OutboxEventPublisher diretamente (o handler é um thin wrapper).
    """

    def test_publisher_handler_invocation_with_real_data(
        self, ledger_engine, dynamodb_client, dynamodb_table
    ):
        """
        Simula invocação do Publisher com evento real do Stream.
        Testa via OutboxEventPublisher diretamente.
        """
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_ph_d", 1500, "BRL", "DEBIT"),
                PostingInput("acc_ph_c", 1500, "BRL", "CREDIT"),
            ],
            metadata={},
        )
        entry = ledger_engine.create_journal_entry(cmd)

        outbox_resp = dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key={
                "PK": {"S": f"OUTBOX#{entry.entry_id}"},
                "SK": {"S": f"OUTBOX#{entry.entry_id}"},
            },
        )

        records = [
            {
                "eventName": "INSERT",
                "dynamodb": {"NewImage": outbox_resp["Item"]},
            }
        ]

        mock_eb = MagicMock()
        mock_sqs = MagicMock()

        publisher = OutboxEventPublisher(mock_eb, mock_sqs, "test-bus", "")
        result = publisher.process_stream_records(records)

        assert result["published"] == 1
        mock_eb.put_events.assert_called_once()

        # Verifica payload do evento publicado
        call_args = mock_eb.put_events.call_args
        entries = call_args[1]["Entries"]
        detail = json.loads(entries[0]["Detail"])
        assert detail["entry_id"] == entry.entry_id


# ===========================================================================
# 7. DynamoDB Mapper round-trip com dados reais
# ===========================================================================


class TestMapperRoundTripE2E:
    """
    Verifica que o mapeamento domínio → DynamoDB → domínio preserva dados.
    """

    def test_journal_entry_round_trip_via_dynamo(
        self, ledger_engine, repository, dynamodb_table
    ):
        """
        Cria JournalEntry via engine, lê de volta via repository,
        e verifica que todos os campos são preservados.
        """
        ext_id = str(uuid.uuid4())
        cmd = CreateJournalEntryCommand(
            external_id=ext_id,
            postings=[
                PostingInput("acc_rt_d", 8888, "USD", "DEBIT"),
                PostingInput("acc_rt_c", 8888, "USD", "CREDIT"),
            ],
            metadata={"test_key": "test_value"},
        )
        created = ledger_engine.create_journal_entry(cmd)

        # Lê de volta via repository
        found = repository.find_journal_entry_by_id(created.entry_id)
        assert found is not None

        # Verifica campos do JournalEntry
        assert found.entry_id == created.entry_id
        assert found.external_id == ext_id
        assert found.entry_type == created.entry_type
        assert found.timestamp == created.timestamp
        assert found.metadata == {"test_key": "test_value"}

        # Verifica postings
        assert len(found.postings) == 2
        amounts = sorted([p.money.amount for p in found.postings])
        assert amounts == [8888, 8888]
        currencies = {p.money.currency for p in found.postings}
        assert currencies == {"USD"}
        directions = {p.direction.value for p in found.postings}
        assert directions == {"DEBIT", "CREDIT"}

    def test_find_by_external_id_round_trip(
        self, ledger_engine, repository, dynamodb_table
    ):
        """find_journal_entry_by_external_id retorna entry correto."""
        ext_id = f"ext-rt-{uuid.uuid4()}"
        cmd = CreateJournalEntryCommand(
            external_id=ext_id,
            postings=[
                PostingInput("acc_ext_d", 500, "EUR", "DEBIT"),
                PostingInput("acc_ext_c", 500, "EUR", "CREDIT"),
            ],
            metadata={},
        )
        created = ledger_engine.create_journal_entry(cmd)

        found = repository.find_journal_entry_by_external_id(ext_id)
        assert found is not None
        assert found.entry_id == created.entry_id


# ===========================================================================
# 8. Hold/Release completo via API handlers
# ===========================================================================


class TestHoldReleaseViaAPI:
    """
    Testa o ciclo hold/release completo via API handlers contra DynamoDB.
    """

    def test_hold_release_via_api_handlers(
        self, command_handler, query_handler, dynamodb_table
    ):
        """
        1. POST /entries (hold: DEBIT Available, CREDIT Hold)
        2. Verifica saldos via GET /balances
        3. POST /entries (release: DEBIT Hold, CREDIT Available)
        4. Verifica saldos zerados via GET /balances
        """
        avail = f"acc_avail_hr_{uuid.uuid4().hex[:8]}"
        hold = f"acc_hold_hr_{uuid.uuid4().hex[:8]}"
        amount = 15000

        # Hold
        hold_body = _make_entry_body(
            debit_account=avail, credit_account=hold, amount=amount,
            metadata={"op": "hold"},
        )
        r = handle_create_entry(_api_event(hold_body), None, command_handler)
        assert r["statusCode"] == 201

        # Verifica saldos após hold
        avail_bal = _parse(handle_get_balance(
            _balance_event(avail, "BRL"), None, query_handler
        ))["data"]
        hold_bal = _parse(handle_get_balance(
            _balance_event(hold, "BRL"), None, query_handler
        ))["data"]
        assert avail_bal["balance_amount"] == amount
        assert hold_bal["balance_amount"] == -amount

        # Release (inverso do hold)
        release_body = _make_entry_body(
            debit_account=hold, credit_account=avail, amount=amount,
            metadata={"op": "release"},
        )
        r2 = handle_create_entry(_api_event(release_body), None, command_handler)
        assert r2["statusCode"] == 201

        # Verifica saldos zerados
        avail_after = _parse(handle_get_balance(
            _balance_event(avail, "BRL"), None, query_handler
        ))["data"]
        hold_after = _parse(handle_get_balance(
            _balance_event(hold, "BRL"), None, query_handler
        ))["data"]
        assert avail_after["balance_amount"] == 0
        assert hold_after["balance_amount"] == 0

        # Verifica extrato tem 2 postings em cada conta
        avail_stmt = _parse(handle_get_statement(
            _statement_event(avail), None, query_handler
        ))["data"]
        assert len(avail_stmt["postings"]) == 2


# ===========================================================================
# 9. Multi-currency support
# ===========================================================================


class TestMultiCurrencyE2E:
    """Testa operações com múltiplas moedas."""

    def test_multi_currency_entry_and_balances(
        self, command_handler, query_handler, dynamodb_table
    ):
        """Lançamento com múltiplas moedas cria saldos separados."""
        acc_d = f"acc_mc_d_{uuid.uuid4().hex[:8]}"
        acc_c = f"acc_mc_c_{uuid.uuid4().hex[:8]}"

        # Lançamento BRL
        body_brl = _make_entry_body(
            debit_account=acc_d, credit_account=acc_c, amount=5000, currency="BRL"
        )
        r1 = handle_create_entry(_api_event(body_brl), None, command_handler)
        assert r1["statusCode"] == 201

        # Lançamento USD
        body_usd = {
            "external_id": str(uuid.uuid4()),
            "postings": [
                {"account_id": acc_d, "amount": 2000, "currency": "USD", "direction": "DEBIT"},
                {"account_id": acc_c, "amount": 2000, "currency": "USD", "direction": "CREDIT"},
            ],
        }
        r2 = handle_create_entry(_api_event(body_usd), None, command_handler)
        assert r2["statusCode"] == 201

        # Verifica saldos por moeda
        brl_bal = _parse(handle_get_balance(
            _balance_event(acc_d, "BRL"), None, query_handler
        ))["data"]
        usd_bal = _parse(handle_get_balance(
            _balance_event(acc_d, "USD"), None, query_handler
        ))["data"]

        assert brl_bal["balance_amount"] == 5000
        assert brl_bal["currency"] == "BRL"
        assert usd_bal["balance_amount"] == 2000
        assert usd_bal["currency"] == "USD"
