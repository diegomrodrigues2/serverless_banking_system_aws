"""
Property 12: CanonicalValidationContext é estável.

Para quaisquer dois CreateJournalEntryCommand semanticamente equivalentes
(mesmas postings, mesmo policy_context, mesmo tenant/operação), o
CanonicalValidationContext resultante deve ser idêntico.

Isso garante fidelidade de replay: o mesmo comando sempre produz o mesmo
contexto, independentemente de quando ou quantas vezes é canonicalizado.

**Validates: Requirements 8.5, 14.1**

Requisitos cobertos: 8.5, 14.1
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ledger.application.commands import CreateJournalEntryCommand, PostingInput
from validation_engine.application.context_builder import (
    DefaultCanonicalValidationContextBuilder,
)
from validation_engine.domain.context import CanonicalValidationContext

# ---------------------------------------------------------------------------
# Strategies para geração de comandos arbitrários
# ---------------------------------------------------------------------------

# Identificadores simples para contas, tenants, etc.
_identifier = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_",
    min_size=1,
    max_size=20,
)

# Moedas válidas (subconjunto representativo)
_currency = st.sampled_from(["BRL", "USD", "EUR", "GBP"])

# Direções válidas
_direction = st.sampled_from(["DEBIT", "CREDIT"])

# Valores em minor units (positivos)
_amount = st.integers(min_value=1, max_value=10_000_000)

# Tipos permitidos em policy_context
_policy_context_value = st.one_of(
    st.text(min_size=0, max_size=20),
    st.integers(min_value=0, max_value=1_000_000),
    st.booleans(),
)


@st.composite
def _posting_input_strategy(draw: st.DrawFn) -> PostingInput:
    """Gera um PostingInput arbitrário mas válido."""
    return PostingInput(
        account_id=draw(_identifier),
        amount=draw(_amount),
        currency=draw(_currency),
        direction=draw(_direction),
    )


@st.composite
def _policy_context_strategy(draw: st.DrawFn) -> dict:
    """Gera um policy_context arbitrário com tipos permitidos."""
    keys = draw(st.lists(_identifier, min_size=0, max_size=5, unique=True))
    return {key: draw(_policy_context_value) for key in keys}


@st.composite
def _command_strategy(draw: st.DrawFn) -> CreateJournalEntryCommand:
    """
    Gera um CreateJournalEntryCommand arbitrário mas válido.

    Inclui campos de identificação via metadata para simular o comportamento
    do builder com a versão atual do comando (antes da task 11.2).
    """
    external_id = draw(_identifier)
    postings = draw(st.lists(_posting_input_strategy(), min_size=1, max_size=8))
    tenant_id = draw(_identifier)
    operation_type = draw(st.sampled_from(["TRANSFER", "PAYMENT", "REVERSAL", "UNKNOWN"]))
    product_code = draw(st.one_of(st.none(), st.sampled_from(["PIX", "TED", "BOLETO"])))
    channel = draw(st.one_of(st.none(), st.sampled_from(["MOBILE", "API", "BRANCH"])))
    policy_context = draw(_policy_context_strategy())

    metadata: dict = {"tenant_id": tenant_id, "operation_type": operation_type}
    if product_code:
        metadata["product_code"] = product_code
    if channel:
        metadata["channel"] = channel

    command = CreateJournalEntryCommand(
        external_id=external_id,
        postings=postings,
        metadata=metadata,
    )
    # Injeta policy_context diretamente (simula campo adicionado na task 11.2)
    if policy_context:
        object.__setattr__(command, "policy_context", policy_context)

    return command


def _clone_command(command: CreateJournalEntryCommand) -> CreateJournalEntryCommand:
    """
    Cria uma cópia semanticamente equivalente do comando.

    A cópia tem os mesmos dados mas é um objeto Python diferente,
    garantindo que a igualdade do contexto não depende de identidade de objeto.
    """
    # Cria nova lista de postings com os mesmos dados
    cloned_postings = [
        PostingInput(
            account_id=p.account_id,
            amount=p.amount,
            currency=p.currency,
            direction=p.direction,
        )
        for p in command.postings
    ]

    # Cria novo comando com os mesmos dados
    cloned = CreateJournalEntryCommand(
        external_id=command.external_id,
        postings=cloned_postings,
        metadata=dict(command.metadata),
    )

    # Copia policy_context se presente
    policy_context = getattr(command, "policy_context", None)
    if policy_context:
        object.__setattr__(cloned, "policy_context", dict(policy_context))

    return cloned


# ---------------------------------------------------------------------------
# Property 12: CanonicalValidationContext é estável
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_canonicalization_is_stable_for_equivalent_commands(
    command: CreateJournalEntryCommand,
) -> None:
    """
    Property 12: Para qualquer comando, canonicalizar duas vezes produz
    contextos idênticos.

    Garante que a canonicalização é determinística: o mesmo comando
    sempre produz o mesmo CanonicalValidationContext.

    **Validates: Requirements 8.5, 14.1**
    """
    builder = DefaultCanonicalValidationContextBuilder()

    # Canonicaliza o mesmo comando duas vezes
    context1 = builder.build(command)
    context2 = builder.build(command)

    # Os contextos devem ser idênticos (frozen dataclasses com igualdade por valor)
    assert context1 == context2


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_canonicalization_is_stable_for_semantically_equivalent_commands(
    command: CreateJournalEntryCommand,
) -> None:
    """
    Property 12 (variante): Para dois comandos semanticamente equivalentes
    (mesmos dados, objetos Python diferentes), o contexto canônico é idêntico.

    Garante que a igualdade do contexto não depende de identidade de objeto,
    apenas de conteúdo semântico.

    **Validates: Requirements 8.5, 14.1**
    """
    builder = DefaultCanonicalValidationContextBuilder()

    # Cria uma cópia semanticamente equivalente do comando
    cloned_command = _clone_command(command)

    context_original = builder.build(command)
    context_cloned = builder.build(cloned_command)

    # Contextos de comandos semanticamente equivalentes devem ser idênticos
    assert context_original == context_cloned


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_derived_facts_are_deterministic(
    command: CreateJournalEntryCommand,
) -> None:
    """
    Property: DerivedFacts calculados a partir do mesmo comando são sempre idênticos.

    Garante que o cálculo de fatos derivados é determinístico e não depende
    de estado externo ou ordem de execução.

    **Validates: Requirements 8.4, 8.5**
    """
    builder = DefaultCanonicalValidationContextBuilder()

    context1 = builder.build(command)
    context2 = builder.build(command)

    # DerivedFacts devem ser idênticos
    assert context1.facts == context2.facts
    assert context1.facts.posting_count == context2.facts.posting_count
    assert context1.facts.currencies == context2.facts.currencies
    assert context1.facts.max_posting_amount == context2.facts.max_posting_amount


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_policy_context_isolation_is_preserved_across_all_commands(
    command: CreateJournalEntryCommand,
) -> None:
    """
    Property: Para qualquer comando, policy_context nunca contém dados de metadata.

    Garante o isolamento estrito entre policy_context e metadata para
    todos os comandos possíveis, não apenas casos específicos.

    **Validates: Requirements 8.2**
    """
    builder = DefaultCanonicalValidationContextBuilder()
    context = builder.build(command)

    # Obtém as chaves do metadata do comando
    metadata_keys = set(getattr(command, "metadata", {}).keys())

    # Obtém as chaves do policy_context do contexto
    policy_context_keys = set(context.policy_context.keys())

    # Chaves que estão em metadata mas NÃO em policy_context do comando
    # (policy_context do comando pode ter chaves que também estão em metadata,
    # mas o contexto deve usar o valor de policy_context, não de metadata)
    # O que garantimos aqui é que metadata não "vaza" para policy_context
    # quando não há policy_context no comando
    policy_context_from_command = getattr(command, "policy_context", {})
    if not policy_context_from_command:
        # Se o comando não tem policy_context, o contexto deve ter policy_context vazio
        # (metadata não deve vazar)
        assert dict(context.policy_context) == {}


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_context_schema_version_is_always_present(
    command: CreateJournalEntryCommand,
) -> None:
    """
    Property: context_schema_version está sempre presente no contexto.

    Garante que o contexto sempre inclui a versão do schema para
    validação de compatibilidade com o bundle (Requisito 8.6).

    **Validates: Requirements 8.6**
    """
    builder = DefaultCanonicalValidationContextBuilder()
    context = builder.build(command)

    assert context.context_schema_version is not None
    assert len(context.context_schema_version) > 0


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_posting_count_invariant(
    command: CreateJournalEntryCommand,
) -> None:
    """
    Property: posting_count em DerivedFacts sempre iguala o número de postings.

    Invariante fundamental: facts.posting_count == len(context.postings).

    **Validates: Requirements 8.4**
    """
    builder = DefaultCanonicalValidationContextBuilder()
    context = builder.build(command)

    assert context.facts.posting_count == len(context.postings)


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_distinct_account_count_invariant(
    command: CreateJournalEntryCommand,
) -> None:
    """
    Property: distinct_account_count é sempre <= posting_count.

    Invariante: não pode haver mais contas distintas do que postings.

    **Validates: Requirements 8.4**
    """
    builder = DefaultCanonicalValidationContextBuilder()
    context = builder.build(command)

    assert context.facts.distinct_account_count <= context.facts.posting_count


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_currencies_invariant(
    command: CreateJournalEntryCommand,
) -> None:
    """
    Property: currencies em DerivedFacts contém apenas moedas presentes nas postings.

    Invariante: toda moeda em facts.currencies deve aparecer em ao menos uma posting.

    **Validates: Requirements 8.4**
    """
    builder = DefaultCanonicalValidationContextBuilder()
    context = builder.build(command)

    posting_currencies = {p.currency for p in context.postings}
    for currency in context.facts.currencies:
        assert currency in posting_currencies


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_max_posting_amount_invariant(
    command: CreateJournalEntryCommand,
) -> None:
    """
    Property: max_posting_amount é sempre >= ao amount de qualquer posting individual.

    Invariante: o máximo não pode ser menor que nenhum elemento.

    **Validates: Requirements 8.4**
    """
    builder = DefaultCanonicalValidationContextBuilder()
    context = builder.build(command)

    for posting in context.postings:
        assert context.facts.max_posting_amount >= posting.amount


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_total_debits_invariant(
    command: CreateJournalEntryCommand,
) -> None:
    """
    Property: total_debits_by_currency soma corretamente os débitos por moeda.

    Invariante: a soma dos débitos de uma moeda deve igualar o valor em
    total_debits_by_currency para aquela moeda.

    **Validates: Requirements 8.4**
    """
    builder = DefaultCanonicalValidationContextBuilder()
    context = builder.build(command)

    # Calcula manualmente os totais de débito por moeda
    expected_debits: dict[str, int] = {}
    for posting in context.postings:
        if posting.direction == "DEBIT":
            expected_debits[posting.currency] = (
                expected_debits.get(posting.currency, 0) + posting.amount
            )

    assert dict(context.facts.total_debits_by_currency) == expected_debits


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_context_is_always_frozen(
    command: CreateJournalEntryCommand,
) -> None:
    """
    Property: O contexto canônico é sempre imutável (frozen dataclass).

    Garante que nenhuma implementação do builder pode retornar um contexto mutável.

    **Validates: Requirements 8.5**
    """
    builder = DefaultCanonicalValidationContextBuilder()
    context = builder.build(command)

    # Verifica que o contexto é uma instância de CanonicalValidationContext
    assert isinstance(context, CanonicalValidationContext)

    # Verifica que tentativas de mutação falham
    with pytest.raises((AttributeError, TypeError)):
        context.external_id = "mutated"  # type: ignore[misc]
