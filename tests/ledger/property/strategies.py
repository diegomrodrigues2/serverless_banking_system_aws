"""
Hypothesis strategies compartilhadas para testes de propriedade do Double-Entry Ledger.

Este módulo centraliza todas as strategies reutilizáveis, eliminando duplicação
entre os arquivos de teste de propriedade. Cada strategy é documentada com
os invariantes que garante e os requisitos que valida.

Strategies disponíveis:
- currencies:                  st.SearchStrategy[str] — moedas ISO 4217 suportadas
- valid_amounts:               st.SearchStrategy[int] — valores em minor units
- account_id_strategy:         st.SearchStrategy[str] — IDs de conta alfanuméricos
- money_strategy:              st.SearchStrategy[Money] — Money válido
- posting_strategy:            st.SearchStrategy[Posting] — Posting válido (direção aleatória)
- balanced_postings_strategy:  st.SearchStrategy[tuple[Posting, ...]] — conjuntos zero-sum
- journal_entry_strategy:      st.SearchStrategy[JournalEntry] — JournalEntry válido e balanceado

Requisitos validados: 1.1, 1.3, 2.1
"""
from __future__ import annotations

import time
import uuid

from hypothesis import strategies as st

from ledger.domain.aggregates import JournalEntry
from ledger.domain.value_objects import (
    Direction,
    EntryType,
    Money,
    OutboxEvent,
    Posting,
)

# ---------------------------------------------------------------------------
# Strategies primitivas
# ---------------------------------------------------------------------------

# Moedas ISO 4217 suportadas pelo sistema.
# Restrito a um conjunto pequeno e conhecido para facilitar debugging de falhas.
currencies: st.SearchStrategy[str] = st.sampled_from(["BRL", "USD", "EUR", "GBP"])

# Valores monetários válidos em minor units (inteiros positivos).
# Limite superior de 10_000_000_00 (R$ 100.000,00) evita overflow em somas.
valid_amounts: st.SearchStrategy[int] = st.integers(min_value=1, max_value=10_000_000_00)

# IDs de conta alfanuméricos sem espaços.
# Usa apenas letras e dígitos para evitar problemas de parsing e serialização.
account_id_strategy: st.SearchStrategy[str] = st.text(
    min_size=1,
    max_size=36,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)


# ---------------------------------------------------------------------------
# Strategy: money_strategy
# ---------------------------------------------------------------------------


# Money válido com amount int > 0 e currency ISO 4217.
# Garante invariantes do Value Object Money (Requisito 2.1).
money_strategy: st.SearchStrategy[Money] = st.builds(
    Money,
    amount=valid_amounts,
    currency=currencies,
)


# ---------------------------------------------------------------------------
# Strategy: posting_strategy
# ---------------------------------------------------------------------------


@st.composite
def posting_strategy(draw: st.DrawFn) -> Posting:
    """
    Gera um Posting válido com direção aleatória (DEBIT ou CREDIT).

    Invariantes garantidas:
    - account_id é string alfanumérica não vazia
    - money.amount é int > 0 (minor units)
    - money.currency é código ISO 4217 de 3 chars
    - direction é DEBIT ou CREDIT
    - index é inteiro >= 0

    Nota: postings gerados individualmente NÃO são necessariamente balanceados.
    Para conjuntos zero-sum, use balanced_postings_strategy.

    Validates: Requirements 1.3, 2.1
    """
    account_id = draw(account_id_strategy)
    money = draw(money_strategy)
    direction = draw(st.sampled_from([Direction.DEBIT, Direction.CREDIT]))
    index = draw(st.integers(min_value=0, max_value=999))

    return Posting(
        account_id=account_id,
        money=money,
        direction=direction,
        index=index,
    )


# ---------------------------------------------------------------------------
# Strategy: balanced_postings_strategy
# ---------------------------------------------------------------------------


@st.composite
def balanced_postings_strategy(
    draw: st.DrawFn,
    min_pairs: int = 1,
    max_pairs: int = 10,
) -> tuple[Posting, ...]:
    """
    Gera conjuntos de postings balanceados (zero-sum) para testes de propriedade.

    Estratégia:
    - Sorteia N pares de postings (N entre min_pairs e max_pairs)
    - Cada par contém um DEBIT e um CREDIT com o mesmo Money (mesma moeda e valor)
    - Isso garante que a soma algébrica de cada par é zero: +amount + (-amount) = 0
    - A soma de todos os pares também é zero por moeda (propriedade distributiva)

    Invariantes garantidas:
    - len(result) == 2 * N (sempre par)
    - len(result) >= 2 (mínimo 1 par = 2 postings)
    - sum(p.signed_amount for p in result if p.money.currency == c) == 0 para toda moeda c

    Exemplo gerado (N=2):
        Posting(acc_a, Money(1000, "BRL"), DEBIT,  index=0)
        Posting(acc_b, Money(1000, "BRL"), CREDIT, index=1)
        Posting(acc_c, Money(500,  "USD"), DEBIT,  index=2)
        Posting(acc_d, Money(500,  "USD"), CREDIT, index=3)
        → soma BRL: +1000 + (-1000) = 0 ✓
        → soma USD: +500  + (-500)  = 0 ✓

    Validates: Requirements 1.1, 1.3, 2.1
    """
    n_pairs = draw(st.integers(min_value=min_pairs, max_value=max_pairs))
    postings: list[Posting] = []
    index = 0

    for _ in range(n_pairs):
        # Mesmo Money para DEBIT e CREDIT garante zero-sum por par.
        # A moeda é sorteada por par para permitir múltiplas moedas no mesmo entry.
        money = draw(money_strategy)
        debit_account = draw(account_id_strategy)
        credit_account = draw(account_id_strategy)

        postings.append(Posting(
            account_id=debit_account,
            money=money,
            direction=Direction.DEBIT,
            index=index,
        ))
        postings.append(Posting(
            account_id=credit_account,
            money=money,
            direction=Direction.CREDIT,
            index=index + 1,
        ))
        index += 2

    return tuple(postings)


# ---------------------------------------------------------------------------
# Strategy: journal_entry_strategy
# ---------------------------------------------------------------------------


@st.composite
def journal_entry_strategy(
    draw: st.DrawFn,
    min_pairs: int = 1,
    max_pairs: int = 10,
    entry_type: EntryType = EntryType.STANDARD,
) -> JournalEntry:
    """
    Gera um JournalEntry válido e balanceado (zero-sum).

    Constrói o JournalEntry diretamente (sem passar pela factory) para
    isolar o comportamento do aggregate dos detalhes de criação.
    Gera entry_id, external_id e OutboxEvent automaticamente.

    Invariantes garantidas:
    - entry.validate_zero_sum() == True (postings balanceados)
    - len(entry.postings) >= 2 (mínimo 1 par)
    - entry.outbox_event.event_id.startswith("OUTBOX#") (prefixo obrigatório)
    - entry.entry_type == entry_type (tipo configurável)

    Args:
        min_pairs:  Número mínimo de pares DEBIT/CREDIT (padrão: 1).
        max_pairs:  Número máximo de pares DEBIT/CREDIT (padrão: 10).
        entry_type: Tipo do lançamento (padrão: STANDARD).

    Validates: Requirements 1.1, 1.3, 2.1
    """
    entry_id = str(uuid.uuid4())
    external_id = str(uuid.uuid4())
    postings = draw(balanced_postings_strategy(min_pairs=min_pairs, max_pairs=max_pairs))

    # OutboxEvent com prefixo obrigatório "OUTBOX#" e TTL de 1 hora
    outbox_event = OutboxEvent(
        event_id=f"OUTBOX#{entry_id}",
        entry_id=entry_id,
        event_type="TransactionCreated" if entry_type == EntryType.STANDARD else "TransactionReversed",
        payload={"entry_id": entry_id, "external_id": external_id},
        expires_at=int(time.time()) + 3600,
    )

    # Metadata vazio para lançamentos padrão; reversões adicionam original_entry_id
    metadata: dict = {}
    if entry_type == EntryType.REVERSAL:
        metadata["original_entry_id"] = str(uuid.uuid4())

    return JournalEntry(
        entry_id=entry_id,
        external_id=external_id,
        entry_type=entry_type,
        postings=postings,
        metadata=metadata,
        timestamp="2026-03-10T00:00:00.000000Z",
        outbox_event=outbox_event,
    )
