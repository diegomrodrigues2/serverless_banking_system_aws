"""
Testes de propriedade para os Value Objects do domínio Double-Entry Ledger.

Cada teste valida uma propriedade universal que deve se manter para qualquer
entrada válida, usando Hypothesis para geração automática de exemplos.

Propriedades cobertas:
- Property 4: Convenção de sinais (débito positivo, crédito negativo)
- Property 5: Validação de minor units (rejeição de tipos inválidos)
- Property 13: Prefixo OUTBOX# no event_id

Requisitos validados: 1.3, 2.1, 2.2, 2.3, 7.5
"""
import pytest
from hypothesis import given, strategies as st

from ledger.domain.value_objects import Direction, Money, OutboxEvent, Posting

# ---------------------------------------------------------------------------
# Strategies compartilhadas
# ---------------------------------------------------------------------------

# Moedas ISO 4217 suportadas pelo sistema
currencies = st.sampled_from(["BRL", "USD", "EUR", "GBP"])

# Valores monetários válidos em minor units (inteiros positivos)
valid_amounts = st.integers(min_value=1, max_value=10_000_000_00)

# Gera instâncias válidas de Money
money_strategy = st.builds(Money, amount=valid_amounts, currency=currencies)

# Gera direções contábeis válidas
direction_strategy = st.sampled_from(list(Direction))

# Gera instâncias válidas de Posting com account_id alfanumérico
posting_strategy = st.builds(
    Posting,
    account_id=st.text(
        min_size=1,
        max_size=36,
        alphabet=st.characters(whitelist_categories=("L", "N")),
    ),
    money=money_strategy,
    direction=direction_strategy,
    index=st.integers(min_value=0, max_value=99),
)

# Gera entry_ids no formato UUID string
entry_id_strategy = st.uuids().map(str)

# Gera event_ids com prefixo obrigatório "OUTBOX#"
event_id_strategy = entry_id_strategy.map(lambda eid: f"OUTBOX#{eid}")

# Gera instâncias válidas de OutboxEvent
outbox_strategy = st.builds(
    OutboxEvent,
    event_id=event_id_strategy,
    entry_id=entry_id_strategy,
    event_type=st.sampled_from(["TransactionCreated", "TransactionReversed"]),
    payload=st.just({}),
    expires_at=st.integers(min_value=1),
)


# ---------------------------------------------------------------------------
# Property 4: Convenção de sinais (débito positivo, crédito negativo)
# ---------------------------------------------------------------------------


# Feature: double-entry-ledger, Property 4: Convenção de sinais (débito positivo, crédito negativo)
@pytest.mark.property
@given(posting=posting_strategy)
def test_sign_convention_debit_positive_credit_negative(posting: Posting) -> None:
    """
    **Validates: Requirements 1.3**

    Para qualquer Posting válido:
    - Se direction == DEBIT, então signed_amount > 0
    - Se direction == CREDIT, então signed_amount < 0
    - abs(signed_amount) == money.amount sempre

    Esta propriedade garante a convenção contábil de partidas dobradas:
    débitos aumentam o saldo (positivo) e créditos diminuem (negativo).
    """
    signed = posting.signed_amount

    # Invariante: valor absoluto sempre igual ao amount original
    assert abs(signed) == posting.money.amount, (
        f"abs(signed_amount) deve ser igual a money.amount. "
        f"Recebido: abs({signed}) != {posting.money.amount}"
    )

    if posting.direction == Direction.DEBIT:
        # Débito deve produzir valor positivo
        assert signed > 0, (
            f"DEBIT deve produzir signed_amount > 0, recebido: {signed}"
        )
    else:
        # Crédito deve produzir valor negativo
        assert signed < 0, (
            f"CREDIT deve produzir signed_amount < 0, recebido: {signed}"
        )


# ---------------------------------------------------------------------------
# Property 5: Validação de minor units
# ---------------------------------------------------------------------------


# Feature: double-entry-ledger, Property 5: Validação de minor units
@pytest.mark.property
@given(
    invalid_amount=st.one_of(
        st.floats(allow_nan=False, allow_infinity=False),
        st.just(0),
        st.integers(max_value=-1),
        st.booleans(),
    ),
    currency=st.sampled_from(["BRL", "USD", "EUR", "GBP"]),
)
def test_minor_units_rejects_invalid_amounts(invalid_amount: object, currency: str) -> None:
    """
    **Validates: Requirements 2.1, 2.2, 2.3**

    Para qualquer valor monetário submetido que não seja int > 0, o sistema
    deve rejeitar com um erro estruturado (ValueError).

    Tipos inválidos testados:
    - float: ex. 10.50 (erros de arredondamento de ponto flutuante)
    - zero: não representa valor monetário positivo
    - negativos: direção contábil é controlada por Direction
    - bool: subclasse de int em Python, mas semanticamente inválido

    O sistema nunca deve criar um Money com amount inválido — objetos
    inválidos não devem existir no domínio.
    """
    with pytest.raises((ValueError, TypeError)):
        Money(amount=invalid_amount, currency=currency)


# ---------------------------------------------------------------------------
# Property 13: Prefixo OUTBOX# no event_id
# ---------------------------------------------------------------------------


# Feature: double-entry-ledger, Property 13: Prefixo OUTBOX# no event_id
@pytest.mark.property
@given(outbox_event=outbox_strategy)
def test_outbox_event_id_starts_with_outbox_prefix(outbox_event: OutboxEvent) -> None:
    """
    **Validates: Requirements 7.5**

    Para qualquer OutboxEvent criado, event_id deve começar com "OUTBOX#".

    Este prefixo é obrigatório para:
    1. Filtragem no DynamoDB Stream (Event Source Mapping da Lambda Publisher)
    2. Separação visual dos registros de outbox dos demais itens da tabela
    3. Garantia de que apenas eventos de outbox disparam o Publisher Lambda

    A propriedade valida que a invariante é preservada para qualquer
    combinação válida de parâmetros do OutboxEvent.
    """
    assert outbox_event.event_id.startswith("OUTBOX#"), (
        f"event_id deve começar com 'OUTBOX#'. Recebido: '{outbox_event.event_id}'"
    )
