"""
Fábricas do domínio do Double-Entry Ledger.

JournalEntryFactory implementa o padrão GoF Factory Method para criação
de JournalEntries padrão e de reversão, garantindo invariantes do agregado.

Responsabilidades da factory:
- Geração de entry_id (UUID v4) — identificador único e imprevisível
- Geração de timestamp ISO 8601 — momento do fato contábil
- Criação do OutboxEvent associado — evento transacional para o Outbox Pattern
- Conversão de PostingInput (DTO) para Posting (Value Object do domínio)
- Inversão de direções para reversões (DEBIT→CREDIT, CREDIT→DEBIT)

A factory NÃO valida regras de negócio (zero-sum, minor units, limites) —
essa responsabilidade pertence à ValidationChain. A factory assume que o
comando já foi validado antes de ser passado para ela.

Requisitos atendidos: 1.4, 7.1, 7.2, 9.2, 9.3
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ledger.domain.aggregates import JournalEntry
from ledger.domain.value_objects import (
    Direction,
    EntryType,
    Money,
    OutboxEvent,
    Posting,
)

if TYPE_CHECKING:
    # Importações apenas para type checking — evita dependência circular
    # entre domain (factories) e application (commands).
    from ledger.application.commands import (
        CreateJournalEntryCommand,
        CreateReversalCommand,
    )
    from ledger.domain.validators import ValidationArtifacts

# TTL padrão para OutboxEvents: 24 horas em segundos.
# Após esse período, o DynamoDB remove automaticamente o registro via TTL.
_OUTBOX_TTL_SECONDS = 86_400


class JournalEntryFactory:
    """
    GoF Factory Method — cria instâncias de JournalEntry.

    Centraliza a lógica de construção do agregado JournalEntry, garantindo
    que todos os campos obrigatórios sejam preenchidos corretamente:
    - entry_id único (UUID v4)
    - timestamp de criação (ISO 8601 UTC)
    - OutboxEvent com prefixo "OUTBOX#" e TTL de 24h
    - Postings convertidos de DTO para Value Objects do domínio

    Dois métodos de criação especializados (Factory Method pattern):
    - create_standard: lançamento contábil padrão
    - create_reversal: lançamento de reversão com postings invertidos
    """

    def create_standard(
        self,
        command: CreateJournalEntryCommand,
        validation_artifacts: ValidationArtifacts | None = None,
    ) -> JournalEntry:
        """
        Cria um JournalEntry padrão (EntryType.STANDARD) a partir de um comando.

        Algoritmo:
        1. Gera entry_id como UUID v4 (str)
        2. Gera timestamp como ISO 8601 UTC (ex: "2026-03-10T14:30:00.000000Z")
        3. Converte cada PostingInput do comando em Posting (Value Object)
        4. Mescla metadata do comando com DecisionSummary dos artefatos de validação
        5. Cria OutboxEvent com event_type "TransactionCreated" e TTL de 24h
        6. Retorna JournalEntry imutável (frozen dataclass)

        O DecisionSummary é persistido atomicamente no metadata do JournalEntry
        sob a chave "policy_validation", sem mutar o comando original (Requisito 12.4).

        Args:
            command:              Comando com external_id, lista de PostingInputs e metadata.
            validation_artifacts: Artefatos da validação de policy (DecisionSummary).
                                  None se a policy validation não está habilitada.

        Returns:
            JournalEntry válido com entry_type=STANDARD e OutboxEvent associado.
        """
        entry_id = str(uuid.uuid4())
        timestamp = _generate_iso8601_timestamp()

        # Converte cada PostingInput (DTO) para Posting (Value Object do domínio).
        # O índice ordinal (index) é atribuído pela posição na lista do comando.
        postings = tuple(
            _convert_posting_input(posting_input, index)
            for index, posting_input in enumerate(command.postings)
        )

        # Mescla metadata do comando com DecisionSummary dos artefatos de validação.
        # O DecisionSummary é adicionado sob a chave "policy_validation" para
        # manter separação clara entre metadata do caller e dados de policy.
        # O comando original NÃO é mutado — criamos um novo dict (Requisito 7.5).
        metadata = _merge_metadata_with_artifacts(command.metadata, validation_artifacts)

        outbox_event = _create_outbox_event(
            entry_id=entry_id,
            event_type="TransactionCreated",
            payload={"entry_id": entry_id, "external_id": command.external_id},
        )

        return JournalEntry(
            entry_id=entry_id,
            external_id=command.external_id,
            entry_type=EntryType.STANDARD,
            postings=postings,
            metadata=metadata,
            timestamp=timestamp,
            outbox_event=outbox_event,
        )

    def create_reversal(
        self,
        original: JournalEntry,
        command: CreateReversalCommand,
    ) -> JournalEntry:
        """
        Cria um JournalEntry de reversão (EntryType.REVERSAL) a partir do original.

        A reversão anula o lançamento original criando postings com direções
        invertidas: DEBIT→CREDIT e CREDIT→DEBIT. A soma dos postings do original
        com os postings da reversão deve ser zero por moeda (propriedade de anulação).

        Algoritmo:
        1. Gera novo entry_id como UUID v4 (diferente do original)
        2. Gera novo timestamp ISO 8601 UTC
        3. Inverte a direção de cada posting do original (DEBIT↔CREDIT)
        4. Define metadata com referência ao original: {"original_entry_id": ...}
        5. Cria OutboxEvent com event_type "TransactionReversed"
        6. Retorna novo JournalEntry imutável com entry_type=REVERSAL

        Args:
            original: JournalEntry original que será revertido.
            command:  Comando com original_entry_id, external_id e metadata adicional.

        Returns:
            Novo JournalEntry com entry_type=REVERSAL e postings invertidos.
        """
        entry_id = str(uuid.uuid4())
        timestamp = _generate_iso8601_timestamp()

        # Inverte a direção de cada posting do original.
        # DEBIT → CREDIT e CREDIT → DEBIT para anular o efeito contábil.
        # O índice ordinal é preservado da posição original.
        reversed_postings = tuple(
            _invert_posting(posting)
            for posting in original.postings
        )

        # Metadata da reversão: referencia o lançamento original (Requisito 9.3).
        # Mescla com metadata adicional do comando, priorizando a referência ao original.
        reversal_metadata: dict = {
            "original_entry_id": original.entry_id,
            **command.metadata,
        }

        outbox_event = _create_outbox_event(
            entry_id=entry_id,
            event_type="TransactionReversed",
            payload={
                "entry_id": entry_id,
                "external_id": command.external_id,
                "original_entry_id": original.entry_id,
            },
        )

        return JournalEntry(
            entry_id=entry_id,
            external_id=command.external_id,
            entry_type=EntryType.REVERSAL,
            postings=reversed_postings,
            metadata=reversal_metadata,
            timestamp=timestamp,
            outbox_event=outbox_event,
        )


# ---------------------------------------------------------------------------
# Funções auxiliares privadas
# ---------------------------------------------------------------------------


def _generate_iso8601_timestamp() -> str:
    """
    Gera timestamp atual em formato ISO 8601 UTC com sufixo 'Z'.

    Exemplo de saída: "2026-03-10T14:30:00.123456Z"

    Usa datetime.utcnow() para compatibilidade com Python 3.11+.
    O sufixo 'Z' indica UTC explicitamente para consumidores do timestamp.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _merge_metadata_with_artifacts(
    command_metadata: dict,
    validation_artifacts: "ValidationArtifacts | None",
) -> dict:
    """
    Mescla metadata do comando com DecisionSummary dos artefatos de validação.

    Se validation_artifacts contém um DecisionSummary com método to_metadata(),
    o resultado é mesclado no metadata sob a chave "policy_validation".
    O dict original do comando NÃO é mutado — um novo dict é criado.

    Args:
        command_metadata:    Metadata original do comando.
        validation_artifacts: Artefatos da validação de policy (pode ser None).

    Returns:
        Novo dict com metadata mesclado. Se não há artefatos, retorna cópia do original.
    """
    # Cria cópia para não mutar o dict do comando original.
    merged = dict(command_metadata)

    if validation_artifacts is None:
        return merged

    # Extrai o DecisionSummary dos artefatos.
    decision_summary = validation_artifacts.decision_summary
    if decision_summary is None:
        return merged

    # O DecisionSummary expõe to_metadata() que retorna
    # {"policy_validation": {...}} — mesclamos no metadata do entry.
    if hasattr(decision_summary, "to_metadata"):
        merged.update(decision_summary.to_metadata())

    return merged


def _create_outbox_event(
    entry_id: str,
    event_type: str,
    payload: dict,
) -> OutboxEvent:
    """
    Cria um OutboxEvent com prefixo 'OUTBOX#' e TTL de 24 horas.

    O event_id segue o formato "OUTBOX#{entry_id}" para facilitar
    filtragem no DynamoDB Stream e no Event Source Mapping da Lambda Publisher.

    Args:
        entry_id:   UUID do JournalEntry associado.
        event_type: Tipo do evento ("TransactionCreated" | "TransactionReversed").
        payload:    Dados do evento para publicação no barramento.

    Returns:
        OutboxEvent com TTL configurado para 24 horas a partir de agora.
    """
    return OutboxEvent(
        event_id=f"OUTBOX#{entry_id}",
        entry_id=entry_id,
        event_type=event_type,
        payload=payload,
        expires_at=int(time.time()) + _OUTBOX_TTL_SECONDS,
    )


def _convert_posting_input(posting_input: object, index: int) -> Posting:
    """
    Converte um PostingInput (DTO da camada de aplicação) em Posting (Value Object).

    O PostingInput possui os campos:
    - account_id: str
    - amount: int (minor units)
    - currency: str (ISO 4217)
    - direction: str ("DEBIT" | "CREDIT")

    Args:
        posting_input: Objeto com campos account_id, amount, currency, direction.
        index:         Posição ordinal do posting dentro do JournalEntry (0-based).

    Returns:
        Posting imutável com Money e Direction do domínio.
    """
    return Posting(
        account_id=posting_input.account_id,  # type: ignore[attr-defined]
        money=Money(
            amount=posting_input.amount,    # type: ignore[attr-defined]
            currency=posting_input.currency,  # type: ignore[attr-defined]
        ),
        direction=Direction(posting_input.direction),  # type: ignore[attr-defined]
        index=index,
    )


def _invert_posting(posting: Posting) -> Posting:
    """
    Cria um novo Posting com a direção invertida (DEBIT↔CREDIT).

    Preserva account_id, money (amount e currency) e index do posting original.
    Apenas a direção é invertida para anular o efeito contábil.

    Args:
        posting: Posting original a ser invertido.

    Returns:
        Novo Posting com direção oposta à do original.
    """
    inverted_direction = (
        Direction.CREDIT if posting.direction == Direction.DEBIT else Direction.DEBIT
    )
    return Posting(
        account_id=posting.account_id,
        money=posting.money,
        direction=inverted_direction,
        index=posting.index,
    )
