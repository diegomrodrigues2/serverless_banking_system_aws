"""
Property: Isolamento de falha do DecisionTrailEmitter.

Para qualquer transação aprovada, falha na emissão do DecisionTrail
NÃO deve afetar o resultado da validação. A transação aprovada deve
continuar válida independentemente do estado do emitter.

Esta property verifica o Requisito 13.4: falha de emissão do trail
não invalida a transação aprovada.

Requisitos cobertos: 7.5, 13.4
"""

from __future__ import annotations

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
    LiteralNode,
    PolicyEffect,
    PolicyRuleNode,
    RuleAST,
)
from validation_engine.infrastructure.decision_trail_emitter import (
    FirehoseDecisionTrailEmitter,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_identifier = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu"), whitelist_characters="_"),
    min_size=1,
    max_size=20,
)

_small_int = st.integers(min_value=0, max_value=1_000_000)

# Estratégia para simular diferentes tipos de falha do Firehose
_firehose_error_strategy = st.sampled_from([
    RuntimeError("Firehose indisponível"),
    ConnectionError("Timeout de rede"),
    ValueError("Payload inválido"),
    Exception("Erro genérico"),
])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_approving_active_policy_set() -> ActivePolicySet:
    """Cria um ActivePolicySet que sempre aprova."""
    rule = PolicyRuleNode(
        name="allow_always",
        priority=100,
        condition=LiteralNode(value=False),  # condição DENY nunca satisfeita
        effect=PolicyEffect.DENY,
        message="Never matches",
    )
    bundle = RuleBundle(
        policy_set_id="prop_trail_test",
        artifact_hash="sha256:trail_prop",
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
            description="Trail isolation property test bundle",
            compiled_at="2024-01-01T00:00:00Z",
            source_hash="sha256:src",
        ),
    )
    snapshot = ReferenceSnapshot(
        snapshot_version="snap_trail_prop",
        snapshot_schema_version="1.0",
        created_at="2024-01-01T00:00:00Z",
        data={"daily_limit_minor": 500_000},
    )
    manifest = PolicyActivationManifest(
        activation_id="act_trail_prop",
        policy_scope_id="tenant:TRANSFER:*:*:prod",
        artifact_hash="sha256:trail_prop",
        snapshot_version="snap_trail_prop",
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


def _make_context(external_id: str, tenant_id: str) -> CanonicalValidationContext:
    """Cria um contexto canônico mínimo para testes de property."""
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
        tenant_id=tenant_id,
        external_id=external_id,
        operation_type="TRANSFER",
        product_code=None,
        channel=None,
        postings=postings,
        policy_context={},
        facts=facts,
        context_schema_version="1.0",
    )


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(
    external_id=_identifier,
    tenant_id=_identifier,
    firehose_error=_firehose_error_strategy,
)
@settings(max_examples=100)
def test_falha_do_emitter_nao_afeta_resultado_da_validacao(
    external_id: str,
    tenant_id: str,
    firehose_error: Exception,
) -> None:
    """
    Property: Falha na emissão do DecisionTrail não afeta o resultado da validação.

    Para qualquer transação aprovada e qualquer tipo de falha do Firehose,
    o resultado da validação deve ser is_valid=True.

    Requisito: 13.4
    """
    # Configura Firehose que sempre falha com o erro gerado pela strategy
    mock_firehose_client = MagicMock()
    mock_firehose_client.put_record.side_effect = firehose_error

    firehose_emitter = FirehoseDecisionTrailEmitter(
        firehose_client=mock_firehose_client,
        delivery_stream_name="test-stream",
    )

    # Configura context builder e registry
    context = _make_context(external_id=external_id, tenant_id=tenant_id)
    mock_context_builder = MagicMock()
    mock_context_builder.build.return_value = context

    mock_registry = MagicMock()
    mock_registry.get_active_policy_set.return_value = _make_approving_active_policy_set()

    facade = PolicyValidationFacade(
        context_builder=mock_context_builder,
        runtime_registry=mock_registry,
        evaluator=RuleEvaluator(),
        trail_emitter=firehose_emitter,
    )

    # A validação deve ser bem-sucedida mesmo com o Firehose falhando
    result = facade.validate(object())

    assert result.is_valid is True


@pytest.mark.property
@given(
    external_id=_identifier,
    tenant_id=_identifier,
)
@settings(max_examples=50)
def test_falha_do_emitter_nao_levanta_excecao(
    external_id: str,
    tenant_id: str,
) -> None:
    """
    Property: Falha na emissão do DecisionTrail não levanta exceção ao chamador.

    Para qualquer transação aprovada, mesmo que o emitter falhe com
    qualquer tipo de exceção, validate() não deve propagar essa exceção.

    Requisito: 13.4
    """
    # Emitter que falha com exceção genérica
    mock_firehose_client = MagicMock()
    mock_firehose_client.put_record.side_effect = Exception("Falha genérica")

    firehose_emitter = FirehoseDecisionTrailEmitter(
        firehose_client=mock_firehose_client,
        delivery_stream_name="test-stream",
    )

    context = _make_context(external_id=external_id, tenant_id=tenant_id)
    mock_context_builder = MagicMock()
    mock_context_builder.build.return_value = context

    mock_registry = MagicMock()
    mock_registry.get_active_policy_set.return_value = _make_approving_active_policy_set()

    facade = PolicyValidationFacade(
        context_builder=mock_context_builder,
        runtime_registry=mock_registry,
        evaluator=RuleEvaluator(),
        trail_emitter=firehose_emitter,
    )

    # Não deve levantar nenhuma exceção
    try:
        facade.validate(object())
    except Exception as exc:
        pytest.fail(
            f"validate() levantou exceção inesperada quando o emitter falhou: {exc}"
        )
