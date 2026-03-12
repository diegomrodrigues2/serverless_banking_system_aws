"""
Property Test: Validações estruturais do ledger são executadas antes da policy.

Property 1 do design:
    Para qualquer transação inválida por invariante estrutural do ledger,
    a rejeição deve ocorrer antes do Validation Engine. Policies nunca
    substituem ZeroSum, MinorUnits, TransactionLimit ou tenant isolation.

Estratégia:
    Gera comandos estruturalmente inválidos (zero-sum violado, amounts
    inválidos) e verifica que a rejeição ocorre com o DomainError
    estrutural correto, mesmo quando uma FakePolicyFacade está injetada
    na cadeia. A facade NÃO deve ser chamada para comandos inválidos.

Requisitos: 1.1, 1.3, 5.5
"""
from __future__ import annotations

import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ledger.application.commands import CreateJournalEntryCommand, PostingInput
from ledger.domain.errors import InvalidAmountType, ZeroSumViolation
from ledger.domain.validators import (
    ValidationArtifacts,
    ValidationChain,
    ValidationResult,
    ZeroSumValidator,
    MinorUnitsValidator,
    TransactionLimitValidator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class SpyPolicyFacade:
    """
    Facade spy que registra se validate() foi chamado.

    Permite verificar que a facade NÃO é invocada quando validadores
    estruturais rejeitam o comando antes dela na cadeia.
    """

    def __init__(self) -> None:
        self.called = False

    def validate(self, command: object) -> ValidationResult:
        self.called = True
        return ValidationResult.success()


# ---------------------------------------------------------------------------
# Hypothesis Strategies
# ---------------------------------------------------------------------------

amount_strategy = st.integers(min_value=1, max_value=10_000_000)
currency_strategy = st.sampled_from(["BRL", "USD", "EUR"])
# Gera offsets para desbalancear postings (sempre > 0 para garantir desbalanceamento).
offset_strategy = st.integers(min_value=1, max_value=1_000_000)


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(amount=amount_strategy, currency=currency_strategy, offset=offset_strategy)
@settings(max_examples=50, deadline=None)
def test_zero_sum_violation_rejects_before_policy(
    amount: int,
    currency: str,
    offset: int,
) -> None:
    """
    Property 1: Transação com zero-sum violado é rejeitada pelo
    ZeroSumValidator ANTES de chegar ao PolicyValidationFacade.
    """
    spy = SpyPolicyFacade()
    chain = ValidationChain([
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
        spy,
    ])

    # Comando desbalanceado: DEBIT amount != CREDIT (amount + offset).
    cmd = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            PostingInput("acc_a", amount, currency, "DEBIT"),
            PostingInput("acc_b", amount + offset, currency, "CREDIT"),
        ],
    )

    with pytest.raises(ZeroSumViolation):
        chain.validate(cmd)

    # A facade NÃO deve ter sido chamada.
    assert spy.called is False


@pytest.mark.property
@given(
    invalid_amount=st.one_of(
        st.floats(min_value=0.1, max_value=1000.0),
        st.just(0),
        st.integers(max_value=-1),
        st.just(True),
        st.just(False),
    ),
    currency=currency_strategy,
)
@settings(max_examples=50, deadline=None)
def test_invalid_amount_rejects_before_policy(
    invalid_amount: object,
    currency: str,
) -> None:
    """
    Property 1: Transação com amount inválido (float, zero, negativo, bool)
    é rejeitada pelo MinorUnitsValidator ANTES de chegar ao PolicyValidationFacade.
    """
    spy = SpyPolicyFacade()
    chain = ValidationChain([
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
        spy,
    ])

    cmd = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            PostingInput("acc_a", invalid_amount, currency, "DEBIT"),
            PostingInput("acc_b", invalid_amount, currency, "CREDIT"),
        ],
    )

    with pytest.raises(InvalidAmountType):
        chain.validate(cmd)

    # A facade NÃO deve ter sido chamada.
    assert spy.called is False


@pytest.mark.property
@given(amount=amount_strategy, currency=currency_strategy)
@settings(max_examples=50, deadline=None)
def test_valid_command_reaches_policy_facade(
    amount: int,
    currency: str,
) -> None:
    """
    Complemento da Property 1: Transação estruturalmente válida
    DEVE chegar ao PolicyValidationFacade na cadeia.
    """
    spy = SpyPolicyFacade()
    chain = ValidationChain([
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
        spy,
    ])

    cmd = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=[
            PostingInput("acc_a", amount, currency, "DEBIT"),
            PostingInput("acc_b", amount, currency, "CREDIT"),
        ],
    )

    chain.validate(cmd)

    # A facade DEVE ter sido chamada para comandos válidos.
    assert spy.called is True
