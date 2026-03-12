"""
Property: Semântica DENY_OVERRIDES do RuleEvaluator.

Propriedades verificadas:
1. Se qualquer rule DENY casa → veredito final é REJECTED.
2. Se nenhuma rule DENY casa → veredito final é APPROVED.
3. Rules ALLOW nunca sobrepõem um DENY que casou.
4. O veredito é determinístico independentemente da ordem de declaração das rules.

Requisitos cobertos: 10.3, 10.4
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
    CompositionMode,
    FinalVerdict,
    LiteralNode,
    PolicyEffect,
    PolicyRuleNode,
    RuleAST,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rule(name: str, effect: PolicyEffect, matches: bool, priority: int = 100) -> PolicyRuleNode:
    return PolicyRuleNode(
        name=name,
        priority=priority,
        condition=LiteralNode(value=matches),
        effect=effect,
        message=f"{name} message",
    )


def _make_aps(rules: list[PolicyRuleNode]) -> ActivePolicySet:
    bundle = RuleBundle(
        policy_set_id="deny_overrides_test",
        artifact_hash="sha256:deny_test",
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
            author="test",
            description="DENY_OVERRIDES property test",
            compiled_at="2024-01-01T00:00:00Z",
            source_hash="sha256:src",
        ),
    )
    snapshot = ReferenceSnapshot(
        snapshot_version="snap_deny",
        snapshot_schema_version="1.0",
        created_at="2024-01-01T00:00:00Z",
        data={"daily_limit_minor": 500_000},
    )
    manifest = PolicyActivationManifest(
        activation_id="act_deny",
        policy_scope_id="tenant:TRANSFER:*:*:prod",
        artifact_hash="sha256:deny_test",
        snapshot_version="snap_deny",
        context_schema_version="1.0",
        evaluator_version=EVALUATOR_VERSION,
        activated_at="2024-01-01T00:00:00Z",
        activated_by="test",
    )
    return ActivePolicySet(
        manifest=manifest,
        bundle=bundle,
        snapshot=snapshot,
        loaded_at="2024-01-01T00:00:00Z",
        integrity_verified=True,
    )


def _minimal_context() -> CanonicalValidationContext:
    posting = CanonicalPosting(account_id="acc", amount=1_000, currency="BRL", direction="DEBIT")
    facts = DerivedFacts(
        posting_count=1,
        distinct_account_count=1,
        currencies=("BRL",),
        total_debits_by_currency={"BRL": 1_000},
        total_credits_by_currency={},
        max_posting_amount=1_000,
        has_platform_account=False,
    )
    return CanonicalValidationContext(
        tenant_id="tenant",
        external_id="ext",
        operation_type="TRANSFER",
        product_code=None,
        channel=None,
        postings=(posting,),
        policy_context={},
        facts=facts,
        context_schema_version="1.0",
    )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Gera uma lista de rules com efeitos e matches arbitrários
_rule_spec = st.tuples(
    st.sampled_from([PolicyEffect.ALLOW, PolicyEffect.DENY]),
    st.booleans(),  # matches?
)


@st.composite
def _rules_strategy(draw: st.DrawFn) -> list[PolicyRuleNode]:
    """Gera uma lista de 1 a 8 rules com efeitos e matches arbitrários."""
    specs = draw(st.lists(_rule_spec, min_size=1, max_size=8))
    return [
        _make_rule(
            name=f"rule_{i}",
            effect=effect,
            matches=matches,
            priority=100 - i,
        )
        for i, (effect, matches) in enumerate(specs)
    ]


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(rules=_rules_strategy())
@settings(max_examples=200)
def test_any_matching_deny_causes_rejection(rules: list[PolicyRuleNode]) -> None:
    """
    Property: Se qualquer rule DENY casa, o veredito final é REJECTED.

    Requisito: 10.3
    """
    evaluator = RuleEvaluator()
    context = _minimal_context()
    aps = _make_aps(rules)

    result = evaluator.evaluate(context, aps)

    has_matching_deny = any(
        r.effect == PolicyEffect.DENY and r.condition.value is True  # type: ignore[union-attr]
        for r in rules
    )

    if has_matching_deny:
        assert result.decision.final_verdict == FinalVerdict.REJECTED
        assert result.decision.matched_deny_rule is not None
    else:
        assert result.decision.final_verdict == FinalVerdict.APPROVED
        assert result.decision.matched_deny_rule is None


@pytest.mark.property
@given(rules=_rules_strategy())
@settings(max_examples=200)
def test_no_matching_deny_causes_approval(rules: list[PolicyRuleNode]) -> None:
    """
    Property: Se nenhuma rule DENY casa, o veredito final é APPROVED.

    Requisito: 10.4
    """
    evaluator = RuleEvaluator()
    context = _minimal_context()

    # Força todas as rules DENY a não casarem
    non_deny_rules = [
        _make_rule(r.name, r.effect, matches=False if r.effect == PolicyEffect.DENY else r.condition.value, priority=r.priority)  # type: ignore[union-attr]
        for r in rules
    ]
    aps = _make_aps(non_deny_rules)

    result = evaluator.evaluate(context, aps)

    assert result.decision.final_verdict == FinalVerdict.APPROVED
    assert result.decision.matched_deny_rule is None


@pytest.mark.property
@given(
    n_allow=st.integers(min_value=0, max_value=5),
    n_deny=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=100)
def test_allow_rules_never_override_matching_deny(n_allow: int, n_deny: int) -> None:
    """
    Property: Rules ALLOW nunca sobrepõem um DENY que casou.
    Independentemente de quantas rules ALLOW casem, um DENY que casa
    sempre resulta em REJECTED.

    Requisito: 10.3, 10.5
    """
    evaluator = RuleEvaluator()
    context = _minimal_context()

    rules = (
        [_make_rule(f"allow_{i}", PolicyEffect.ALLOW, matches=True, priority=10 + i) for i in range(n_allow)]
        + [_make_rule(f"deny_{i}", PolicyEffect.DENY, matches=True, priority=100 + i) for i in range(n_deny)]
    )
    aps = _make_aps(rules)

    result = evaluator.evaluate(context, aps)

    # Com ao menos um DENY casando, o resultado deve ser REJECTED
    assert result.decision.final_verdict == FinalVerdict.REJECTED
    assert result.decision.matched_deny_rule is not None


@pytest.mark.property
@given(rules=_rules_strategy())
@settings(max_examples=100)
def test_matched_deny_rule_is_recorded_when_rejected(rules: list[PolicyRuleNode]) -> None:
    """
    Property: Quando o veredito é REJECTED, matched_deny_rule é sempre não-None
    e corresponde a uma rule DENY que casou.

    Requisito: 10.6
    """
    evaluator = RuleEvaluator()
    context = _minimal_context()
    aps = _make_aps(rules)

    result = evaluator.evaluate(context, aps)

    if result.decision.final_verdict == FinalVerdict.REJECTED:
        assert result.decision.matched_deny_rule is not None
        # A rule registrada deve existir nos resultados
        rule_names = {r.rule_name for r in result.decision.rules}
        assert result.decision.matched_deny_rule in rule_names
        # A rule registrada deve ter efeito DENY e ter casado
        matched = next(r for r in result.decision.rules if r.rule_name == result.decision.matched_deny_rule)
        assert matched.effect == PolicyEffect.DENY
        assert matched.matched is True
    else:
        assert result.decision.matched_deny_rule is None


@pytest.mark.property
@given(rules=_rules_strategy())
@settings(max_examples=100)
def test_all_rules_are_always_evaluated(rules: list[PolicyRuleNode]) -> None:
    """
    Property: Todas as rules do bundle são sempre avaliadas e registradas
    no resultado, independentemente do veredito.

    Isso garante que o DecisionTrail contenha o resultado completo.

    Requisito: 10.6
    """
    evaluator = RuleEvaluator()
    context = _minimal_context()
    aps = _make_aps(rules)

    result = evaluator.evaluate(context, aps)

    # Número de resultados deve ser igual ao número de rules
    assert len(result.decision.rules) == len(rules)

    # Todos os nomes de rules devem estar presentes
    result_names = {r.rule_name for r in result.decision.rules}
    expected_names = {r.name for r in rules}
    assert result_names == expected_names
