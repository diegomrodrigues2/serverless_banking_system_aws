"""
Testes unitarios para a camada de infraestrutura do Double-Entry Ledger.

Cobre:
- DynamoDB Mapper: round-trip dominio -> DynamoDB -> dominio
- Publisher: publicacao de eventos validos e fallback para DLQ em caso de falha
- AuditTransformer: filtragem, deserializacao, enriquecimento e envio ao Firehose

Requisitos validados: 11.1, 11.2, 7.3, 7.4, 10.1, 10.3
"""
from __future__ import annotations

import json
import time
import uuid
from unittest.mock import MagicMock, call

import pytest

from ledger.domain.aggregates import JournalEntry
from ledger.domain.value_objects import (
    Balance,
    Direction,
    EntryType,
    Money,
    OutboxEvent,
    Posting,
)
from ledger.infrastructure.audit_exporter import AuditRecord, AuditTransformer
from ledger.infrastructure.dynamodb_mapper import (
    build_posting_sort_key,
    dynamo_item_to_balance,
    dynamo_item_to_journal_entry,
    dynamo_item_to_outbox_event,
    dynamo_item_to_posting,
    idempotency_record_to_dynamo_item,
    journal_entry_to_dynamo_item,
    outbox_event_to_dynamo_item,
    posting_to_dynamo_item,
)
from ledger.infrastructure.publisher import OutboxEventPublisher, _deserialize_outbox_event


# ---------------------------------------------------------------------------
# Helpers de construcao de fixtures
# ---------------------------------------------------------------------------


def _make_posting(
    account_id: str = "acc_available",
    amount: int = 1000,
    currency: str = "BRL",
    direction: Direction = Direction.DEBIT,
    index: int = 0,
) -> Posting:
    """Cria um Posting valido para uso nos testes."""
    return Posting(
        account_id=account_id,
        money=Money(amount=amount, currency=currency),
        direction=direction,
        index=index,
    )


def _make_outbox_event(entry_id: str | None = None) -> OutboxEvent:
    """Cria um OutboxEvent valido para uso nos testes."""
    eid = entry_id or str(uuid.uuid4())
    return OutboxEvent(
        event_id=f"OUTBOX#{eid}",
        entry_id=eid,
        event_type="TransactionCreated",
        payload={"entry_id": eid},
        expires_at=int(time.time()) + 86400,
    )


def _make_journal_entry(
    entry_id: str | None = None,
    external_id: str | None = None,
) -> JournalEntry:
    """Cria um JournalEntry valido com um par DEBIT/CREDIT para uso nos testes."""
    eid = entry_id or str(uuid.uuid4())
    return JournalEntry(
        entry_id=eid,
        external_id=external_id or str(uuid.uuid4()),
        entry_type=EntryType.STANDARD,
        postings=(
            _make_posting("acc_available", 1000, "BRL", Direction.DEBIT, 0),
            _make_posting("acc_hold", 1000, "BRL", Direction.CREDIT, 1),
        ),
        metadata={"tenant_id": "tenant_001"},
        timestamp="2026-03-10T14:30:00.000000Z",
        outbox_event=_make_outbox_event(eid),
    )


# ===========================================================================
# 1. DynamoDB Mapper -- round-trip dominio -> DynamoDB -> dominio
# ===========================================================================


@pytest.mark.unit
class TestDynamoDBMapperJournalEntry:
    """Testa o mapeamento bidirecional de JournalEntry."""

    def test_journal_entry_to_dynamo_item_has_correct_keys(self) -> None:
        """Item DynamoDB deve ter PK e SK no formato JOURNAL#{entry_id}."""
        entry = _make_journal_entry()
        item = journal_entry_to_dynamo_item(entry)

        assert item["PK"]["S"] == f"JOURNAL#{entry.entry_id}"
        assert item["SK"]["S"] == f"JOURNAL#{entry.entry_id}"

    def test_journal_entry_to_dynamo_item_preserves_fields(self) -> None:
        """Todos os campos do JournalEntry devem ser preservados no item DynamoDB."""
        entry = _make_journal_entry()
        item = journal_entry_to_dynamo_item(entry)

        assert item["entry_id"]["S"] == entry.entry_id
        assert item["external_id"]["S"] == entry.external_id
        assert item["entry_type"]["S"] == entry.entry_type.value
        assert item["timestamp"]["S"] == entry.timestamp
        assert json.loads(item["metadata"]["S"]) == entry.metadata

    def test_dynamo_item_to_journal_entry_round_trip(self) -> None:
        """Round-trip: JournalEntry -> DynamoDB item -> JournalEntry deve preservar dados."""
        original = _make_journal_entry()
        dynamo_item = journal_entry_to_dynamo_item(original)

        # Reconstroi o JournalEntry a partir do item DynamoDB
        postings = list(original.postings)
        outbox = original.outbox_event
        reconstructed = dynamo_item_to_journal_entry(dynamo_item, postings, outbox)

        assert reconstructed.entry_id == original.entry_id
        assert reconstructed.external_id == original.external_id
        assert reconstructed.entry_type == original.entry_type
        assert reconstructed.timestamp == original.timestamp
        assert reconstructed.metadata == original.metadata


@pytest.mark.unit
class TestDynamoDBMapperPosting:
    """Testa o mapeamento bidirecional de Posting."""

    def test_posting_to_dynamo_item_has_correct_keys(self) -> None:
        """Item DynamoDB deve ter PK = ACCOUNT#{account_id} e SK = POSTING#..."""
        posting = _make_posting("acc_available", 1000, "BRL", Direction.DEBIT, 0)
        entry_id = str(uuid.uuid4())
        timestamp = "2026-03-10T14:30:00.000000Z"

        item = posting_to_dynamo_item(posting, entry_id, timestamp)

        assert item["PK"]["S"] == "ACCOUNT#acc_available"
        assert item["SK"]["S"].startswith("POSTING#")
        assert entry_id in item["SK"]["S"]
        assert "#0" in item["SK"]["S"]

    def test_posting_to_dynamo_item_preserves_fields(self) -> None:
        """Todos os campos do Posting devem ser preservados no item DynamoDB."""
        posting = _make_posting("acc_hold", 2500, "USD", Direction.CREDIT, 3)
        entry_id = str(uuid.uuid4())
        timestamp = "2026-03-10T14:30:00.000000Z"

        item = posting_to_dynamo_item(posting, entry_id, timestamp)

        assert item["account_id"]["S"] == "acc_hold"
        assert int(item["amount"]["N"]) == 2500
        assert item["currency"]["S"] == "USD"
        assert item["direction"]["S"] == "CREDIT"
        assert int(item["posting_index"]["N"]) == 3
        assert item["entry_id"]["S"] == entry_id

    def test_dynamo_item_to_posting_round_trip(self) -> None:
        """Round-trip: Posting -> DynamoDB item -> Posting deve preservar dados."""
        original = _make_posting("acc_available", 1500, "BRL", Direction.DEBIT, 2)
        entry_id = str(uuid.uuid4())
        timestamp = "2026-03-10T14:30:00.000000Z"

        dynamo_item = posting_to_dynamo_item(original, entry_id, timestamp)
        reconstructed = dynamo_item_to_posting(dynamo_item)

        assert reconstructed.account_id == original.account_id
        assert reconstructed.money == original.money
        assert reconstructed.direction == original.direction
        assert reconstructed.index == original.index


@pytest.mark.unit
class TestDynamoDBMapperOutboxEvent:
    """Testa o mapeamento bidirecional de OutboxEvent."""

    def test_outbox_event_to_dynamo_item_has_correct_keys(self) -> None:
        """Item DynamoDB deve ter PK e SK no formato OUTBOX#{entry_id}."""
        event = _make_outbox_event()
        item = outbox_event_to_dynamo_item(event)

        assert item["PK"]["S"] == f"OUTBOX#{event.entry_id}"
        assert item["SK"]["S"] == f"OUTBOX#{event.entry_id}"

    def test_outbox_event_to_dynamo_item_preserves_fields(self) -> None:
        """Todos os campos do OutboxEvent devem ser preservados no item DynamoDB."""
        event = _make_outbox_event()
        item = outbox_event_to_dynamo_item(event)

        assert item["event_id"]["S"] == event.event_id
        assert item["entry_id"]["S"] == event.entry_id
        assert item["event_type"]["S"] == event.event_type
        assert json.loads(item["payload"]["S"]) == event.payload
        assert int(item["expires_at"]["N"]) == event.expires_at

    def test_dynamo_item_to_outbox_event_round_trip(self) -> None:
        """Round-trip: OutboxEvent -> DynamoDB item -> OutboxEvent deve preservar dados."""
        original = _make_outbox_event()
        dynamo_item = outbox_event_to_dynamo_item(original)
        reconstructed = dynamo_item_to_outbox_event(dynamo_item)

        assert reconstructed.event_id == original.event_id
        assert reconstructed.entry_id == original.entry_id
        assert reconstructed.event_type == original.event_type
        assert reconstructed.payload == original.payload
        assert reconstructed.expires_at == original.expires_at


@pytest.mark.unit
class TestDynamoDBMapperBalance:
    """Testa o mapeamento de Balance."""

    def test_dynamo_item_to_balance_parses_correctly(self) -> None:
        """Item DynamoDB de Balance deve ser convertido corretamente para Balance."""
        dynamo_item = {
            "PK": {"S": "ACCOUNT#acc_001"},
            "SK": {"S": "BALANCE#BRL"},
            "account_id": {"S": "acc_001"},
            "currency": {"S": "BRL"},
            "balance_amount": {"N": "5000"},
            "version": {"N": "3"},
            "last_update": {"S": "2026-03-10T14:30:00.000000Z"},
        }

        balance = dynamo_item_to_balance(dynamo_item)

        assert balance.account_id == "acc_001"
        assert balance.currency == "BRL"
        assert balance.balance_amount == 5000
        assert balance.version == 3
        assert balance.last_update == "2026-03-10T14:30:00.000000Z"


@pytest.mark.unit
class TestDynamoDBMapperIdempotency:
    """Testa o mapeamento do registro de idempotencia."""

    def test_idempotency_record_has_correct_keys(self) -> None:
        """Registro de idempotencia deve ter PK e SK no formato IDEMPOTENCY#{external_id}."""
        external_id = "order-payment-001"
        entry_id = str(uuid.uuid4())

        item = idempotency_record_to_dynamo_item(external_id, entry_id)

        assert item["PK"]["S"] == f"IDEMPOTENCY#{external_id}"
        assert item["SK"]["S"] == f"IDEMPOTENCY#{external_id}"
        assert item["external_id"]["S"] == external_id
        assert item["entry_id"]["S"] == entry_id


@pytest.mark.unit
class TestBuildPostingSortKey:
    """Testa a geracao do posting_sort_key."""

    def test_format_is_posting_hash_timestamp_hash_entry_id_hash_index(self) -> None:
        """posting_sort_key deve seguir o formato canonico."""
        timestamp = "2026-03-10T14:30:00.000000Z"
        entry_id = "550e8400-e29b-41d4-a716-446655440000"
        index = 0

        sort_key = build_posting_sort_key(timestamp, entry_id, index)

        assert sort_key == f"POSTING#{timestamp}#{entry_id}#{index}"

    def test_sort_key_starts_with_posting_prefix(self) -> None:
        """posting_sort_key deve comecar com 'POSTING#'."""
        sort_key = build_posting_sort_key("2026-03-10T00:00:00.000000Z", str(uuid.uuid4()), 5)
        assert sort_key.startswith("POSTING#")

    def test_sort_key_has_four_segments(self) -> None:
        """posting_sort_key deve ter exatamente 4 segmentos separados por '#'."""
        sort_key = build_posting_sort_key("2026-03-10T00:00:00.000000Z", str(uuid.uuid4()), 2)
        segments = sort_key.split("#")
        assert len(segments) == 4

    def test_chronological_ordering_preserved(self) -> None:
        """Timestamps anteriores devem gerar sort_keys menores (ordenacao cronologica)."""
        entry_id = str(uuid.uuid4())
        earlier = build_posting_sort_key("2026-01-01T00:00:00.000000Z", entry_id, 0)
        later = build_posting_sort_key("2026-12-31T23:59:59.999999Z", entry_id, 0)
        assert earlier < later


# ===========================================================================
# 2. Publisher -- publicacao de eventos e fallback para DLQ
# ===========================================================================


def _make_stream_record(
    pk: str,
    event_id: str,
    entry_id: str,
    event_type: str = "TransactionCreated",
    payload: dict | None = None,
    expires_at: int | None = None,
    event_name: str = "INSERT",
) -> dict:
    """Cria um registro de DynamoDB Stream no formato Lambda event."""
    return {
        "eventName": event_name,
        "dynamodb": {
            "NewImage": {
                "PK": {"S": pk},
                "SK": {"S": pk},
                "event_id": {"S": event_id},
                "entry_id": {"S": entry_id},
                "event_type": {"S": event_type},
                "payload": {"S": json.dumps(payload or {"entry_id": entry_id})},
                "expires_at": {"N": str(expires_at or int(time.time()) + 86400)},
            }
        },
    }


@pytest.mark.unit
class TestOutboxEventPublisher:
    """Testa o OutboxEventPublisher com mocks dos clients AWS."""

    def _make_publisher(
        self,
        eventbridge_mock: MagicMock | None = None,
        sqs_mock: MagicMock | None = None,
    ) -> OutboxEventPublisher:
        """Cria um OutboxEventPublisher com mocks injetados."""
        return OutboxEventPublisher(
            eventbridge_client=eventbridge_mock or MagicMock(),
            sqs_client=sqs_mock or MagicMock(),
            event_bus_name="test-event-bus",
            dlq_url="https://sqs.us-east-1.amazonaws.com/123456789/test-dlq",
        )

    def test_publishes_outbox_record_to_eventbridge(self) -> None:
        """Registro OUTBOX# valido deve ser publicado no EventBridge."""
        eventbridge_mock = MagicMock()
        publisher = self._make_publisher(eventbridge_mock=eventbridge_mock)

        entry_id = str(uuid.uuid4())
        record = _make_stream_record(
            pk=f"OUTBOX#{entry_id}",
            event_id=f"OUTBOX#{entry_id}",
            entry_id=entry_id,
            event_type="TransactionCreated",
        )

        result = publisher.process_stream_records([record])

        assert result["published"] == 1
        assert result["failed"] == 0
        assert result["filtered"] == 0
        eventbridge_mock.put_events.assert_called_once()

    def test_filters_non_outbox_records(self) -> None:
        """Registros sem prefixo OUTBOX# devem ser filtrados (nao publicados)."""
        eventbridge_mock = MagicMock()
        publisher = self._make_publisher(eventbridge_mock=eventbridge_mock)

        entry_id = str(uuid.uuid4())
        # Registro JOURNAL# -- nao deve ser publicado pelo Publisher
        record = _make_stream_record(
            pk=f"JOURNAL#{entry_id}",
            event_id=entry_id,
            entry_id=entry_id,
        )

        result = publisher.process_stream_records([record])

        assert result["published"] == 0
        assert result["filtered"] == 1
        eventbridge_mock.put_events.assert_not_called()

    def test_filters_remove_events(self) -> None:
        """Eventos REMOVE do DynamoDB Stream devem ser filtrados."""
        publisher = self._make_publisher()
        entry_id = str(uuid.uuid4())
        record = _make_stream_record(
            pk=f"OUTBOX#{entry_id}",
            event_id=f"OUTBOX#{entry_id}",
            entry_id=entry_id,
            event_name="REMOVE",
        )

        result = publisher.process_stream_records([record])

        assert result["filtered"] == 1
        assert result["published"] == 0

    def test_sends_to_dlq_on_eventbridge_failure(self) -> None:
        """Falha no EventBridge deve encaminhar o registro para a DLQ."""
        eventbridge_mock = MagicMock()
        sqs_mock = MagicMock()
        eventbridge_mock.put_events.side_effect = Exception("EventBridge unavailable")

        publisher = self._make_publisher(
            eventbridge_mock=eventbridge_mock,
            sqs_mock=sqs_mock,
        )

        entry_id = str(uuid.uuid4())
        record = _make_stream_record(
            pk=f"OUTBOX#{entry_id}",
            event_id=f"OUTBOX#{entry_id}",
            entry_id=entry_id,
        )

        result = publisher.process_stream_records([record])

        assert result["failed"] == 1
        assert result["published"] == 0
        sqs_mock.send_message.assert_called_once()

    def test_processes_mixed_batch(self) -> None:
        """Batch misto deve publicar apenas registros OUTBOX# validos."""
        eventbridge_mock = MagicMock()
        publisher = self._make_publisher(eventbridge_mock=eventbridge_mock)

        entry_id_1 = str(uuid.uuid4())
        entry_id_2 = str(uuid.uuid4())
        entry_id_3 = str(uuid.uuid4())

        records = [
            _make_stream_record(f"OUTBOX#{entry_id_1}", f"OUTBOX#{entry_id_1}", entry_id_1),
            _make_stream_record(f"JOURNAL#{entry_id_2}", entry_id_2, entry_id_2),  # filtrado
            _make_stream_record(f"OUTBOX#{entry_id_3}", f"OUTBOX#{entry_id_3}", entry_id_3),
        ]

        result = publisher.process_stream_records(records)

        assert result["published"] == 2
        assert result["filtered"] == 1
        assert result["failed"] == 0
        assert eventbridge_mock.put_events.call_count == 2

    def test_eventbridge_put_events_called_with_correct_params(self) -> None:
        """PutEvents deve ser chamado com Source, DetailType e EventBusName corretos."""
        eventbridge_mock = MagicMock()
        publisher = self._make_publisher(eventbridge_mock=eventbridge_mock)

        entry_id = str(uuid.uuid4())
        payload = {"entry_id": entry_id, "external_id": "order-001"}
        record = _make_stream_record(
            pk=f"OUTBOX#{entry_id}",
            event_id=f"OUTBOX#{entry_id}",
            entry_id=entry_id,
            event_type="TransactionCreated",
            payload=payload,
        )

        publisher.process_stream_records([record])

        call_args = eventbridge_mock.put_events.call_args
        entries = call_args.kwargs["Entries"]
        assert len(entries) == 1
        assert entries[0]["Source"] == "ledger.subledger"
        assert entries[0]["DetailType"] == "TransactionCreated"
        assert entries[0]["EventBusName"] == "test-event-bus"
        assert json.loads(entries[0]["Detail"]) == payload


@pytest.mark.unit
class TestDeserializeOutboxEvent:
    """Testa a deserializacao de OutboxEvent do formato DynamoDB JSON."""

    def test_deserializes_valid_new_image(self) -> None:
        """NewImage valido deve ser deserializado corretamente."""
        entry_id = str(uuid.uuid4())
        payload = {"entry_id": entry_id}
        expires_at = int(time.time()) + 86400

        new_image = {
            "PK": {"S": f"OUTBOX#{entry_id}"},
            "SK": {"S": f"OUTBOX#{entry_id}"},
            "event_id": {"S": f"OUTBOX#{entry_id}"},
            "entry_id": {"S": entry_id},
            "event_type": {"S": "TransactionCreated"},
            "payload": {"S": json.dumps(payload)},
            "expires_at": {"N": str(expires_at)},
        }

        result = _deserialize_outbox_event(new_image)

        assert result["event_id"] == f"OUTBOX#{entry_id}"
        assert result["entry_id"] == entry_id
        assert result["event_type"] == "TransactionCreated"
        assert result["payload"] == payload
        assert result["expires_at"] == expires_at


# ===========================================================================
# 3. AuditTransformer -- filtragem, deserializacao, enriquecimento e Firehose
# ===========================================================================


def _make_journal_stream_record(
    entry_id: str | None = None,
    external_id: str | None = None,
    timestamp: str = "2026-03-10T14:30:00.000000Z",
    metadata: dict | None = None,
) -> dict:
    """Cria um registro de DynamoDB Stream para um item JOURNAL#."""
    eid = entry_id or str(uuid.uuid4())
    meta = metadata or {"tenant_id": "tenant_001"}
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "PK": {"S": f"JOURNAL#{eid}"},
                "SK": {"S": f"JOURNAL#{eid}"},
                "entry_id": {"S": eid},
                "external_id": {"S": external_id or str(uuid.uuid4())},
                "entry_type": {"S": "STANDARD"},
                "timestamp": {"S": timestamp},
                "metadata": {"S": json.dumps(meta)},
            }
        },
    }


def _make_posting_stream_record(
    entry_id: str | None = None,
    account_id: str = "acc_available",
    amount: int = 1000,
    currency: str = "BRL",
    direction: str = "DEBIT",
    posting_index: int = 0,
    timestamp: str = "2026-03-10T14:30:00.000000Z",
) -> dict:
    """Cria um registro de DynamoDB Stream para um item POSTING#."""
    eid = entry_id or str(uuid.uuid4())
    sort_key = build_posting_sort_key(timestamp, eid, posting_index)
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "PK": {"S": f"ACCOUNT#{account_id}"},
                "SK": {"S": sort_key},
                "entry_id": {"S": eid},
                "account_id": {"S": account_id},
                "amount": {"N": str(amount)},
                "currency": {"S": currency},
                "direction": {"S": direction},
                "posting_index": {"N": str(posting_index)},
                "timestamp": {"S": timestamp},
            }
        },
    }


def _make_balance_stream_record(account_id: str = "acc_available") -> dict:
    """Cria um registro de DynamoDB Stream para um item BALANCE# (deve ser descartado)."""
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "PK": {"S": f"ACCOUNT#{account_id}"},
                "SK": {"S": "BALANCE#BRL"},
                "account_id": {"S": account_id},
                "currency": {"S": "BRL"},
                "balance_amount": {"N": "5000"},
                "version": {"N": "1"},
                "last_update": {"S": "2026-03-10T14:30:00.000000Z"},
            }
        },
    }


@pytest.mark.unit
class TestAuditTransformerFiltering:
    """Testa a filtragem de registros do DynamoDB Stream pelo AuditTransformer."""

    def _make_transformer(self, firehose_mock: MagicMock | None = None) -> AuditTransformer:
        """Cria um AuditTransformer com mock do Firehose injetado."""
        mock = firehose_mock or MagicMock()
        mock.put_record_batch.return_value = {"FailedPutCount": 0, "RequestResponses": []}
        return AuditTransformer(
            firehose_stream_name="test-audit-stream",
            firehose_client=mock,
        )

    def test_journal_record_is_included(self) -> None:
        """Registros JOURNAL# devem ser incluidos na auditoria."""
        firehose_mock = MagicMock()
        firehose_mock.put_record_batch.return_value = {
            "FailedPutCount": 0,
            "RequestResponses": [{}],
        }
        transformer = self._make_transformer(firehose_mock)

        records = [_make_journal_stream_record()]
        sent = transformer.process_stream_records(records)

        assert sent == 1
        firehose_mock.put_record_batch.assert_called_once()

    def test_posting_record_is_included(self) -> None:
        """Registros POSTING# devem ser incluidos na auditoria."""
        firehose_mock = MagicMock()
        firehose_mock.put_record_batch.return_value = {
            "FailedPutCount": 0,
            "RequestResponses": [{}],
        }
        transformer = self._make_transformer(firehose_mock)

        records = [_make_posting_stream_record()]
        sent = transformer.process_stream_records(records)

        assert sent == 1

    def test_balance_record_is_discarded(self) -> None:
        """Registros BALANCE# devem ser descartados (nao relevantes para auditoria)."""
        firehose_mock = MagicMock()
        transformer = self._make_transformer(firehose_mock)

        records = [_make_balance_stream_record()]
        sent = transformer.process_stream_records(records)

        assert sent == 0
        firehose_mock.put_record_batch.assert_not_called()

    def test_mixed_batch_filters_correctly(self) -> None:
        """Batch misto deve incluir apenas JOURNAL# e POSTING#, descartar BALANCE#."""
        firehose_mock = MagicMock()
        firehose_mock.put_record_batch.return_value = {
            "FailedPutCount": 0,
            "RequestResponses": [{}, {}],
        }
        transformer = self._make_transformer(firehose_mock)

        records = [
            _make_journal_stream_record(),   # incluido
            _make_posting_stream_record(),   # incluido
            _make_balance_stream_record(),   # descartado
        ]
        sent = transformer.process_stream_records(records)

        assert sent == 2

    def test_modify_events_are_filtered_out(self) -> None:
        """Eventos MODIFY do DynamoDB Stream devem ser filtrados (apenas INSERT)."""
        firehose_mock = MagicMock()
        transformer = self._make_transformer(firehose_mock)

        entry_id = str(uuid.uuid4())
        record = _make_journal_stream_record(entry_id=entry_id)
        record["eventName"] = "MODIFY"  # Altera para MODIFY

        sent = transformer.process_stream_records([record])

        assert sent == 0
        firehose_mock.put_record_batch.assert_not_called()


@pytest.mark.unit
class TestAuditTransformerDeserialization:
    """Testa a deserializacao de registros DynamoDB para AuditRecord."""

    def _make_transformer(self) -> AuditTransformer:
        mock = MagicMock()
        mock.put_record_batch.return_value = {"FailedPutCount": 0, "RequestResponses": [{}]}
        return AuditTransformer("test-stream", mock)

    def test_journal_entry_audit_record_has_correct_fields(self) -> None:
        """AuditRecord de JournalEntry deve ter record_type='JOURNAL_ENTRY' e campos corretos."""
        entry_id = str(uuid.uuid4())
        external_id = "order-001"
        timestamp = "2026-03-10T14:30:00.000000Z"
        metadata = {"tenant_id": "tenant_abc"}

        firehose_mock = MagicMock()
        firehose_mock.put_record_batch.return_value = {
            "FailedPutCount": 0,
            "RequestResponses": [{}],
        }
        transformer = AuditTransformer("test-stream", firehose_mock)

        records = [_make_journal_stream_record(
            entry_id=entry_id,
            external_id=external_id,
            timestamp=timestamp,
            metadata=metadata,
        )]
        transformer.process_stream_records(records)

        # Verifica o payload enviado ao Firehose
        call_args = firehose_mock.put_record_batch.call_args
        firehose_records = call_args.kwargs["Records"]
        assert len(firehose_records) == 1

        audit_data = json.loads(firehose_records[0]["Data"].decode("utf-8").strip())
        assert audit_data["record_type"] == "JOURNAL_ENTRY"
        assert audit_data["entry_id"] == entry_id
        assert audit_data["external_id"] == external_id
        assert audit_data["entry_type"] == "STANDARD"
        assert audit_data["account_id"] is None
        assert audit_data["amount"] is None
        assert audit_data["direction"] is None

    def test_posting_audit_record_has_correct_fields(self) -> None:
        """AuditRecord de Posting deve ter record_type='POSTING' e todos os campos."""
        entry_id = str(uuid.uuid4())
        timestamp = "2026-03-10T14:30:00.000000Z"

        firehose_mock = MagicMock()
        firehose_mock.put_record_batch.return_value = {
            "FailedPutCount": 0,
            "RequestResponses": [{}],
        }
        transformer = AuditTransformer("test-stream", firehose_mock)

        records = [_make_posting_stream_record(
            entry_id=entry_id,
            account_id="acc_available",
            amount=1500,
            currency="BRL",
            direction="DEBIT",
            posting_index=0,
            timestamp=timestamp,
        )]
        transformer.process_stream_records(records)

        call_args = firehose_mock.put_record_batch.call_args
        firehose_records = call_args.kwargs["Records"]
        audit_data = json.loads(firehose_records[0]["Data"].decode("utf-8").strip())

        assert audit_data["record_type"] == "POSTING"
        assert audit_data["entry_id"] == entry_id
        assert audit_data["account_id"] == "acc_available"
        assert audit_data["amount"] == 1500
        assert audit_data["currency"] == "BRL"
        assert audit_data["direction"] == "DEBIT"
        assert audit_data["posting_index"] == 0

    def test_partition_fields_extracted_from_timestamp(self) -> None:
        """Campos year, month, day devem ser extraidos corretamente do timestamp."""
        firehose_mock = MagicMock()
        firehose_mock.put_record_batch.return_value = {
            "FailedPutCount": 0,
            "RequestResponses": [{}],
        }
        transformer = AuditTransformer("test-stream", firehose_mock)

        records = [_make_journal_stream_record(timestamp="2026-07-15T10:00:00.000000Z")]
        transformer.process_stream_records(records)

        call_args = firehose_mock.put_record_batch.call_args
        firehose_records = call_args.kwargs["Records"]
        audit_data = json.loads(firehose_records[0]["Data"].decode("utf-8").strip())

        assert audit_data["year"] == "2026"
        assert audit_data["month"] == "07"
        assert audit_data["day"] == "15"


@pytest.mark.unit
class TestAuditTransformerFirehose:
    """Testa o envio de AuditRecords para o Firehose via PutRecordBatch."""

    def test_put_record_batch_called_with_correct_stream_name(self) -> None:
        """PutRecordBatch deve ser chamado com o nome correto do delivery stream."""
        firehose_mock = MagicMock()
        firehose_mock.put_record_batch.return_value = {
            "FailedPutCount": 0,
            "RequestResponses": [{}],
        }
        transformer = AuditTransformer("my-audit-firehose", firehose_mock)

        records = [_make_journal_stream_record()]
        transformer.process_stream_records(records)

        call_args = firehose_mock.put_record_batch.call_args
        assert call_args.kwargs["DeliveryStreamName"] == "my-audit-firehose"

    def test_records_serialized_as_ndjson(self) -> None:
        """Cada AuditRecord deve ser serializado como JSON com newline (NDJSON)."""
        firehose_mock = MagicMock()
        firehose_mock.put_record_batch.return_value = {
            "FailedPutCount": 0,
            "RequestResponses": [{}],
        }
        transformer = AuditTransformer("test-stream", firehose_mock)

        records = [_make_journal_stream_record()]
        transformer.process_stream_records(records)

        call_args = firehose_mock.put_record_batch.call_args
        firehose_records = call_args.kwargs["Records"]
        raw_data = firehose_records[0]["Data"]

        # Deve ser bytes terminando com newline
        assert isinstance(raw_data, bytes)
        assert raw_data.endswith(b"\n")

        # Deve ser JSON valido
        json.loads(raw_data.decode("utf-8").strip())

    def test_empty_batch_does_not_call_firehose(self) -> None:
        """Batch vazio nao deve chamar o Firehose."""
        firehose_mock = MagicMock()
        transformer = AuditTransformer("test-stream", firehose_mock)

        sent = transformer.process_stream_records([])

        assert sent == 0
        firehose_mock.put_record_batch.assert_not_called()
