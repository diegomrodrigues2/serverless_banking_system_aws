"""
Property: Imutabilidade do comando no pipeline da PolicyValidationFacade.

Para qualquer comando válido, o pipeline da facade NÃO deve mutar
nenhum campo do comando original — nem em aprovação, nem em rejeição.

Esta property verifica o Requisito 7.5: a facade não muta o comando.

Requisitos cobertos: 7.5
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Mapping
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from validation_engine.application.facade import PolicyValidationFacade
from validation_engine.domain.context import (
    CanonicalPosting,
    CanonicalValidationContext,
    DerivedFacts,
)
from validation_engine.domain.errors import PolicyRejected
from validation_engine.domain.evaluator import EVALUATOR_VERSION, RuleEvaluator
from validation_engine.domain.models import (
    ActivePolicySet,
    BundleCompatibility,
    CompilationMetadata,
    PolicyActivationManifest,
    ReferenceSnapshot,
    RuleBundle,
)
from validation_engine.domain.policy_ast import (
    CompositionMode,
    FinalVerdict,
    LiteralNode,
    PolicyEffect,
    PolicyRuleNode,
    RuleAST,
)
from validation_engine.infrastructure.decision_trail_emitter import NoOpDecisionTrailEmitter

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_identifier = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu"), whitelist_characters="_"),
    min_size=1,
    max_size=20,
)

_small_int = st.integers(min_value=0, max_value=1_000_000)


@dataclass
class _MutableCommand:
    """
    Comando mutável para verificar que a facade não altera seus campos.

    Todos os campos são mutáveis para que a property possa detectar
    qualquer mutação acidental pelo pipeline.
    """

    external_id: str
    tenant_id: str
    operation_type: str
    product_code: str | None
    channel: str | None
    policy_context: dict
    metadata: dict


@st.composite
def _command_strategy(draw: st.DrawFn) -> _MutableCommand:
    """Gera um comando mutável arbitrário para testes de property."""
    return _MutableCommand(
        external_id=draw(_identifier),
        tenant_id=draw(_identifier),
        operation_type=draw(st.sampled_from(["TRANSFER", "PAYMENT", "REVERSAL"])),
        product_code=draw(st.one_of(st.none(), st.sampled_from(["PIX", "TED", "BOLETO"]))),
        channel=draw(st.one_of(st.none(), st.sampled_from(["MOBILE", "API", "BRANCH"]))),
        policy_context={"daily_limit_minor": draw(_small_int)},
        metadata={"trace_id": draw(_identifier)},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_active_policy_set(always_deny: bool) -> ActivePolicySet:
    """Cria um ActivePolicySet que sempre aprova ou sempre nega."""
    rule = PolicyRuleNode(
        name="test_rule",
        priority=100,
        condition=LiteralNode(value=always_deny),
        effect=PolicyEffect.DENY if always_deny else PolicyEffect.ALLOW,
        message="Test rule",
    )
    bundle = RuleBundle(
        policy_set_id="prop_test",
        artifact_hash="sha256:prop",
        ast=RuleAST(rules=(rule,), composition_mode=CompositionMode.DENY_OVERRIDES),
        execution_plan={},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version="1.0",
            snapshot_schema_version="1.0",
            evaluator_min_version="1.0.0",
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author="prop_test",
            description="Property test bundle",
            compiled_at="2024-01-01T00:00:00Z",
            source_hash="sha256:src",
        ),
    )
    snapshot = ReferenceSnapshot(
        snapshot_version="snap_prop",
        snapshot_schema_version="1.0",
        created_at="2024-01-01T00:00:00Z",
        data={"daily_limit_minor": 500_000},
    )
    manifest = PolicyActivationManifest(
        activation_id="act_prop",
        policy_scope_id="tenant:TRANSFER:*:*:prod",
        artifact_hash="sha256:prop",
        snapshot_version="snap_prop",
        context_schema_version="1.0",
        evaluator_version=EVALUATOR_VERSION,
        activated_at="2024-01-01T00:00:00Z",
        activated_by="prop_test",
    )
    return ActivePolicySet(
        manifest=manifest,
        bundle=bundle,
        snapshot=snapshot,
        loaded_at="2024-01-01T00:00:00Z",
        integrity_verified=True,
    )


def _make_context_from_command(command: _MutableCommand) -> CanonicalValidationContext:
    """Constrói um contexto canônico a partir do comando mutável."""
    postings = (
        CanonicalPosting(
            account_id="acc_debit",
            amount=10000,
            currency="BRL",
            direction="DEBIT",
        ),
    )
    facts = DerivedFacts(
        posting_count=1,
        distinct_account_count=1,
        currencies=("BRL",),
        total_debits_by_currency={"BRL": 10000},
        total_credits_by_currency={},
        max_posting_amount=10000,
        has_platform_account=False,
    )
    return CanonicalValidationContext(
        tenant_id=command.tenant_id,
        external_id=command.external_id,
        operation_type=command.operation_type,
        product_code=command.product_code,
        channel=command.channel,
        postings=postings,
        policy_context=command.policy_context,
        facts=facts,
        context_schema_version="1.0",
    )


def _make_facade(always_deny: bool) -> tuple[PolicyValidationFacade, NoOpDecisionTrailEmitter]:
    """Cria uma facade configurada para sempre aprovar ou sempre negar."""
    aps = _make_active_policy_set(always_deny=always_deny)

    mock_context_builder = MagicMock()
    mock_registry = MagicMock()
    mock_registry.get_active_policy_set.return_value = aps
    emitter = NoOpDecisionTrailEmitter()

    facade = PolicyValidationFacade(
        context_builder=mock_context_builder,
        runtime_registry=mock_registry,
        evaluator=RuleEvaluator(),
        trail_emitter=emitter,
    )
    return facade, emitter


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_comando_nao_e_mutado_em_aprovacao(command: _MutableCommand) -> None:
    """
    Property: O comando original não é mutado quando a policy aprova.

    Para qualquer comando válido, todos os campos do comando devem ser
    idênticos antes e depois da chamada a validate() quando aprovado.

    Requisito: 7.5
    """
    facade, emitter = _make_facade(always_deny=False)

    # Captura o estado original do comando antes da validação
    original_state = copy.deepcopy(command)

    # Configura o context builder para retornar um contexto baseado no comando
    facade._context_builder.build.return_value = _make_context_from_command(command)

    facade.validate(command)

    # Verifica que nenhum campo foi mutado
    assert command.external_id == original_state.external_id
    assert command.tenant_id == original_state.tenant_id
    assert command.operation_type == original_state.operation_type
    assert command.product_code == original_state.product_code
    assert command.channel == original_state.channel
    assert command.policy_context == original_state.policy_context
    assert command.metadata == original_state.metadata


@pytest.mark.property
@given(command=_command_strategy())
@settings(max_examples=100)
def test_comando_nao_e_mutado_em_rejeicao(command: _MutableCommand) -> None:
    """
    Property: O comando original não é mutado quando a policy rejeita.

    Para qualquer comando válido, todos os campos do comando devem ser
    idênticos antes e depois da chamada a validate() quando rejeitado.

    Requisito: 7.5
    """
    facade, emitter = _make_facade(always_deny=True)

    # Captura o estado original do comando antes da validação
    original_state = copy.deepcopy(command)

    # Configura o context builder para retornar um contexto baseado no comando
    facade._context_builder.build.return_value = _make_context_from_command(command)

    with pytest.raises(PolicyRejected):
        facade.validate(command)

    # Verifica que nenhum campo foi mutado mesmo após rejeição
    assert command.external_id == original_state.external_id
    assert command.tenant_id == original_state.tenant_id
    assert command.operation_type == original_state.operation_type
    assert command.product_code == original_state.product_code
    assert command.channel == original_state.channel
    assert command.policy_context == original_state.policy_context
    assert command.metadata == original_state.metadata
