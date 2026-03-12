"""
Lambda handler do Audit Pipeline do Double-Entry Ledger.

Ponto de entrada da funcao Lambda que consome DynamoDB Streams e
delega o processamento para AuditTransformer (audit_exporter.py).

Fluxo:
    DynamoDB Streams (NEW_IMAGE, filtro JOURNAL# e ACCOUNT#)
        -> Audit Transform Lambda (este handler)
            -> AuditTransformer.process_stream_records()
                -> Kinesis Data Firehose PutRecordBatch (sucesso)
                -> Audit DLQ SQS (falha critica)

Em caso de falha critica (excecao nao tratada), a Lambda retorna erro
e o DynamoDB Streams reprocessa o batch automaticamente. A DLQ e usada
apenas para falhas persistentes apos todas as retentativas.

Requisitos atendidos: 10.1, 10.2, 10.3
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Nome do Firehose delivery stream -- configurado via variavel de ambiente
_FIREHOSE_STREAM_NAME = os.environ.get("AUDIT_FIREHOSE_STREAM_NAME", "ledger-audit")

# URL da DLQ SQS para falhas criticas -- configurado via variavel de ambiente
_AUDIT_DLQ_URL = os.environ.get("AUDIT_DLQ_URL", "")


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    """
    Ponto de entrada da Lambda do Audit Pipeline.

    Recebe o evento do DynamoDB Stream e delega para AuditTransformer.
    Os clients boto3 sao criados aqui para reutilizacao entre invocacoes
    (warm start do Lambda).

    Em caso de falha critica (excecao nao tratada), encaminha o batch
    completo para a Audit DLQ antes de propagar o erro.

    Args:
        event:   Evento do DynamoDB Stream com lista de records.
        context: Contexto de execucao da Lambda (nao utilizado).

    Returns:
        Dict com numero de registros enviados ao Firehose.
    """
    import boto3

    from ledger.infrastructure.audit_exporter import AuditTransformer

    firehose_client = boto3.client("firehose")
    sqs_client = boto3.client("sqs")

    transformer = AuditTransformer(
        firehose_stream_name=_FIREHOSE_STREAM_NAME,
        firehose_client=firehose_client,
    )

    records = event.get("Records", [])
    logger.info(
        "Audit Transform Lambda invocada",
        extra={
            "operation": "handler",
            "record_count": len(records),
        },
    )

    try:
        sent_count = transformer.process_stream_records(records)
        return {"sent_to_firehose": sent_count}

    except Exception as exc:
        # Falha critica -- encaminha batch para Audit DLQ antes de propagar
        logger.error(
            "falha critica no Audit Pipeline -- encaminhando batch para DLQ",
            extra={
                "operation": "handler",
                "result": "critical_error",
                "error": str(exc),
                "record_count": len(records),
            },
        )
        _send_batch_to_dlq(sqs_client, records, str(exc))
        # Propaga o erro para que o DynamoDB Streams reprocesse o batch
        raise


def _send_batch_to_dlq(
    sqs_client: Any,
    records: list[dict[str, Any]],
    error_message: str,
) -> None:
    """
    Encaminha o batch completo para a Audit DLQ em caso de falha critica.

    A Audit DLQ e separada da DLQ do Publisher para evitar que falhas
    no pipeline de auditoria bloqueiem a publicacao de eventos de negocio.

    Args:
        sqs_client:    Client boto3 SQS.
        records:       Batch de registros do DynamoDB Stream.
        error_message: Mensagem de erro que causou a falha critica.
    """
    import json

    if not _AUDIT_DLQ_URL:
        logger.warning(
            "Audit DLQ nao configurada -- batch com falha descartado",
            extra={"operation": "send_batch_to_dlq", "result": "skipped"},
        )
        return

    try:
        sqs_client.send_message(
            QueueUrl=_AUDIT_DLQ_URL,
            MessageBody=json.dumps({
                "records": records,
                "error": error_message,
            }),
        )
        logger.info(
            "batch encaminhado para Audit DLQ",
            extra={
                "operation": "send_batch_to_dlq",
                "result": "success",
                "record_count": len(records),
            },
        )
    except Exception as dlq_exc:
        # Falha na DLQ -- loga mas nao propaga (o erro original ja sera propagado)
        logger.error(
            "falha ao encaminhar batch para Audit DLQ",
            extra={
                "operation": "send_batch_to_dlq",
                "result": "error",
                "error": str(dlq_exc),
            },
        )
