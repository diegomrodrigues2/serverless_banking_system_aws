"""
AuditTransformer do Double-Entry Ledger.

Lambda leve que consome DynamoDB Streams (filtro JOURNAL# e ACCOUNT#) e
alimenta o Kinesis Data Firehose com registros contabeis normalizados.

Responsabilidades (apenas transformacao -- NAO faz escrita no S3):
1. Recebe batch de registros do DynamoDB Stream
2. Filtra: descarta itens com SK comecando em BALANCE# (nao relevantes para auditoria)
3. Deserializa DynamoDB JSON -> schema flat de auditoria (AuditRecord)
4. Enriquece com campos de particionamento (year, month, day, tenant_id)
5. Envia para Firehose via PutRecordBatch

O Firehose cuida de:
- Batching (buffer de 128MB ou 60s)
- Conversao JSON -> Parquet (via Glue Table schema)
- Particionamento dinamico no S3 (year/month/day/tenant)
- Compressao (Snappy)
- Retry e entrega garantida
- Error handling (backup em error bucket)

Idempotencia:
- DynamoDB Streams e at-least-once; registros duplicados podem chegar
- Firehose aceita duplicatas sem efeito colateral (append-only no S3)
- Para analytics, deduplicacao e feita no query time via entry_id + posting_index

Requisitos atendidos: 10.1, 10.2, 10.3
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Prefixos de SK que devem ser incluidos na auditoria
_AUDIT_SK_PREFIXES = ("JOURNAL#", "POSTING#")

# Prefixo de SK que deve ser descartado (saldos nao sao dados de auditoria)
_BALANCE_SK_PREFIX = "BALANCE#"

# Tamanho maximo do batch para PutRecordBatch do Firehose (limite AWS: 500 registros)
_FIREHOSE_MAX_BATCH_SIZE = 500


@dataclass(frozen=True)
class AuditRecord:
    """
    Schema flat para registros de auditoria no Firehose/Parquet.

    Representa um registro de auditoria normalizado, derivado de um
    JournalEntry (record_type="JOURNAL_ENTRY") ou de um Posting
    (record_type="POSTING").

    Campos de particionamento (year, month, day, tenant_id) sao extraidos
    do timestamp e usados pelo Firehose para Dynamic Partitioning no S3.

    Campos opcionais (account_id, amount, direction, currency, posting_index)
    sao preenchidos apenas para registros do tipo POSTING.
    """

    record_type: str            # "JOURNAL_ENTRY" | "POSTING"
    entry_id: str               # UUID do JournalEntry
    external_id: str            # Chave de idempotencia
    entry_type: str             # "STANDARD" | "REVERSAL"
    account_id: str | None      # Presente apenas para POSTING
    amount: int | None          # Minor units, presente apenas para POSTING
    direction: str | None       # "DEBIT" | "CREDIT", presente apenas para POSTING
    currency: str | None        # ISO 4217, presente apenas para POSTING
    posting_index: int | None   # Indice ordinal, presente apenas para POSTING
    tenant_id: str              # Para particionamento dinamico
    timestamp: str              # ISO 8601 do fato contabil
    metadata: str               # JSON serializado dos metadados do JournalEntry
    # Campos de particionamento (extraidos do timestamp)
    year: str
    month: str
    day: str


class AuditTransformer:
    """
    Transforma registros DynamoDB Stream em AuditRecords para Firehose.

    Encapsula a logica de transformacao para facilitar testes unitarios
    via injecao de dependencia do client Firehose.
    """

    def __init__(self, firehose_stream_name: str, firehose_client: Any) -> None:
        """
        Inicializa o transformer com o client Firehose e o nome do stream.

        Args:
            firehose_stream_name: Nome do Kinesis Data Firehose delivery stream.
            firehose_client:      Client boto3 Firehose (ou mock).
        """
        self._firehose_stream_name = firehose_stream_name
        self._firehose = firehose_client

    def process_stream_records(self, records: list[dict[str, Any]]) -> int:
        """
        Processa um batch de registros do DynamoDB Stream.

        Algoritmo:
        1. Filtra registros relevantes (JOURNAL# e POSTING#, descarta BALANCE#)
        2. Converte cada registro para AuditRecord
        3. Envia batch para Firehose via PutRecordBatch
        4. Retorna numero de registros enviados

        Args:
            records: Lista de registros do DynamoDB Stream no formato Lambda event.

        Returns:
            Numero de registros enviados ao Firehose.
        """
        # Filtra apenas registros INSERT com NewImage relevante
        relevant_records = self._filter_audit_records(records)

        if not relevant_records:
            logger.info(
                "nenhum registro relevante para auditoria",
                extra={
                    "operation": "process_stream_records",
                    "total_received": len(records),
                    "filtered_out": len(records),
                    "sent_to_firehose": 0,
                },
            )
            return 0

        # Converte para AuditRecords
        audit_records: list[AuditRecord] = []
        entry_ids_processed: list[str] = []

        for dynamo_record in relevant_records:
            new_image = dynamo_record.get("dynamodb", {}).get("NewImage", {})
            try:
                audit_record = self._to_audit_record(new_image)
                audit_records.append(audit_record)
                entry_ids_processed.append(audit_record.entry_id)
            except Exception as exc:
                logger.error(
                    "falha ao converter registro para AuditRecord",
                    extra={
                        "operation": "to_audit_record",
                        "result": "error",
                        "error": str(exc),
                        "pk": new_image.get("PK", {}).get("S", "unknown"),
                    },
                )

        if not audit_records:
            return 0

        # Envia para Firehose em batches de ate _FIREHOSE_MAX_BATCH_SIZE
        total_sent = self._send_to_firehose(audit_records)

        logger.info(
            "registros de auditoria enviados ao Firehose",
            extra={
                "operation": "process_stream_records",
                "total_received": len(records),
                "filtered_out": len(records) - len(relevant_records),
                "sent_to_firehose": total_sent,
                "entry_ids": list(set(entry_ids_processed)),
            },
        )

        return total_sent

    def _filter_audit_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Filtra registros relevantes para auditoria.

        Criterios de inclusao:
        - eventName == "INSERT" (apenas novos registros)
        - NewImage presente
        - SK comeca com JOURNAL# ou POSTING# (descarta BALANCE#, OUTBOX#, IDEMPOTENCY#)

        Args:
            records: Lista de registros do DynamoDB Stream.

        Returns:
            Lista de registros filtrados com NewImage relevante.
        """
        relevant = []
        for record in records:
            # Processa apenas eventos INSERT -- registros sao imutaveis (append-only)
            if record.get("eventName") != "INSERT":
                continue

            new_image = record.get("dynamodb", {}).get("NewImage", {})
            if not new_image:
                continue

            sk_value = new_image.get("SK", {}).get("S", "")

            # Inclui apenas JOURNAL# e POSTING# -- descarta BALANCE#, OUTBOX#, IDEMPOTENCY#
            if any(sk_value.startswith(prefix) for prefix in _AUDIT_SK_PREFIXES):
                relevant.append(record)

        return relevant

    def _to_audit_record(self, new_image: dict[str, Any]) -> AuditRecord:
        """
        Deserializa um item DynamoDB (NewImage) para AuditRecord flat.

        Determina o record_type pelo prefixo do SK:
        - SK comeca com JOURNAL# -> record_type = "JOURNAL_ENTRY"
        - SK comeca com POSTING# -> record_type = "POSTING"

        Extrai campos de particionamento (year, month, day) do timestamp ISO 8601.

        Args:
            new_image: NewImage do registro DynamoDB Stream no formato tipado.

        Returns:
            AuditRecord com todos os campos preenchidos.

        Raises:
            KeyError:   Se campos obrigatorios estiverem ausentes.
            ValueError: Se o timestamp nao estiver no formato esperado.
        """
        sk_value = new_image.get("SK", {}).get("S", "")
        timestamp = new_image.get("timestamp", {}).get("S", "")

        # Extrai campos de particionamento do timestamp ISO 8601
        # Formato esperado: YYYY-MM-DDTHH:MM:SS.ffffffZ
        year, month, day = _extract_partition_fields(timestamp)

        if sk_value.startswith("JOURNAL#"):
            return self._journal_entry_to_audit_record(new_image, year, month, day)
        elif sk_value.startswith("POSTING#"):
            return self._posting_to_audit_record(new_image, year, month, day)
        else:
            raise ValueError(f"SK nao reconhecido para auditoria: {sk_value}")

    def _journal_entry_to_audit_record(
        self,
        new_image: dict[str, Any],
        year: str,
        month: str,
        day: str,
    ) -> AuditRecord:
        """
        Converte um item JOURNAL# do DynamoDB para AuditRecord.

        Campos opcionais (account_id, amount, direction, currency, posting_index)
        sao None para registros de JournalEntry.

        Args:
            new_image: NewImage do item JOURNAL# do DynamoDB Stream.
            year:      Ano extraido do timestamp para particionamento.
            month:     Mes extraido do timestamp para particionamento.
            day:       Dia extraido do timestamp para particionamento.

        Returns:
            AuditRecord com record_type="JOURNAL_ENTRY".
        """
        metadata_str = new_image.get("metadata", {}).get("S", "{}")
        metadata_dict = json.loads(metadata_str)

        # tenant_id extraido do metadata (se disponivel) ou do entry_id como fallback
        tenant_id = metadata_dict.get("tenant_id", "unknown")

        return AuditRecord(
            record_type="JOURNAL_ENTRY",
            entry_id=new_image["entry_id"]["S"],
            external_id=new_image["external_id"]["S"],
            entry_type=new_image["entry_type"]["S"],
            account_id=None,
            amount=None,
            direction=None,
            currency=None,
            posting_index=None,
            tenant_id=tenant_id,
            timestamp=new_image["timestamp"]["S"],
            metadata=metadata_str,
            year=year,
            month=month,
            day=day,
        )

    def _posting_to_audit_record(
        self,
        new_image: dict[str, Any],
        year: str,
        month: str,
        day: str,
    ) -> AuditRecord:
        """
        Converte um item POSTING# do DynamoDB para AuditRecord.

        Todos os campos sao preenchidos para registros de Posting.
        O tenant_id e extraido do account_id (convencao: acc_{tenant_id}_{tipo})
        ou do metadata do JournalEntry pai (nao disponivel aqui -- usa "unknown").

        Args:
            new_image: NewImage do item POSTING# do DynamoDB Stream.
            year:      Ano extraido do timestamp para particionamento.
            month:     Mes extraido do timestamp para particionamento.
            day:       Dia extraido do timestamp para particionamento.

        Returns:
            AuditRecord com record_type="POSTING" e todos os campos preenchidos.
        """
        return AuditRecord(
            record_type="POSTING",
            entry_id=new_image["entry_id"]["S"],
            # external_id nao esta disponivel no item POSTING -- usa entry_id como referencia
            external_id=new_image.get("external_id", {}).get("S", ""),
            # entry_type nao esta disponivel no item POSTING -- usa string vazia
            entry_type=new_image.get("entry_type", {}).get("S", ""),
            account_id=new_image["account_id"]["S"],
            amount=int(new_image["amount"]["N"]),
            direction=new_image["direction"]["S"],
            currency=new_image["currency"]["S"],
            posting_index=int(new_image["posting_index"]["N"]),
            # tenant_id nao disponivel diretamente no posting -- usa "unknown"
            # Em producao, enriquecer via lookup ou incluir no item de posting
            tenant_id=new_image.get("tenant_id", {}).get("S", "unknown"),
            timestamp=new_image["timestamp"]["S"],
            metadata="{}",
            year=year,
            month=month,
            day=day,
        )

    def _send_to_firehose(self, audit_records: list[AuditRecord]) -> int:
        """
        Envia AuditRecords para o Kinesis Data Firehose via PutRecordBatch.

        Divide em batches de ate _FIREHOSE_MAX_BATCH_SIZE (limite AWS: 500 registros).
        Cada registro e serializado como JSON com newline para compatibilidade
        com o processamento do Firehose.

        Args:
            audit_records: Lista de AuditRecords a serem enviados.

        Returns:
            Numero total de registros enviados com sucesso.
        """
        total_sent = 0

        # Divide em batches para respeitar o limite do Firehose
        for batch_start in range(0, len(audit_records), _FIREHOSE_MAX_BATCH_SIZE):
            batch = audit_records[batch_start:batch_start + _FIREHOSE_MAX_BATCH_SIZE]

            # Serializa cada AuditRecord como JSON + newline (formato NDJSON)
            firehose_records = [
                {"Data": (json.dumps(asdict(record)) + "\n").encode("utf-8")}
                for record in batch
            ]

            response = self._firehose.put_record_batch(
                DeliveryStreamName=self._firehose_stream_name,
                Records=firehose_records,
            )

            # Verifica se houve falhas no batch
            failed_count = response.get("FailedPutCount", 0)
            sent_count = len(batch) - failed_count
            total_sent += sent_count

            if failed_count > 0:
                logger.warning(
                    "alguns registros falharam no PutRecordBatch do Firehose",
                    extra={
                        "operation": "send_to_firehose",
                        "batch_size": len(batch),
                        "failed_count": failed_count,
                        "sent_count": sent_count,
                    },
                )

        return total_sent


# ---------------------------------------------------------------------------
# Funcoes auxiliares privadas
# ---------------------------------------------------------------------------


def _extract_partition_fields(timestamp: str) -> tuple[str, str, str]:
    """
    Extrai campos de particionamento (year, month, day) de um timestamp ISO 8601.

    Formato esperado: YYYY-MM-DDTHH:MM:SS.ffffffZ
    Exemplo: "2026-03-10T14:30:00.000000Z" -> ("2026", "03", "10")

    Args:
        timestamp: Timestamp ISO 8601 no formato gerado pela factory.

    Returns:
        Tupla (year, month, day) como strings com zero-padding.

    Raises:
        ValueError: Se o timestamp nao tiver pelo menos 10 caracteres (YYYY-MM-DD).
    """
    if len(timestamp) < 10:
        raise ValueError(f"Timestamp invalido para extracao de particao: '{timestamp}'")

    # Formato: YYYY-MM-DDTHH:MM:SS...
    date_part = timestamp[:10]  # "YYYY-MM-DD"
    parts = date_part.split("-")

    if len(parts) != 3:
        raise ValueError(f"Formato de data invalido no timestamp: '{timestamp}'")

    year, month, day = parts
    return year, month, day
