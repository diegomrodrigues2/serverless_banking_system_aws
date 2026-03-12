"""
Testes end-to-end contra AWS real (dev) — us-east-1.

Recursos utilizados:
  - DynamoDB:   ledger-dev
  - Lambda:     double-entry-ledger-dev-write / read / publisher / audit-transform
  - API Gateway: https://rx5o7imnxh.execute-api.us-east-1.amazonaws.com
  - EventBridge: ledger-events-dev
  - Firehose:   ledger-audit-dev

Cada teste usa account_ids com UUID para evitar colisão entre execuções.
Não há teardown de dados — os itens ficam na tabela (append-only por design).

Uso:
    pytest tests/ledger/integration/test_aws_e2e.py -v --no-header
"""
from __future__ import annotations

import json
import time
import uuid
from unittest.mock import MagicMock

import boto3
import pytest

from ledger.api.write_handler import handle_create_entry, handle_create_reversal
from ledger.api.read_handler import handle_get_balance, handle_get_statement
from ledger.application.commands import (
    CreateJournalEntryCommand,
    CreateReversalCommand,
    PostingInput,
)
from ledger.application.handlers import CommandHandler, QueryHandler
from ledger.infrastructure.publisher import OutboxEventPublisher
from ledger.infrastructure.audit_exporter import AuditTransformer

# Importa fixtures do conftest_aws via pytest (conftest_aws.py no mesmo diretório)
from tests.ledger.integration.conftest_aws import (
    aws_dynamodb_client,
    aws_repository,
    aws_ledger_engine,
)

pytestmark = pytest.mark.integration

API_ENDPOINT = "https://rx5o7imnxh.execute-api.us-east-1.amazonaws.com"
AWS_REGION = "us-east-1"
TABLE_NAME = "ledger-dev"
EVENT_BUS = "ledger-events-dev"
FIREHOSE_STREAM = "ledger-audit-dev"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _api_event(body: dict) -> dict:
    return {
        "body": json.dumps(body),
        "requestContext": {"requestId": str(uuid.uuid4())},
    }


def _balance_event(account_id: str, currency: str) -> dict:
    return {
        "pathParameters": {"account_id": account_id},
        "queryStringParameters": {"currency": currency},
        "requestContext": {"requestId": str(uuid.uuid4())},
    }


def _statement_event(account_id: str, cursor: str | None = None, page_size: int = 20) -> dict:
    qsp: dict = {"page_size": str(page_size)}
    if cursor:
        qsp["cursor"] = cursor
    return {
        "pathParameters": {"account_id": account_id},
        "queryStringParameters": qsp,
        "requestContext": {"requestId": str(uuid.uuid4())},
    }


def _parse(response: dict) -> dict:
    return json.loads(response["body"])


def _uid() -> str:
    """Gera sufixo único curto para account_ids de teste."""
    return uuid.uuid4().hex[:10]


def _make_transfer(
    debit: str,
    credit: str,
    amount: int = 5000,
    currency: str = "BRL",
    ext_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    return {
        "external_id": ext_id or str(uuid.uuid4()),
        "postings": [
            {"account_id": debit, "amount": amount, "currency": currency, "direction": "DEBIT"},
            {"account_id": credit, "amount": amount, "currency": currency, "direction": "CREDIT"},
        ],
        "metadata": metadata or {},
    }


# ---------------------------------------------------------------------------
# Fixtures locais
# ---------------------------------------------------------------------------


@pytest.fixture
def command_handler(aws_ledger_engine):
    return CommandHandler(engine=aws_ledger_engine)


@pytest.fixture
def query_handler(aws_repository):
    return QueryHandler(repository=aws_repository)


# ===========================================================================
# 1. Write Lambda — POST /entries via handler direto contra DynamoDB real
# ===========================================================================


class TestWritePathAWS:
    """Testa o Write Path contra DynamoDB real em us-east-1."""

    def test_create_entry_persists_all_items(
        self, aws_ledger_engine, aws_dynamodb_client
    ):
        """
        Cria um JournalEntry e verifica que todos os itens da TransactWriteItems
        foram persistidos na tabela real: JournalEntry, Postings, Balances, OutboxEvent.
        """
        uid = _uid()
        debit_acc = f"aws-test-avail-{uid}"
        credit_acc = f"aws-test-hold-{uid}"

        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(debit_acc, 10000, "BRL", "DEBIT"),
                PostingInput(credit_acc, 10000, "BRL", "CREDIT"),
            ],
            metadata={"env": "aws-e2e-test"},
        )
        entry = aws_ledger_engine.create_journal_entry(cmd)

        # Verifica JournalEntry
        j = aws_dynamodb_client.get_item(
            TableName=TABLE_NAME,
            Key={"PK": {"S": f"JOURNAL#{entry.entry_id}"}, "SK": {"S": f"JOURNAL#{entry.entry_id}"}},
        )
        assert j.get("Item"), "JournalEntry deve existir no DynamoDB real"
        assert j["Item"]["entry_id"]["S"] == entry.entry_id

        # Verifica OutboxEvent
        o = aws_dynamodb_client.get_item(
            TableName=TABLE_NAME,
            Key={"PK": {"S": f"OUTBOX#{entry.entry_id}"}, "SK": {"S": f"OUTBOX#{entry.entry_id}"}},
        )
        assert o.get("Item"), "OutboxEvent deve existir no DynamoDB real"
        assert o["Item"]["event_type"]["S"] == "TransactionCreated"

        # Verifica Balances
        bal_d = aws_dynamodb_client.get_item(
            TableName=TABLE_NAME,
            Key={"PK": {"S": f"ACCOUNT#{debit_acc}"}, "SK": {"S": "BALANCE#BRL"}},
        )
        assert bal_d.get("Item"), "Balance da conta debitada deve existir"
        assert int(bal_d["Item"]["balance_amount"]["N"]) == 10000

        bal_c = aws_dynamodb_client.get_item(
            TableName=TABLE_NAME,
            Key={"PK": {"S": f"ACCOUNT#{credit_acc}"}, "SK": {"S": "BALANCE#BRL"}},
        )
        assert bal_c.get("Item"), "Balance da conta creditada deve existir"
        assert int(bal_c["Item"]["balance_amount"]["N"]) == -10000

    def test_idempotency_on_aws(self, aws_ledger_engine):
        """Submissão duplicada retorna o mesmo entry_id sem criar duplicata."""
        from ledger.domain.errors import IdempotencyConflict

        ext_id = str(uuid.uuid4())
        uid = _uid()
        cmd = CreateJournalEntryCommand(
            external_id=ext_id,
            postings=[
                PostingInput(f"aws-idem-d-{uid}", 1000, "BRL", "DEBIT"),
                PostingInput(f"aws-idem-c-{uid}", 1000, "BRL", "CREDIT"),
            ],
            metadata={},
        )

        first = aws_ledger_engine.create_journal_entry(cmd)

        with pytest.raises(IdempotencyConflict) as exc_info:
            aws_ledger_engine.create_journal_entry(cmd)

        assert exc_info.value.existing_entry_id == first.entry_id

    def test_zero_sum_violation_rejected(self, command_handler):
        """Postings desbalanceados retornam 400 ZERO_SUM_VIOLATION."""
        uid = _uid()
        body = {
            "external_id": str(uuid.uuid4()),
            "postings": [
                {"account_id": f"aws-zs-d-{uid}", "amount": 1000, "currency": "BRL", "direction": "DEBIT"},
                {"account_id": f"aws-zs-c-{uid}", "amount": 500, "currency": "BRL", "direction": "CREDIT"},
            ],
        }
        resp = handle_create_entry(_api_event(body), None, command_handler)
        assert resp["statusCode"] == 400
        assert _parse(resp)["error"]["code"] == "ZERO_SUM_VIOLATION"

    def test_reversal_creates_inverted_postings_on_aws(
        self, aws_ledger_engine, aws_repository
    ):
        """Reversão cria postings invertidos e zera saldos no DynamoDB real."""
        uid = _uid()
        debit_acc = f"aws-rev-d-{uid}"
        credit_acc = f"aws-rev-c-{uid}"
        amount = 7500

        # Cria original
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(debit_acc, amount, "BRL", "DEBIT"),
                PostingInput(credit_acc, amount, "BRL", "CREDIT"),
            ],
            metadata={},
        )
        original = aws_ledger_engine.create_journal_entry(cmd)

        # Cria reversão
        rev_cmd = CreateReversalCommand(
            original_entry_id=original.entry_id,
            external_id=str(uuid.uuid4()),
            metadata={"reason": "aws-e2e-test"},
        )
        reversal = aws_ledger_engine.create_reversal(rev_cmd)

        assert reversal.metadata["original_entry_id"] == original.entry_id

        # Saldos devem ser zero após hold + release
        bal_d = aws_repository.get_balance(debit_acc, "BRL")
        bal_c = aws_repository.get_balance(credit_acc, "BRL")
        assert bal_d.balance_amount == 0
        assert bal_c.balance_amount == 0


# ===========================================================================
# 2. Read Path — GET /balances e GET /statements contra DynamoDB real
# ===========================================================================


class TestReadPathAWS:
    """Testa o Read Path contra DynamoDB real."""

    def test_balance_reflects_write_on_aws(self, command_handler, query_handler):
        """Saldo materializado reflete o lançamento no DynamoDB real."""
        uid = _uid()
        debit_acc = f"aws-bal-d-{uid}"
        credit_acc = f"aws-bal-c-{uid}"
        amount = 3333

        body = _make_transfer(debit_acc, credit_acc, amount)
        r = handle_create_entry(_api_event(body), None, command_handler)
        assert r["statusCode"] == 201

        bal_resp = handle_get_balance(
            _balance_event(debit_acc, "BRL"), None, query_handler
        )
        assert bal_resp["statusCode"] == 200
        data = _parse(bal_resp)["data"]
        assert data["balance_amount"] == amount
        assert data["version"] == 1
        assert data["currency"] == "BRL"

    def test_statement_returns_postings_on_aws(self, command_handler, query_handler):
        """Extrato retorna postings corretos do DynamoDB real."""
        uid = _uid()
        acc = f"aws-stmt-{uid}"
        clr = f"aws-clr-{uid}"

        for i in range(3):
            body = _make_transfer(acc, clr, 1000 * (i + 1))
            handle_create_entry(_api_event(body), None, command_handler)

        stmt = _parse(handle_get_statement(
            _statement_event(acc), None, query_handler
        ))["data"]

        assert len(stmt["postings"]) == 3
        amounts = sorted(p["amount"] for p in stmt["postings"])
        assert amounts == [1000, 2000, 3000]

    def test_statement_pagination_on_aws(self, command_handler, query_handler):
        """Paginação de extrato funciona contra DynamoDB real."""
        uid = _uid()
        acc = f"aws-page-{uid}"
        clr = f"aws-clr-page-{uid}"

        for _ in range(5):
            body = _make_transfer(acc, clr, 500)
            handle_create_entry(_api_event(body), None, command_handler)

        all_postings = []
        cursor = None
        while True:
            page = _parse(handle_get_statement(
                _statement_event(acc, cursor=cursor, page_size=2), None, query_handler
            ))["data"]
            all_postings.extend(page["postings"])
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]

        assert len(all_postings) == 5

    def test_nonexistent_balance_returns_null_on_aws(self, query_handler):
        """Conta sem saldo retorna data: null."""
        resp = handle_get_balance(
            _balance_event(f"aws-ghost-{_uid()}", "BRL"), None, query_handler
        )
        assert resp["statusCode"] == 200
        assert _parse(resp)["data"] is None


# ===========================================================================
# 3. Publisher Lambda — invocação direta via AWS Lambda API
# ===========================================================================


class TestPublisherLambdaAWS:
    """Invoca a Publisher Lambda diretamente via AWS Lambda API."""

    def test_publisher_lambda_invocation(self, aws_ledger_engine, aws_dynamodb_client):
        """
        Cria um JournalEntry real, lê o OutboxEvent do DynamoDB,
        e invoca a Publisher Lambda diretamente com o evento do Stream.
        """
        uid = _uid()
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(f"aws-pub-d-{uid}", 2000, "BRL", "DEBIT"),
                PostingInput(f"aws-pub-c-{uid}", 2000, "BRL", "CREDIT"),
            ],
            metadata={},
        )
        entry = aws_ledger_engine.create_journal_entry(cmd)

        # Lê OutboxEvent real do DynamoDB
        outbox = aws_dynamodb_client.get_item(
            TableName=TABLE_NAME,
            Key={
                "PK": {"S": f"OUTBOX#{entry.entry_id}"},
                "SK": {"S": f"OUTBOX#{entry.entry_id}"},
            },
        )["Item"]

        # Monta evento do DynamoDB Stream
        stream_event = {
            "Records": [{
                "eventName": "INSERT",
                "dynamodb": {"NewImage": outbox},
            }]
        }

        # Invoca a Lambda real via AWS API
        lambda_client = boto3.client("lambda", region_name=AWS_REGION)
        response = lambda_client.invoke(
            FunctionName="double-entry-ledger-dev-publisher",
            InvocationType="RequestResponse",
            Payload=json.dumps(stream_event),
        )

        assert response["StatusCode"] == 200
        payload = json.loads(response["Payload"].read())

        # Publisher pode retornar erro de EventBridge (bus pode não ter regras ativas)
        # mas a Lambda deve executar sem crash (StatusCode 200)
        assert "FunctionError" not in response or response.get("FunctionError") is None, (
            f"Lambda Publisher retornou erro: {payload}"
        )
        assert "published" in payload or "failed" in payload, (
            f"Payload inesperado: {payload}"
        )

    def test_publisher_lambda_filters_non_outbox(self, aws_ledger_engine, aws_dynamodb_client):
        """Publisher Lambda ignora registros que não são OUTBOX#."""
        uid = _uid()
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(f"aws-filt-d-{uid}", 1000, "BRL", "DEBIT"),
                PostingInput(f"aws-filt-c-{uid}", 1000, "BRL", "CREDIT"),
            ],
            metadata={},
        )
        entry = aws_ledger_engine.create_journal_entry(cmd)

        # Usa o JournalEntry (não OUTBOX#) como evento
        journal = aws_dynamodb_client.get_item(
            TableName=TABLE_NAME,
            Key={
                "PK": {"S": f"JOURNAL#{entry.entry_id}"},
                "SK": {"S": f"JOURNAL#{entry.entry_id}"},
            },
        )["Item"]

        stream_event = {
            "Records": [{
                "eventName": "INSERT",
                "dynamodb": {"NewImage": journal},
            }]
        }

        lambda_client = boto3.client("lambda", region_name=AWS_REGION)
        response = lambda_client.invoke(
            FunctionName="double-entry-ledger-dev-publisher",
            InvocationType="RequestResponse",
            Payload=json.dumps(stream_event),
        )

        assert response["StatusCode"] == 200
        payload = json.loads(response["Payload"].read())
        assert payload.get("filtered", 0) == 1
        assert payload.get("published", 0) == 0


# ===========================================================================
# 4. Audit Transform Lambda — invocação direta via AWS Lambda API
# ===========================================================================


class TestAuditTransformLambdaAWS:
    """Invoca a Audit Transform Lambda diretamente via AWS Lambda API."""

    def test_audit_lambda_processes_journal_and_postings(
        self, aws_ledger_engine, aws_dynamodb_client
    ):
        """
        Cria um JournalEntry real, lê os itens do DynamoDB,
        e invoca a Audit Transform Lambda com os eventos do Stream.
        """
        uid = _uid()
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(f"aws-aud-d-{uid}", 4000, "BRL", "DEBIT"),
                PostingInput(f"aws-aud-c-{uid}", 4000, "BRL", "CREDIT"),
            ],
            metadata={"tenant_id": "tenant_aws_test"},
        )
        entry = aws_ledger_engine.create_journal_entry(cmd)

        # Lê JournalEntry e Postings do DynamoDB
        journal = aws_dynamodb_client.get_item(
            TableName=TABLE_NAME,
            Key={
                "PK": {"S": f"JOURNAL#{entry.entry_id}"},
                "SK": {"S": f"JOURNAL#{entry.entry_id}"},
            },
        )["Item"]

        postings_resp = aws_dynamodb_client.query(
            TableName=TABLE_NAME,
            IndexName="GSI-EntryPostings",
            KeyConditionExpression="entry_id_gsi = :eid",
            ExpressionAttributeValues={":eid": {"S": f"JOURNAL#{entry.entry_id}"}},
        )

        records = [{"eventName": "INSERT", "dynamodb": {"NewImage": journal}}]
        for item in postings_resp.get("Items", []):
            records.append({"eventName": "INSERT", "dynamodb": {"NewImage": item}})

        stream_event = {"Records": records}

        lambda_client = boto3.client("lambda", region_name=AWS_REGION)
        response = lambda_client.invoke(
            FunctionName="double-entry-ledger-dev-audit-transform",
            InvocationType="RequestResponse",
            Payload=json.dumps(stream_event),
        )

        assert response["StatusCode"] == 200
        payload = json.loads(response["Payload"].read())

        assert "FunctionError" not in response or response.get("FunctionError") is None, (
            f"Audit Lambda retornou erro: {payload}"
        )
        # 1 JOURNAL + 2 POSTING = 3 registros enviados ao Firehose
        assert payload.get("sent_to_firehose", 0) == 3, (
            f"Esperado 3 registros enviados ao Firehose, obtido: {payload}"
        )

    def test_audit_lambda_filters_balance_records(
        self, aws_ledger_engine, aws_dynamodb_client
    ):
        """Audit Lambda descarta registros BALANCE# corretamente."""
        uid = _uid()
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput(f"aws-bfilt-d-{uid}", 1000, "BRL", "DEBIT"),
                PostingInput(f"aws-bfilt-c-{uid}", 1000, "BRL", "CREDIT"),
            ],
            metadata={},
        )
        aws_ledger_engine.create_journal_entry(cmd)

        # Lê o Balance (deve ser filtrado)
        balance = aws_dynamodb_client.get_item(
            TableName=TABLE_NAME,
            Key={
                "PK": {"S": f"ACCOUNT#aws-bfilt-d-{uid}"},
                "SK": {"S": "BALANCE#BRL"},
            },
        )["Item"]

        stream_event = {
            "Records": [{"eventName": "INSERT", "dynamodb": {"NewImage": balance}}]
        }

        lambda_client = boto3.client("lambda", region_name=AWS_REGION)
        response = lambda_client.invoke(
            FunctionName="double-entry-ledger-dev-audit-transform",
            InvocationType="RequestResponse",
            Payload=json.dumps(stream_event),
        )

        assert response["StatusCode"] == 200
        payload = json.loads(response["Payload"].read())
        assert payload.get("sent_to_firehose", 0) == 0, (
            f"BALANCE# deve ser filtrado, mas {payload.get('sent_to_firehose')} registros foram enviados"
        )


# ===========================================================================
# 5. Full lifecycle via API handlers contra DynamoDB real
# ===========================================================================


class TestFullLifecycleAWS:
    """Ciclo de vida completo via API handlers contra DynamoDB real."""

    def test_create_read_reverse_verify_on_aws(self, command_handler, query_handler):
        """
        Ciclo completo: POST /entries → GET /balances → POST /reversals → verify.
        """
        uid = _uid()
        debit_acc = f"aws-lc-d-{uid}"
        credit_acc = f"aws-lc-c-{uid}"
        amount = 12000

        # Cria lançamento
        body = _make_transfer(debit_acc, credit_acc, amount)
        r = handle_create_entry(_api_event(body), None, command_handler)
        assert r["statusCode"] == 201
        entry_id = _parse(r)["data"]["entry_id"]

        # Verifica saldo
        bal = _parse(handle_get_balance(
            _balance_event(debit_acc, "BRL"), None, query_handler
        ))["data"]
        assert bal["balance_amount"] == amount

        # Cria reversão
        rev_body = {
            "original_entry_id": entry_id,
            "external_id": str(uuid.uuid4()),
            "metadata": {"reason": "aws-lifecycle-test"},
        }
        rev_r = handle_create_reversal(_api_event(rev_body), None, command_handler)
        assert rev_r["statusCode"] == 201
        rev_data = _parse(rev_r)["data"]
        assert rev_data["entry_type"] == "REVERSAL"
        assert rev_data["metadata"]["original_entry_id"] == entry_id

        # Saldos zerados
        bal_after = _parse(handle_get_balance(
            _balance_event(debit_acc, "BRL"), None, query_handler
        ))["data"]
        assert bal_after["balance_amount"] == 0

    def test_idempotency_via_api_on_aws(self, command_handler):
        """Idempotência via API handler contra DynamoDB real."""
        uid = _uid()
        ext_id = str(uuid.uuid4())
        body = _make_transfer(f"aws-idem2-d-{uid}", f"aws-idem2-c-{uid}", ext_id=ext_id)

        r1 = handle_create_entry(_api_event(body), None, command_handler)
        assert r1["statusCode"] == 201
        original_id = _parse(r1)["data"]["entry_id"]

        r2 = handle_create_entry(_api_event(body), None, command_handler)
        assert r2["statusCode"] == 200
        assert _parse(r2)["data"]["entry_id"] == original_id
        assert _parse(r2)["data"]["idempotent"] is True

    def test_hold_release_cycle_on_aws(self, command_handler, query_handler):
        """Hold/release completo contra DynamoDB real."""
        uid = _uid()
        avail = f"aws-avail-{uid}"
        hold = f"aws-hold-{uid}"
        amount = 20000

        # Hold
        handle_create_entry(
            _api_event(_make_transfer(avail, hold, amount, metadata={"op": "hold"})),
            None, command_handler,
        )

        bal_avail = _parse(handle_get_balance(
            _balance_event(avail, "BRL"), None, query_handler
        ))["data"]
        assert bal_avail["balance_amount"] == amount

        # Release
        handle_create_entry(
            _api_event(_make_transfer(hold, avail, amount, metadata={"op": "release"})),
            None, command_handler,
        )

        bal_after = _parse(handle_get_balance(
            _balance_event(avail, "BRL"), None, query_handler
        ))["data"]
        assert bal_after["balance_amount"] == 0
