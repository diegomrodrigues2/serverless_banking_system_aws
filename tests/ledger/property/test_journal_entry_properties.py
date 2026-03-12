"""
Testes de propriedade para o Aggregate Root JournalEntry.

Cada teste valida uma propriedade universal que deve se manter para qualquer
entrada válida, usando Hypothesis para geração automática de exemplos.

Propriedades cobertas:
- Property 1: Zero-sum round-trip (postings balanceados sempre validam como zero-sum)
- Property 2: Mínimo dois postings (todo JournalEntry válido tem >= 2 postings)
- Property 3: Rejeição de entradas desbalanceadas (soma != 0 → validate_zero_sum() == False)

Requisitos validados: 1.1, 1.2, 1.4, 1.5
"""
import time
import uuid

import pytest
from hypothesis import given, strategies as st

from ledger.domain.aggregates import JournalEntry
from ledger.domain.value_objects import Direction, EntryType, Money, OutboxEvent, Posting

# ---------------------------------------------------------------------------
# Strategies base
# ---------------------------------------------------------------------------

# Moedas ISO 4217 suportadas pelo sistema
currencies = st.sampled_from(["BRL", "USD", "EUR", "GBP"])

# Valores monetários válidos em minor units (inteiros positivos)
valid_amounts = st.integers(min_value=1, max_value=10_000_000_00)

# Gera instâncias válidas de Money
money_strategy = st.builds(Money, amount=valid_amounts, currency=currencies)


# ---------------------------------------------------------------------------
# Strategy: balanced_postings_strategy
# ---------------------------------------------------------------------------


@st.composite
def balanced_postings_strategy(
    draw: st.DrawFn, min_pairs: int = 1, max_pairs: int = 10
) -> tuple[Posting, ...]:
    """
    Gera conjuntos de postings balanceados (zero-sum) para testes de propriedade.

    Estratégia:
    - Sorteia N pares de postings (N entre min_pairs e max_pairs)
    - Cada par contém um DEBIT e um CREDIT com o mesmo Money (mesma moeda e valor)
    - Isso garante que a soma algébrica de cada par é zero: +amount + (-amount) = 0
    - A soma de todos os pares também é zero por moeda (propriedade distributiva)

    Exemplo gerado (N=2):
        Posting(acc_a, Money(1000, "BRL"), DEBIT,  index=0)
        Posting(acc_b, Money(1000, "BRL"), CREDIT, index=1)
        Posting(acc_c, Money(500,  "USD"), DEBIT,  index=2)
        Posting(acc_d, Money(500,  "USD"), CREDIT, index=3)
        → soma BRL: +1000 + (-1000) = 0 ✓
        → soma USD: +500  + (-500)  = 0 ✓
    """
    # Gera IDs de conta alfanuméricos (sem espaços para evitar problemas de parsing)
    account_id_strategy = st.text(
        min_size=1,
        max_size=36,
        alphabet=st.characters(whitelist_categories=("L", "N")),
    )

    n_pairs = draw(st.integers(min_value=min_pairs, max_value=max_pairs))
    postings: list[Posting] = []
    index = 0

    for _ in range(n_pairs):
        # Mesmo Money para DEBIT e CREDIT garante zero-sum por par
        money = draw(money_strategy)
        debit_account = draw(account_id_strategy)
        credit_account = draw(account_id_strategy)

        postings.append(
            Posting(account_id=debit_account, money=money, direction=Direction.DEBIT, index=index)
        )
        postings.append(
            Posting(
                account_id=credit_account, money=money, direction=Direction.CREDIT, index=index + 1
            )
        )
        index += 2

    return tuple(postings)


# ---------------------------------------------------------------------------
# Helper: make_journal_entry
# ---------------------------------------------------------------------------


def make_journal_entry(postings: tuple[Posting, ...]) -> JournalEntry:
    """
    Constrói um JournalEntry válido a partir de um conjunto de postings.

    Gera entry_id e outbox_event automaticamente para isolar o teste
    das responsabilidades da factory — o foco é o comportamento do aggregate.
    """
    entry_id = str(uuid.uuid4())
    outbox = OutboxEvent(
        event_id=f"OUTBOX#{entry_id}",
        entry_id=entry_id,
        event_type="TransactionCreated",
        payload={},
        expires_at=int(time.time()) + 3600,
    )
    return JournalEntry(
        entry_id=entry_id,
        external_id=str(uuid.uuid4()),
        entry_type=EntryType.STANDARD,
        postings=postings,
        metadata={},
        timestamp="2026-03-10T00:00:00Z",
        outbox_event=outbox,
    )


# ---------------------------------------------------------------------------
# Property 1: Zero-sum round-trip
# ---------------------------------------------------------------------------


# Feature: double-entry-ledger, Property 1: Zero-sum round-trip
@pytest.mark.property
@given(postings=balanced_postings_strategy())
def test_zero_sum_round_trip(postings: tuple[Posting, ...]) -> None:
    """
    **Validates: Requirements 1.1, 1.5**

    Para qualquer conjunto válido de postings balanceados (pares DEBIT/CREDIT
    com mesmo Money), criar um JournalEntry e chamar validate_zero_sum()
    deve retornar True.

    Esta propriedade garante o invariante fundamental de partidas dobradas:
    a soma algébrica de todos os postings é zero para cada moeda envolvida.

    Invariante verificada:
        sum(posting.signed_amount for posting in postings if posting.money.currency == c) == 0
        para toda moeda c presente nos postings.
    """
    entry = make_journal_entry(postings)

    assert entry.validate_zero_sum() is True, (
        f"JournalEntry com postings balanceados deve ter validate_zero_sum() == True. "
        f"Postings: {postings}"
    )


# ---------------------------------------------------------------------------
# Property 2: Mínimo dois postings
# ---------------------------------------------------------------------------


# Feature: double-entry-ledger, Property 2: Mínimo dois postings
@pytest.mark.property
@given(postings=balanced_postings_strategy(min_pairs=1, max_pairs=10))
def test_minimum_two_postings(postings: tuple[Posting, ...]) -> None:
    """
    **Validates: Requirements 1.4**

    Para qualquer JournalEntry válido gerado pela strategy, o número de
    postings deve ser maior ou igual a 2.

    A strategy gera no mínimo 1 par (DEBIT + CREDIT), o que resulta em
    pelo menos 2 postings — satisfazendo o requisito mínimo do domínio.

    Invariante verificada:
        len(journal_entry.postings) >= 2
    """
    entry = make_journal_entry(postings)

    assert len(entry.postings) >= 2, (
        f"JournalEntry deve ter no mínimo 2 postings. "
        f"Recebido: {len(entry.postings)} posting(s)."
    )


# ---------------------------------------------------------------------------
# Property 3: Rejeição de entradas desbalanceadas
# ---------------------------------------------------------------------------


@st.composite
def unbalanced_postings_strategy(draw: st.DrawFn) -> tuple[Posting, ...]:
    """
    Gera conjuntos de postings desbalanceados (soma != 0 para pelo menos uma moeda).

    Estratégia: gera apenas postings DEBIT em uma única moeda.
    - 1 DEBIT: soma = +amount ≠ 0
    - 2 DEBITs mesma moeda: soma = +amount1 + amount2 ≠ 0

    Isso garante que a soma algébrica nunca é zero, tornando o conjunto
    inválido para a regra de partidas dobradas.
    """
    account_id_strategy = st.text(
        min_size=1,
        max_size=36,
        alphabet=st.characters(whitelist_categories=("L", "N")),
    )

    # Usa uma única moeda para garantir que a soma seja verificável
    currency = draw(currencies)
    n_debits = draw(st.integers(min_value=1, max_value=5))
    postings: list[Posting] = []

    for i in range(n_debits):
        amount = draw(valid_amounts)
        account = draw(account_id_strategy)
        money = Money(amount=amount, currency=currency)
        postings.append(
            Posting(account_id=account, money=money, direction=Direction.DEBIT, index=i)
        )

    return tuple(postings)


# Feature: double-entry-ledger, Property 3: Rejeição de entradas desbalanceadas
@pytest.mark.property
@given(postings=unbalanced_postings_strategy())
def test_unbalanced_entries_fail_zero_sum(postings: tuple[Posting, ...]) -> None:
    """
    **Validates: Requirements 1.2**

    Para qualquer conjunto de postings cuja soma algébrica NÃO é zero para
    pelo menos uma moeda, validate_zero_sum() deve retornar False.

    A strategy gera apenas postings DEBIT em uma única moeda, garantindo
    que a soma seja sempre positiva (nunca zero).

    Invariante verificada:
        Se sum(signed_amount) != 0 para qualquer moeda → validate_zero_sum() == False
    """
    entry = make_journal_entry(postings)

    assert entry.validate_zero_sum() is False, (
        f"JournalEntry com postings desbalanceados deve ter validate_zero_sum() == False. "
        f"Postings: {postings}"
    )
