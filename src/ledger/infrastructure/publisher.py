"""Lambda Publisher do Double-Entry Ledger -- GoF Observer.

Consome DynamoDB Streams (filtro OUTBOX#) e publica no EventBridge.
Em caso de falha, encaminha para DLQ (SQS).

Requisitos atendidos: 7.3, 7.4, 7.5
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "ledger-events")
_DLQ_URL = os.environ.get("PUBLISHER_DLQ_URL", "")
_EVENT_SOURCE = "ledger.subledger"


class OutboxEventPublisher:
    """GoF Observer -- publica OutboxEvents do DynamoDB Stream no EventBridge."""

    def __init__(self, eventbridge_client, sqs_client, event_bus_name=_EVENT_BUS_NAME, dlq_url=_DLQ_URL):
        self._eventbridge = eventbridge_client
        self._sqs = sqs_client
        self._event_bus_name = event_bus_name
        self._dlq_url = dlq_url

    def process_stream_records(self, records):
        """Processa batch de registros do DynamoDB Stream."""
        published = 0
        failed = 0
        filtered = 0
        for record in records:
            event_name = record.get("eventName", "")
            if event_name not in ("INSERT", "MODIFY"):
                filtered += 1
                continue
            new_image = record.get("dynamodb", {}).get("NewImage", {})
            if not new_image:
                filtered += 1
                continue
            pk_value = new_image.get("PK", {}).get("S", "")
            if not pk_value.startswith("OUTBOX#"):
                filtered += 1
                continue
            try:
                outbox_event = _deserialize_outbox_event(new_image)
                self._publish_to_eventbridge(outbox_event)
                published += 1
                logger.info("evento publicado no EventBridge", extra={"event_id": outbox_event["event_id"], "entry_id": outbox_event["entry_id"], "event_type": outbox_event["event_type"], "operation": "publish_event", "result": "success"})
            except Exception as exc:
                failed += 1
                logger.error("falha ao publicar evento", extra={"pk": pk_value, "operation": "publish_event", "result": "error", "error": str(exc)})
                self._send_to_dlq(record, str(exc))
        logger.info("batch processado", extra={"operation": "process_stream_records", "published": published, "failed": failed, "filtered": filtered, "total": len(records)})
        return {"published": published, "failed": failed, "filtered": filtered}

    def _publish_to_eventbridge(self, outbox_event):
        """Publica OutboxEvent no EventBridge via PutEvents."""
        self._eventbridge.put_events(Entries=[{"Source": _EVENT_SOURCE, "DetailType": outbox_event["event_type"], "Detail": json.dumps(outbox_event["payload"]), "EventBusName": self._event_bus_name}])

    def _send_to_dlq(self, original_record, error_message):
        """Encaminha registro com falha para a DLQ (SQS)."""
        if not self._dlq_url:
            logger.warning("DLQ nao configurada", extra={"operation": "send_to_dlq", "result": "skipped"})
            return
        try:
            self._sqs.send_message(QueueUrl=self._dlq_url, MessageBody=json.dumps({"original_record": original_record, "error": error_message}))
            logger.info("registro encaminhado para DLQ", extra={"operation": "send_to_dlq", "result": "success"})
        except Exception as dlq_exc:
            logger.error("falha ao encaminhar para DLQ", extra={"operation": "send_to_dlq", "result": "error", "error": str(dlq_exc)})


def handler(event, context):
    """Ponto de entrada da Lambda Publisher."""
    import boto3
    publisher = OutboxEventPublisher(eventbridge_client=boto3.client("events"), sqs_client=boto3.client("sqs"), event_bus_name=_EVENT_BUS_NAME, dlq_url=_DLQ_URL)
    records = event.get("Records", [])
    logger.info("Lambda Publisher invocada", extra={"operation": "handler", "record_count": len(records)})
    return publisher.process_stream_records(records)


def _deserialize_outbox_event(new_image):
    """Deserializa OutboxEvent do formato DynamoDB JSON (NewImage) para dict Python."""
    payload = json.loads(new_image["payload"]["S"])
    return {"event_id": new_image["event_id"]["S"], "entry_id": new_image["entry_id"]["S"], "event_type": new_image["event_type"]["S"], "payload": payload, "expires_at": int(new_image["expires_at"]["N"])}
