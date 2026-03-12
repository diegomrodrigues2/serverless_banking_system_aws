"""
Property 3: Determinismo semântico do RuleEvaluator.

Para todo CanonicalValidationContext e ActivePolicySet, duas avaliações
devem produzir a mesma EvaluationDecision. Campos efêmeros como latência
não participam da igualdade semântica.

Requisitos cobertos: 9.3, 10.3
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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
    AggregateNode,
    CollectionRefNode,
    CompositionMode,
    ComparisonNode,
    FieldAccessNode,
    LiteralNode,
    LogicalOpNode,
    NotOpNode,
    PolicyEffect,
    PolicyRuleNode,
    PredicateNode,
    RefAccessNode,
    RuleAST,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_identifier = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu"), whitelist_characters="_"),
    min_size=1,
    max_size=15,
)

_small_int = st.integers(min_value=0, max_value=1_000_000)
_currency = st.sampled_from(["BRL", "USD", "EUR"])
_direction = st.sampled_from(["DEBIT", "CREDIT"])


@st.composite
def _posting_strategy(draw: st.DrawFn) -> CanonicalPosting:
    return CanonicalPosting(
        account_id=draw(_identifier),
        amount=draw(_small_int),
        currency=draw(_currency),
        direction=draw(_direction),
    )


@st.composite
def _context_strategy(draw: st.DrawFn) -> CanonicalValidationContext:
    """Gera um CanonicalValidationContext arbitrário mas válido."""
    postings = draw(st.lists(_posting_strategy(), min_size=1, max_size=6))
    posting_tuple = tuple(postings)

    debit_total = sum(p.amount for p in postings if p.direction == "DEBIT")
    credit_total = sum(p.amount for p in postings if p.direction == "CREDIT")
    currencies = tuple(sorted({p.currency for p in postings}))
    distinct_accounts = len({p.account_id for p in postings})
    max_amount = max(p.amount for p in postings)

    facts = DerivedFacts(
        posting_count=len(postings),
        distinct_account_count=distinct_accounts,
        currencies=currencies,
        total_debits_by_currency={"BRL": debit_total} if debit_total else {},
        total_credits_by_currency={"BRL": credit_total} if credit_total else {},
        max_posting_amount=max_amount,
        has_platform_account=draw(st.booleans()),
    )
    return CanonicalValidationContext(
        tenant_id=draw(_identifier),
        external_id=draw(_identifier),
        operation_type="TRANSFER",
        product_code=None,
        channel=None,
        postings=posting_tuple,
        policy_context={"daily_limit_minor": draw(_small_int)},
        facts=facts,
        context_schema_version="1.0",
    )


def _make_simple_rule(name: str, effect: PolicyEffect, always_match: bool) -> PolicyRuleNode:
    """Cria uma rule simples que sempre casa ou nunca casa."""
    return PolicyRuleNode(
        name=name,
        priority=100,
        condition=LiteralNode(value=always_match),
        effect=effect,
        message=f"Rule {name}",
    )


def _make_active_policy_set(rules: list[PolicyRuleNode]) -> ActivePolicySet:
    bundle = RuleBundle(
        policy_set_id="prop_test_bundle",
        artifact_hash="sha256:prop_test",
        ast=RuleAST(rules=tuple(rules), composition_mode=CompositionMode.DENY_OVERRIDES),
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
        data={"daily_limit_minor": 500_000, "blocked_accounts": ()},
    )
    manifest = PolicyActivationManifest(
        activation_id="act_prop",
        policy_scope_id="tenant:TRANSFER:*:*:prod",
        artifact_hash="sha256:prop_test",
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


# ---------------------------------------------------------------------------
# Property: Determinismo semântico
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(context=_context_strategy())
@settings(max_examples=100)
def test_evaluator_is_deterministic_for_same_inputs(context: CanonicalValidationContext) -> None:
    """
    Property 3: Para qualquer contexto válido, duas avaliações com o mesmo
    bundle produzem a mesma EvaluationDecision.

    Campos efêmeros (latência) não participam da igualdade semântica.
    Requisito: 9.3
    """
    evaluator = RuleEvaluator()
    rules = [
        _make_simple_rule("deny_always", PolicyEffect.DENY, always_match=True),
        _make_simple_rule("allow_always", PolicyEffect.ALLOW, always_match=True),
    ]
    aps = _make_active_policy_set(rules)

    result1 = evaluator.evaluate(context, aps)
    result2 = evaluator.evaluate(context, aps)

    # A decisão semântica deve ser idêntica
    assert result1.decision == result2.decision
    assert result1.decision.final_verdict == result2.decision.final_verdict
    assert result1.decision.matched_deny_rule == result2.decision.matched_deny_rule


@pytest.mark.property
@given(
    context=_context_strategy(),
    always_deny=st.booleans(),
)
@settings(max_examples=100)
def test_verdict_is_stable_across_repeated_evaluations(
    context: CanonicalValidationContext,
    always_deny: bool,
) -> None:
    """
    Property: O veredito é estável — avaliações repetidas com os mesmos inputs
    produzem sempre o mesmo veredito final.

    Requisito: 9.3
    """
    evaluator = RuleEvaluator()
    rules = [_make_simple_rule("rule_a", PolicyEffect.DENY, always_match=always_deny)]
    aps = _make_active_policy_set(rules)

    verdicts = [evaluator.evaluate(context, aps).decision.final_verdict for _ in range(3)]

    # Todos os vereditos devem ser iguais
    assert len(set(verdicts)) == 1


@pytest.mark.property
@given(context=_context_strategy())
@settings(max_examples=100)
def test_metrics_do_not_affect_semantic_decision(context: CanonicalValidationContext) -> None:
    """
    Property: Métricas (latência) são separadas da decisão semântica.
    Duas avaliações com os mesmos inputs produzem a mesma decisão,
    independentemente das métricas coletadas.

    Requisito: 9.3, 9.4
    """
    evaluator = RuleEvaluator()
    aps = _make_active_policy_set([_make_simple_rule("deny", PolicyEffect.DENY, always_match=False)])

    r1 = evaluator.evaluate(context, aps)
    r2 = evaluator.evaluate(context, aps)

    # Decisões semânticas idênticas
    assert r1.decision == r2.decision
    # Latências podem diferir (são efêmeras) — não testamos igualdade de latência
    assert r1.metrics.evaluated_rules == r2.metrics.evaluated_rules
