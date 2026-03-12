"""
DynamoDB Mapper do Double-Entry Ledger.

Responsável pela conversão bidirecional entre objetos de domínio e itens DynamoDB,
seguindo o single-table design definido no modelo de dados.

Modelo de chaves (single-table design):
    ACCOUNT    → PK: ACCOUNT#{account_id}   SK: ACCOUNT#{account_id}
    BALANCE    → PK: ACCOUNT#{account_id}   SK: BALANCE#{currency}
    JOURNAL    → PK: JOURNAL#{entry_id}     SK: JOURNAL#{entry_id}
    POSTING    → PK: ACCOUNT#{account_id}   SK: POSTING#{timestamp}#{entry_id}#{index}
    OUTBOX     → PK: OUTBOX#{entry_id}      SK: OUTBOX#{entry_id}
    IDEMPOTENCY→ PK: IDEMPOTENCY#{ext_id}   SK: IDEMPOTENCY#{ext_id}

Requisitos atendidos: 11.1, 11.2, 11.3, 11.4
"""
from __future__ import annotations

import json
from typing import Any

from ledger.domain.aggregates import JournalEntry
from ledger.domain.value_objects import (
    Balance,
    Direction,
    EntryType,
    Money,
    OutboxEvent,
    Posting,
)


# ---------------------------------------------------------------------------
# Constantes de prefixo de chave (single-table design)
# ---------------------------------------------------------------------------

PK_ACCOUNT = "ACCOUNT"
PK_JOURNAL = "JOURNAL"
PK_OUTBOX = "OUTBOX"
PK_IDEMPOTENCY = "IDEMPOTENCY"

SK_BALANCE = "BALANCE"
SK_POSTING = "POSTING"


# ---------------------------------------------------------------------------
# Mapeamento domínio → DynamoDB items
# ---------------------------------------------------------------------------


def journal_entry_to_dynamo_item(entry: JournalEntry) -> dict[str, Any]:
    """
    Converte um JournalEntry para o item DynamoDB correspondente.

    Chaves:
        PK: JOURNAL#{entry_id}
        SK: JOURNAL#{entry_id}

    Args:
        entry: Aggregate Root JournalEntry do domínio.

    Returns:
        Dict com atributos DynamoDB prontos para Put.
    """
    return {
        "PK": {"S": f"{PK_JOURNAL}#{entry.entry_id}"},
        "SK": {"S": f"{PK_JOURNAL}#{entry.entry_id}"},
        "entry_id": {"S": entry.entry_id},
        "external_id": {"S": entry.external_id},
        "entry_type": {"S": entry.entry_type.value},
        "timestamp": {"S": entry.timestamp},
        # metadata serializado como JSON string para suportar estruturas arbitrárias
        "metadata": {"S": json.dumps(entry.metadata)},
    }


def posting_to_dynamo_item(posting: Posting, entry_id: str, timestamp: str) -> dict[str, Any]:
    """
    Converte um Posting para o item DynamoDB correspondente.

    Chaves:
        PK: ACCOUNT#{account_id}
        SK: POSTING#{timestamp}#{entry_id}#{index}

    O posting_sort_key garante ordenação cronológica no extrato (Requisito 11.2).

    Args:
        posting:   Value Object Posting do domínio.
        entry_id:  UUID do JournalEntry pai.
        timestamp: Timestamp ISO 8601 do JournalEntry (para o sort key).

    Returns:
        Dict com atributos DynamoDB prontos para Put.
    """
    posting_sort_key = build_posting_sort_key(timestamp, entry_id, posting.index)
    return {
        "PK": {"S": f"{PK_ACCOUNT}#{posting.account_id}"},
        "SK": {"S": posting_sort_key},
        # entry_id_gsi é o atributo de partição do GSI-EntryPostings.
        # Permite buscar todos os postings de um JournalEntry por entry_id
        # sem fazer scan completo da tabela.
        "entry_id_gsi": {"S": f"{PK_JOURNAL}#{entry_id}"},
        "entry_id": {"S": entry_id},
        "account_id": {"S": posting.account_id},
        "amount": {"N": str(posting.money.amount)},
        "currency": {"S": posting.money.currency},
        "direction": {"S": posting.direction.value},
        "posting_index": {"N": str(posting.index)},
        "timestamp": {"S": timestamp},
    }


def outbox_event_to_dynamo_item(event: OutboxEvent) -> dict[str, Any]:
    """
    Converte um OutboxEvent para o item DynamoDB correspondente.

    Chaves:
        PK: OUTBOX#{entry_id}
        SK: OUTBOX#{entry_id}

    O campo expires_at é usado como TTL pelo DynamoDB para limpeza automática.

    Args:
        event: Value Object OutboxEvent do domínio.

    Returns:
        Dict com atributos DynamoDB prontos para Put.
    """
    return {
        "PK": {"S": f"{PK_OUTBOX}#{event.entry_id}"},
        "SK": {"S": f"{PK_OUTBOX}#{event.entry_id}"},
        "event_id": {"S": event.event_id},
        "entry_id": {"S": event.entry_id},
        "event_type": {"S": event.event_type},
        # payload serializado como JSON string
        "payload": {"S": json.dumps(event.payload)},
        # expires_at como número para TTL do DynamoDB
        "expires_at": {"N": str(event.expires_at)},
    }


def idempotency_record_to_dynamo_item(external_id: str, entry_id: str) -> dict[str, Any]:
    """
    Cria o item de idempotência para garantir unicidade do external_id.

    Chaves:
        PK: IDEMPOTENCY#{external_id}
        SK: IDEMPOTENCY#{external_id}

    Gravado com ConditionExpression attribute_not_exists(PK) para garantir
    que o mesmo external_id não seja processado duas vezes.

    Args:
        external_id: Chave de idempotência fornecida pelo caller.
        entry_id:    UUID do JournalEntry criado.

    Returns:
        Dict com atributos DynamoDB prontos para Put condicional.
    """
    return {
        "PK": {"S": f"{PK_IDEMPOTENCY}#{external_id}"},
        "SK": {"S": f"{PK_IDEMPOTENCY}#{external_id}"},
        "external_id": {"S": external_id},
        "entry_id": {"S": entry_id},
    }


def balance_update_expression(
    account_id: str,
    currency: str,
    signed_amount: int,
    expected_version: int,
) -> dict[str, Any]:
    """
    Gera os parâmetros para atualização de Balance com OCC (Optimistic Concurrency Control).

    Usa UpdateExpression para incrementar balance_amount e version atomicamente.
    A ConditionExpression garante que a versão esperada corresponde à atual,
    prevenindo escritas concorrentes que corrompam o saldo.

    Fórmula:
        balance_amount += signed_amount
        version        += 1
        last_update     = <timestamp atual>

    Args:
        account_id:       Identificador da conta.
        currency:         Código ISO 4217 da moeda.
        signed_amount:    Valor com sinal (positivo para DEBIT, negativo para CREDIT).
        expected_version: Versão atual do Balance para OCC.

    Returns:
        Dict com Key, UpdateExpression, ConditionExpression e ExpressionAttributeValues.
    """
    return {
        "Key": {
            "PK": {"S": f"{PK_ACCOUNT}#{account_id}"},
            "SK": {"S": f"{SK_BALANCE}#{currency}"},
        },
        "UpdateExpression": (
            "SET balance_amount = if_not_exists(balance_amount, :zero) + :delta, "
            "#ver = if_not_exists(#ver, :zero) + :one, "
            "last_update = :now, "
            "account_id = :account_id, "
            "currency = :currency"
        ),
        # Condição OCC: version deve ser igual ao esperado (ou não existir para novo saldo)
        "ConditionExpression": (
            "attribute_not_exists(PK) OR #ver = :expected_version"
        ),
        "ExpressionAttributeNames": {
            # 'version' é palavra reservada no DynamoDB — usa alias #ver
            "#ver": "version",
        },
        "ExpressionAttributeValues": {
            ":delta": {"N": str(signed_amount)},
            ":zero": {"N": "0"},
            ":one": {"N": "1"},
            ":expected_version": {"N": str(expected_version)},
            ":now": {"S": _current_iso8601()},
            ":account_id": {"S": account_id},
            ":currency": {"S": currency},
        },
    }


# ---------------------------------------------------------------------------
# Mapeamento DynamoDB items → domínio
# ---------------------------------------------------------------------------


def dynamo_item_to_journal_entry(
    item: dict[str, Any],
    postings: list[Posting],
    outbox_event: OutboxEvent,
) -> JournalEntry:
    """
    Converte um item DynamoDB de JournalEntry de volta para o domínio.

    Requer os postings e o outbox_event já convertidos, pois eles são
    armazenados em itens separados no single-table design.

    Args:
        item:        Item DynamoDB com atributos do JournalEntry.
        postings:    Lista de Postings já convertidos do DynamoDB.
        outbox_event: OutboxEvent já convertido do DynamoDB.

    Returns:
        JournalEntry reconstruído com todos os campos do domínio.
    """
    return JournalEntry(
        entry_id=item["entry_id"]["S"],
        external_id=item["external_id"]["S"],
        entry_type=EntryType(item["entry_type"]["S"]),
        postings=tuple(postings),
        metadata=json.loads(item["metadata"]["S"]),
        timestamp=item["timestamp"]["S"],
        outbox_event=outbox_event,
    )


def dynamo_item_to_posting(item: dict[str, Any]) -> Posting:
    """
    Converte um item DynamoDB de Posting de volta para o Value Object do domínio.

    Args:
        item: Item DynamoDB com atributos do Posting.

    Returns:
        Posting imutável reconstruído.
    """
    return Posting(
        account_id=item["account_id"]["S"],
        money=Money(
            amount=int(item["amount"]["N"]),
            currency=item["currency"]["S"],
        ),
        direction=Direction(item["direction"]["S"]),
        index=int(item["posting_index"]["N"]),
    )


def dynamo_item_to_outbox_event(item: dict[str, Any]) -> OutboxEvent:
    """
    Converte um item DynamoDB de OutboxEvent de volta para o Value Object do domínio.

    Args:
        item: Item DynamoDB com atributos do OutboxEvent.

    Returns:
        OutboxEvent imutável reconstruído.
    """
    return OutboxEvent(
        event_id=item["event_id"]["S"],
        entry_id=item["entry_id"]["S"],
        event_type=item["event_type"]["S"],
        payload=json.loads(item["payload"]["S"]),
        expires_at=int(item["expires_at"]["N"]),
    )


def dynamo_item_to_balance(item: dict[str, Any]) -> Balance:
    """
    Converte um item DynamoDB de Balance de volta para o Value Object do domínio.

    Args:
        item: Item DynamoDB com atributos do Balance.

    Returns:
        Balance reconstruído com version para OCC.
    """
    return Balance(
        account_id=item["account_id"]["S"],
        currency=item["currency"]["S"],
        balance_amount=int(item["balance_amount"]["N"]),
        version=int(item["version"]["N"]),
        last_update=item["last_update"]["S"],
    )


# ---------------------------------------------------------------------------
# Funções auxiliares
# ---------------------------------------------------------------------------


def build_posting_sort_key(timestamp: str, entry_id: str, index: int) -> str:
    """
    Gera o posting_sort_key no formato canônico do single-table design.

    Formato: POSTING#{timestamp}#{entry_id}#{index}

    A ordenação lexicográfica do sort key garante que postings sejam
    retornados em ordem cronológica nas queries de extrato (Requisito 11.2).

    Exemplo:
        build_posting_sort_key("2026-03-10T14:30:00.000000Z", "uuid-v4", 0)
        → "POSTING#2026-03-10T14:30:00.000000Z#uuid-v4#0"

    Args:
        timestamp: Timestamp ISO 8601 do JournalEntry pai.
        entry_id:  UUID v4 do JournalEntry pai.
        index:     Índice ordinal do posting dentro do JournalEntry (0-based).

    Returns:
        String no formato "POSTING#{timestamp}#{entry_id}#{index}".
    """
    return f"{SK_POSTING}#{timestamp}#{entry_id}#{index}"


def extract_entry_id_from_idempotency_item(item: dict[str, Any]) -> str:
    """
    Extrai o entry_id de um item de idempotência do DynamoDB.

    Usado pelo repositório para retornar o entry_id original quando
    um external_id duplicado é detectado.

    Args:
        item: Item DynamoDB do registro de idempotência.

    Returns:
        entry_id do JournalEntry original.
    """
    return item["entry_id"]["S"]


def _current_iso8601() -> str:
    """
    Retorna o timestamp atual em formato ISO 8601 UTC.

    Usado internamente para preencher o campo last_update nas atualizações
    de Balance via UpdateExpression.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
