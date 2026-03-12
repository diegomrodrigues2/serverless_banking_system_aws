"""
Testes unitários para o RuleEvaluator.

Cobre os cenários principais de avaliação:
- Aprovação (nenhuma rule DENY casa)
- Rejeição (ao menos uma rule DENY casa)
- Semântica DENY_OVERRIDES (DENY prevalece sobre ALLOW)
- Agregações: SUM, COUNT, MIN, MAX, ANY, ALL
- Resolução de namespaces: facts.*, policy_context.*, ref.*
- Incompatibilidade de bundle (versão, schema, integridade)
- Erros de avaliação (namespace inválido, operador desconhecido)

Requisitos cobertos: 9.1, 9.3, 10.3, 10.4, 10.5
"""
from __future__ import annotations

import pytest

from validation_engine.domain.context import (
    CanonicalPosting,
    CanonicalValidationContext,
    DerivedFacts,
)
from validation_engine.domain.errors import InvalidPolicyBundle, PolicyEvaluationError
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
    FinalVerdict,
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
# Helpers de construção de fixtures
# ---------------------------------------------------------------------------


def _make_compatibility(
    context_schema_version: str = "1.0",
    evaluator_min_version: str = "1.0.0",
) -> BundleCompatibility:
    return BundleCompatibility(
        dsl_version="1.0",
        context_schema_version=context_schema_version,
        snapshot_schema_version="1.0",
        evaluator_min_version=evaluator_min_version,
    )


def _make_metadata() -> CompilationMetadata:
    return CompilationMetadata(
        author="test",
        description="Test bundle",
        compiled_at="2024-01-01T00:00:00Z",
        source_hash="sha256:test",
    )


def _make_manifest(policy_scope_id: str = "tenant:TRANSFER:*:*:prod") -> PolicyActivationManifest:
    return PolicyActivationManifest(
        activation_id="act_001",
        policy_scope_id=policy_scope_id,
        artifact_hash="sha256:abc",
        snapshot_version="snap_001",
        context_schema_version="1.0",
        evaluator_version=EVALUATOR_VERSION,
        activated_at="2024-01-01T00:00:00Z",
        activated_by="test",
    )


def _make_snapshot(data: dict | None = None) -> ReferenceSnapshot:
    return ReferenceSnapshot(
        snapshot_version="snap_001",
        snapshot_schema_version="1.0",
        created_at="2024-01-01T00:00:00Z",
        data=data or {"daily_limit_minor": 500_000, "blocked_accounts": ()},
    )


def _make_bundle(rules: list[PolicyRuleNode], compat: BundleCompatibility | None = None) -> RuleBundle:
    return RuleBundle(
        policy_set_id="test_bundle",
        artifact_hash="sha256:abc",
        ast=RuleAST(rules=tuple(rules), composition_mode=CompositionMode.DENY_OVERRIDES),
        execution_plan={},
        compatibility=compat or _make_compatibility(),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=_make_metadata(),
    )


def _make_active_policy_set(
    rules: list[PolicyRuleNode],
    snapshot_data: dict | None = None,
    compat: BundleCompatibility | None = None,
    integrity_verified: bool = True,
) -> ActivePolicySet:
    bundle = _make_bundle(rules, compat)
    snapshot = _make_snapshot(snapshot_data)
    return ActivePolicySet(
        manifest=_make_manifest(),
        bundle=bundle,
        snapshot=snapshot,
        loaded_at="2024-01-01T00:00:00Z",
        integrity_verified=integrity_verified,
    )


def _make_context(
    postings: list[CanonicalPosting] | None = None,
    policy_context: dict | None = None,
    context_schema_version: str = "1.0",
) -> CanonicalValidationContext:
    """Constrói um contexto canônico mínimo para testes."""
    if postings is None:
        postings = [
            CanonicalPosting(account_id="acc_debit", amount=10_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=10_000, currency="BRL", direction="CREDIT"),
        ]
    posting_count = len(postings)
    debit_total = sum(p.amount for p in postings if p.direction == "DEBIT")
    credit_total = sum(p.amount for p in postings if p.direction == "CREDIT")
    currencies = tuple(sorted({p.currency for p in postings}))
    distinct_accounts = len({p.account_id for p in postings})
    max_amount = max((p.amount for p in postings), default=0)

    facts = DerivedFacts(
        posting_count=posting_count,
        distinct_account_count=distinct_accounts,
        currencies=currencies,
        total_debits_by_currency={"BRL": debit_total} if debit_total else {},
        total_credits_by_currency={"BRL": credit_total} if credit_total else {},
        max_posting_amount=max_amount,
        has_platform_account=False,
    )
    return CanonicalValidationContext(
        tenant_id="tenant_test",
        external_id="ext_001",
        operation_type="TRANSFER",
        product_code="PIX",
        channel="MOBILE",
        postings=tuple(postings),
        policy_context=policy_context or {"daily_limit_minor": 500_000},
        facts=facts,
        context_schema_version=context_schema_version,
    )


# ---------------------------------------------------------------------------
# Fixtures pytest
# ---------------------------------------------------------------------------


@pytest.fixture
def evaluator() -> RuleEvaluator:
    return RuleEvaluator()


@pytest.fixture
def simple_deny_rule() -> PolicyRuleNode:
    """Rule que nega quando posting_count == 0."""
    return PolicyRuleNode(
        name="deny_empty",
        priority=100,
        condition=ComparisonNode(
            left=FieldAccessNode(path=("facts", "posting_count")),
            operator="==",
            right=LiteralNode(value=0),
        ),
        effect=PolicyEffect.DENY,
        message="No postings",
    )


@pytest.fixture
def simple_allow_rule() -> PolicyRuleNode:
    """Rule que aprova quando posting_count >= 2."""
    return PolicyRuleNode(
        name="allow_standard",
        priority=10,
        condition=ComparisonNode(
            left=FieldAccessNode(path=("facts", "posting_count")),
            operator=">=",
            right=LiteralNode(value=2),
        ),
        effect=PolicyEffect.ALLOW,
        message="Standard flow",
    )


# ---------------------------------------------------------------------------
# Testes de aprovação e rejeição básica
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBasicVerdicts:
    """Testa os vereditos básicos de aprovação e rejeição."""

    def test_approve_when_no_deny_rule_matches(self, evaluator: RuleEvaluator) -> None:
        """Nenhuma rule DENY casa → APPROVED."""
        # Rule DENY que nunca casa (posting_count == 0, mas temos 2 postings)
        deny_rule = PolicyRuleNode(
            name="deny_empty",
            priority=100,
            condition=ComparisonNode(
                left=FieldAccessNode(path=("facts", "posting_count")),
                operator="==",
                right=LiteralNode(value=0),
            ),
            effect=PolicyEffect.DENY,
            message="No postings",
        )
        aps = _make_active_policy_set([deny_rule])
        context = _make_context()

        result = evaluator.evaluate(context, aps)

        assert result.decision.final_verdict == FinalVerdict.APPROVED
        assert result.decision.matched_deny_rule is None

    def test_reject_when_deny_rule_matches(self, evaluator: RuleEvaluator) -> None:
        """Rule DENY casa → REJECTED com nome da rule registrado."""
        # Contexto com apenas 1 posting (debit sem credit)
        postings = [CanonicalPosting(account_id="acc", amount=5_000, currency="BRL", direction="DEBIT")]
        context = _make_context(postings=postings)

        # Rule que nega quando posting_count < 2
        deny_rule = PolicyRuleNode(
            name="deny_unbalanced",
            priority=100,
            condition=ComparisonNode(
                left=FieldAccessNode(path=("facts", "posting_count")),
                operator="<",
                right=LiteralNode(value=2),
            ),
            effect=PolicyEffect.DENY,
            message="Must have at least 2 postings",
        )
        aps = _make_active_policy_set([deny_rule])

        result = evaluator.evaluate(context, aps)

        assert result.decision.final_verdict == FinalVerdict.REJECTED
        assert result.decision.matched_deny_rule == "deny_unbalanced"

    def test_approve_with_only_allow_rules(self, evaluator: RuleEvaluator) -> None:
        """Apenas rules ALLOW → APPROVED (ALLOW não rejeita)."""
        allow_rule = PolicyRuleNode(
            name="allow_brl",
            priority=10,
            condition=ComparisonNode(
                left=FieldAccessNode(path=("facts", "posting_count")),
                operator=">=",
                right=LiteralNode(value=1),
            ),
            effect=PolicyEffect.ALLOW,
            message="BRL flow",
        )
        aps = _make_active_policy_set([allow_rule])
        context = _make_context()

        result = evaluator.evaluate(context, aps)

        assert result.decision.final_verdict == FinalVerdict.APPROVED

    def test_all_rules_are_evaluated_and_recorded(self, evaluator: RuleEvaluator) -> None:
        """Todas as rules são avaliadas e registradas no resultado."""
        rules = [
            PolicyRuleNode(
                name="rule_a",
                priority=100,
                condition=LiteralNode(value=True),
                effect=PolicyEffect.ALLOW,
                message="Always allow",
            ),
            PolicyRuleNode(
                name="rule_b",
                priority=50,
                condition=LiteralNode(value=False),
                effect=PolicyEffect.DENY,
                message="Never deny",
            ),
        ]
        aps = _make_active_policy_set(rules)
        context = _make_context()

        result = evaluator.evaluate(context, aps)

        rule_names = {r.rule_name for r in result.decision.rules}
        assert rule_names == {"rule_a", "rule_b"}
        assert result.metrics.evaluated_rules == 2


# ---------------------------------------------------------------------------
# Testes de semântica DENY_OVERRIDES
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDenyOverrides:
    """Testa a semântica DENY_OVERRIDES: DENY prevalece sobre ALLOW."""

    def test_deny_overrides_allow(self, evaluator: RuleEvaluator) -> None:
        """DENY que casa prevalece mesmo quando ALLOW também casa."""
        rules = [
            PolicyRuleNode(
                name="allow_standard",
                priority=10,
                condition=LiteralNode(value=True),  # sempre casa
                effect=PolicyEffect.ALLOW,
                message="Standard",
            ),
            PolicyRuleNode(
                name="deny_blocked",
                priority=100,
                condition=LiteralNode(value=True),  # sempre casa
                effect=PolicyEffect.DENY,
                message="Blocked",
            ),
        ]
        aps = _make_active_policy_set(rules)
        context = _make_context()

        result = evaluator.evaluate(context, aps)

        assert result.decision.final_verdict == FinalVerdict.REJECTED
        assert result.decision.matched_deny_rule == "deny_blocked"

    def test_first_deny_by_priority_is_recorded(self, evaluator: RuleEvaluator) -> None:
        """Quando múltiplas rules DENY casam, a de maior prioridade é registrada."""
        rules = [
            PolicyRuleNode(
                name="deny_low_priority",
                priority=10,
                condition=LiteralNode(value=True),
                effect=PolicyEffect.DENY,
                message="Low priority deny",
            ),
            PolicyRuleNode(
                name="deny_high_priority",
                priority=200,
                condition=LiteralNode(value=True),
                effect=PolicyEffect.DENY,
                message="High priority deny",
            ),
        ]
        aps = _make_active_policy_set(rules)
        context = _make_context()

        result = evaluator.evaluate(context, aps)

        # A rule de maior prioridade (200) é avaliada primeiro e registrada
        assert result.decision.matched_deny_rule == "deny_high_priority"

    def test_deny_not_matching_does_not_reject(self, evaluator: RuleEvaluator) -> None:
        """Rule DENY que não casa não rejeita a transação."""
        rules = [
            PolicyRuleNode(
                name="deny_never",
                priority=100,
                condition=LiteralNode(value=False),  # nunca casa
                effect=PolicyEffect.DENY,
                message="Never",
            ),
        ]
        aps = _make_active_policy_set(rules)
        context = _make_context()

        result = evaluator.evaluate(context, aps)

        assert result.decision.final_verdict == FinalVerdict.APPROVED
        assert result.decision.matched_deny_rule is None


# ---------------------------------------------------------------------------
# Testes de agregações
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAggregations:
    """Testa as funções de agregação: SUM, COUNT, MIN, MAX, ANY, ALL."""

    def _deny_when(self, condition, name: str = "deny_agg") -> PolicyRuleNode:
        return PolicyRuleNode(
            name=name,
            priority=100,
            condition=condition,
            effect=PolicyEffect.DENY,
            message="Aggregation deny",
        )

    def test_sum_debit_postings(self, evaluator: RuleEvaluator) -> None:
        """SUM de débitos acima do limite → REJECTED."""
        postings = [
            CanonicalPosting(account_id="a", amount=600_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="b", amount=600_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings=postings)

        # SUM(postings WHERE direction == "DEBIT" SELECT amount) > 500000
        condition = ComparisonNode(
            left=AggregateNode(
                function="SUM",
                collection=CollectionRefNode(name="postings"),
                where=PredicateNode(
                    binding="item",
                    condition=ComparisonNode(
                        left=FieldAccessNode(path=("direction",)),
                        operator="==",
                        right=LiteralNode(value="DEBIT"),
                    ),
                ),
                select=FieldAccessNode(path=("amount",)),
            ),
            operator=">",
            right=LiteralNode(value=500_000),
        )
        aps = _make_active_policy_set([self._deny_when(condition)])

        result = evaluator.evaluate(context, aps)

        assert result.decision.final_verdict == FinalVerdict.REJECTED

    def test_sum_below_limit_approves(self, evaluator: RuleEvaluator) -> None:
        """SUM abaixo do limite → APPROVED."""
        postings = [
            CanonicalPosting(account_id="a", amount=100_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="b", amount=100_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings=postings)

        condition = ComparisonNode(
            left=AggregateNode(
                function="SUM",
                collection=CollectionRefNode(name="postings"),
                where=PredicateNode(
                    binding="item",
                    condition=ComparisonNode(
                        left=FieldAccessNode(path=("direction",)),
                        operator="==",
                        right=LiteralNode(value="DEBIT"),
                    ),
                ),
                select=FieldAccessNode(path=("amount",)),
            ),
            operator=">",
            right=LiteralNode(value=500_000),
        )
        aps = _make_active_policy_set([self._deny_when(condition)])

        result = evaluator.evaluate(context, aps)

        assert result.decision.final_verdict == FinalVerdict.APPROVED

    def test_count_postings(self, evaluator: RuleEvaluator) -> None:
        """COUNT de postings BRL == 2 → condição verdadeira."""
        postings = [
            CanonicalPosting(account_id="a", amount=5_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="b", amount=5_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings=postings)

        # DENY quando COUNT(postings WHERE currency == "BRL") != 2
        condition = ComparisonNode(
            left=AggregateNode(
                function="COUNT",
                collection=CollectionRefNode(name="postings"),
                where=PredicateNode(
                    binding="item",
                    condition=ComparisonNode(
                        left=FieldAccessNode(path=("currency",)),
                        operator="==",
                        right=LiteralNode(value="BRL"),
                    ),
                ),
            ),
            operator="!=",
            right=LiteralNode(value=2),
        )
        aps = _make_active_policy_set([self._deny_when(condition)])

        result = evaluator.evaluate(context, aps)

        # COUNT == 2, condição != 2 é False → APPROVED
        assert result.decision.final_verdict == FinalVerdict.APPROVED

    def test_any_blocked_account(self, evaluator: RuleEvaluator) -> None:
        """ANY posting com account_id bloqueado → REJECTED."""
        postings = [
            CanonicalPosting(account_id="blocked_acc", amount=5_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="normal_acc", amount=5_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings=postings)

        # ANY(postings WHERE account_id IN ref.blocked_accounts)
        condition = AggregateNode(
            function="ANY",
            collection=CollectionRefNode(name="postings"),
            where=PredicateNode(
                binding="item",
                condition=ComparisonNode(
                    left=FieldAccessNode(path=("account_id",)),
                    operator="IN",
                    right=RefAccessNode(path=("blocked_accounts",)),
                ),
            ),
        )
        snapshot_data = {"blocked_accounts": ("blocked_acc", "other_blocked"), "daily_limit_minor": 500_000}
        aps = _make_active_policy_set([self._deny_when(condition)], snapshot_data=snapshot_data)

        result = evaluator.evaluate(context, aps)

        assert result.decision.final_verdict == FinalVerdict.REJECTED

    def test_all_postings_brl(self, evaluator: RuleEvaluator) -> None:
        """ALL postings em BRL → condição verdadeira."""
        postings = [
            CanonicalPosting(account_id="a", amount=5_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="b", amount=5_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings=postings)

        # DENY quando NOT ALL(postings WHERE currency == "BRL")
        condition = NotOpNode(
            operand=AggregateNode(
                function="ALL",
                collection=CollectionRefNode(name="postings"),
                where=PredicateNode(
                    binding="item",
                    condition=ComparisonNode(
                        left=FieldAccessNode(path=("currency",)),
                        operator="==",
                        right=LiteralNode(value="BRL"),
                    ),
                ),
            )
        )
        aps = _make_active_policy_set([self._deny_when(condition)])

        result = evaluator.evaluate(context, aps)

        # ALL são BRL → NOT ALL é False → APPROVED
        assert result.decision.final_verdict == FinalVerdict.APPROVED

    def test_min_max_aggregation(self, evaluator: RuleEvaluator) -> None:
        """MIN e MAX retornam valores corretos."""
        postings = [
            CanonicalPosting(account_id="a", amount=1_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="b", amount=9_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="c", amount=10_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings=postings)

        # DENY quando MAX(postings SELECT amount) > 8000
        condition = ComparisonNode(
            left=AggregateNode(
                function="MAX",
                collection=CollectionRefNode(name="postings"),
                select=FieldAccessNode(path=("amount",)),
            ),
            operator=">",
            right=LiteralNode(value=8_000),
        )
        aps = _make_active_policy_set([self._deny_when(condition)])

        result = evaluator.evaluate(context, aps)

        # MAX é 10000 > 8000 → REJECTED
        assert result.decision.final_verdict == FinalVerdict.REJECTED


# ---------------------------------------------------------------------------
# Testes de resolução de namespaces
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNamespaceResolution:
    """Testa a resolução dos namespaces facts.*, policy_context.* e ref.*."""

    def test_facts_namespace(self, evaluator: RuleEvaluator) -> None:
        """Acesso a facts.posting_count funciona corretamente."""
        context = _make_context()  # 2 postings
        rule = PolicyRuleNode(
            name="deny_if_not_two",
            priority=100,
            condition=ComparisonNode(
                left=FieldAccessNode(path=("facts", "posting_count")),
                operator="!=",
                right=LiteralNode(value=2),
            ),
            effect=PolicyEffect.DENY,
            message="Must have 2 postings",
        )
        aps = _make_active_policy_set([rule])

        result = evaluator.evaluate(context, aps)

        # posting_count == 2, condição != 2 é False → APPROVED
        assert result.decision.final_verdict == FinalVerdict.APPROVED

    def test_policy_context_namespace(self, evaluator: RuleEvaluator) -> None:
        """Acesso a policy_context.daily_limit_minor funciona corretamente."""
        context = _make_context(policy_context={"daily_limit_minor": 100})

        # DENY quando policy_context.daily_limit_minor < 500
        rule = PolicyRuleNode(
            name="deny_low_limit",
            priority=100,
            condition=ComparisonNode(
                left=FieldAccessNode(path=("policy_context", "daily_limit_minor")),
                operator="<",
                right=LiteralNode(value=500),
            ),
            effect=PolicyEffect.DENY,
            message="Limit too low",
        )
        aps = _make_active_policy_set([rule])

        result = evaluator.evaluate(context, aps)

        # 100 < 500 → REJECTED
        assert result.decision.final_verdict == FinalVerdict.REJECTED

    def test_ref_namespace(self, evaluator: RuleEvaluator) -> None:
        """Acesso a ref.daily_limit_minor do snapshot funciona corretamente."""
        postings = [
            CanonicalPosting(account_id="a", amount=600_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="b", amount=600_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings=postings)

        # DENY quando SUM(DEBIT) > ref.daily_limit_minor
        rule = PolicyRuleNode(
            name="deny_over_limit",
            priority=100,
            condition=ComparisonNode(
                left=AggregateNode(
                    function="SUM",
                    collection=CollectionRefNode(name="postings"),
                    where=PredicateNode(
                        binding="item",
                        condition=ComparisonNode(
                            left=FieldAccessNode(path=("direction",)),
                            operator="==",
                            right=LiteralNode(value="DEBIT"),
                        ),
                    ),
                    select=FieldAccessNode(path=("amount",)),
                ),
                operator=">",
                right=RefAccessNode(path=("daily_limit_minor",)),
            ),
            effect=PolicyEffect.DENY,
            message="Over daily limit",
        )
        # Snapshot com limite de 500_000 — débito de 600_000 excede
        aps = _make_active_policy_set([rule], snapshot_data={"daily_limit_minor": 500_000})

        result = evaluator.evaluate(context, aps)

        assert result.decision.final_verdict == FinalVerdict.REJECTED

    def test_logical_and_operator(self, evaluator: RuleEvaluator) -> None:
        """Operador AND: ambas as condições devem ser verdadeiras."""
        context = _make_context()  # 2 postings, BRL

        # DENY quando posting_count >= 2 AND has_platform_account == True
        rule = PolicyRuleNode(
            name="deny_platform",
            priority=100,
            condition=LogicalOpNode(
                operator="AND",
                left=ComparisonNode(
                    left=FieldAccessNode(path=("facts", "posting_count")),
                    operator=">=",
                    right=LiteralNode(value=2),
                ),
                right=ComparisonNode(
                    left=FieldAccessNode(path=("facts", "has_platform_account")),
                    operator="==",
                    right=LiteralNode(value=True),
                ),
            ),
            effect=PolicyEffect.DENY,
            message="Platform account",
        )
        aps = _make_active_policy_set([rule])

        result = evaluator.evaluate(context, aps)

        # posting_count >= 2 é True, mas has_platform_account é False → AND é False → APPROVED
        assert result.decision.final_verdict == FinalVerdict.APPROVED

    def test_not_operator(self, evaluator: RuleEvaluator) -> None:
        """Operador NOT inverte o resultado da condição."""
        context = _make_context()  # has_platform_account = False

        # DENY quando NOT has_platform_account (ou seja, quando NÃO tem conta de plataforma)
        rule = PolicyRuleNode(
            name="deny_no_platform",
            priority=100,
            condition=NotOpNode(
                operand=ComparisonNode(
                    left=FieldAccessNode(path=("facts", "has_platform_account")),
                    operator="==",
                    right=LiteralNode(value=True),
                )
            ),
            effect=PolicyEffect.DENY,
            message="No platform account",
        )
        aps = _make_active_policy_set([rule])

        result = evaluator.evaluate(context, aps)

        # has_platform_account é False → NOT False → True → REJECTED
        assert result.decision.final_verdict == FinalVerdict.REJECTED


# ---------------------------------------------------------------------------
# Testes de incompatibilidade de bundle
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleIncompatibility:
    """Testa rejeição de bundles inválidos ou incompatíveis."""

    def test_rejects_bundle_without_integrity_verified(self, evaluator: RuleEvaluator) -> None:
        """Bundle sem integridade verificada é rejeitado."""
        aps = _make_active_policy_set([], integrity_verified=False)
        context = _make_context()

        with pytest.raises(InvalidPolicyBundle, match="integridade"):
            evaluator.evaluate(context, aps)

    def test_rejects_bundle_with_incompatible_evaluator_version(self, evaluator: RuleEvaluator) -> None:
        """Bundle que exige versão do evaluator superior à atual é rejeitado."""
        compat = _make_compatibility(evaluator_min_version="99.0.0")
        aps = _make_active_policy_set([], compat=compat)
        context = _make_context()

        with pytest.raises(InvalidPolicyBundle, match="inferior ao mínimo"):
            evaluator.evaluate(context, aps)

    def test_rejects_bundle_with_incompatible_context_schema(self, evaluator: RuleEvaluator) -> None:
        """Bundle com context_schema_version diferente do contexto é rejeitado."""
        compat = _make_compatibility(context_schema_version="2.0")
        aps = _make_active_policy_set([], compat=compat)
        # Contexto com schema 1.0, bundle espera 2.0
        context = _make_context(context_schema_version="1.0")

        with pytest.raises(InvalidPolicyBundle, match="context_schema_version"):
            evaluator.evaluate(context, aps)

    def test_accepts_compatible_bundle(self, evaluator: RuleEvaluator) -> None:
        """Bundle compatível com o evaluator e contexto é aceito."""
        compat = _make_compatibility(
            context_schema_version="1.0",
            evaluator_min_version="1.0.0",
        )
        aps = _make_active_policy_set([], compat=compat)
        context = _make_context(context_schema_version="1.0")

        # Sem rules → APPROVED sem erros
        result = evaluator.evaluate(context, aps)
        assert result.decision.final_verdict == FinalVerdict.APPROVED


# ---------------------------------------------------------------------------
# Testes de métricas
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMetrics:
    """Testa que métricas são coletadas corretamente e não afetam a decisão."""

    def test_latency_is_positive(self, evaluator: RuleEvaluator) -> None:
        """Latência de avaliação é sempre positiva."""
        aps = _make_active_policy_set([])
        context = _make_context()

        result = evaluator.evaluate(context, aps)

        assert result.metrics.evaluation_latency_ms >= 0.0

    def test_evaluated_rules_count_matches(self, evaluator: RuleEvaluator) -> None:
        """Número de rules avaliadas corresponde ao número de rules no bundle."""
        rules = [
            PolicyRuleNode(
                name=f"rule_{i}",
                priority=i,
                condition=LiteralNode(value=False),
                effect=PolicyEffect.DENY,
                message=f"Rule {i}",
            )
            for i in range(5)
        ]
        aps = _make_active_policy_set(rules)
        context = _make_context()

        result = evaluator.evaluate(context, aps)

        assert result.metrics.evaluated_rules == 5

    def test_metrics_do_not_affect_verdict(self, evaluator: RuleEvaluator) -> None:
        """Métricas são separadas da decisão semântica."""
        aps = _make_active_policy_set([])
        context = _make_context()

        result1 = evaluator.evaluate(context, aps)
        result2 = evaluator.evaluate(context, aps)

        # Decisões devem ser idênticas independentemente das métricas
        assert result1.decision == result2.decision
        # Mas latências podem diferir (são efêmeras)
        assert result1.decision.final_verdict == result2.decision.final_verdict


# ---------------------------------------------------------------------------
# Testes de erros de avaliação
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvaluationErrors:
    """Testa tratamento de erros durante a avaliação."""

    def test_unknown_ref_key_raises_evaluation_error(self, evaluator: RuleEvaluator) -> None:
        """Acesso a chave inexistente no snapshot levanta PolicyEvaluationError."""
        rule = PolicyRuleNode(
            name="deny_missing_ref",
            priority=100,
            condition=ComparisonNode(
                left=RefAccessNode(path=("nonexistent_key",)),
                operator=">",
                right=LiteralNode(value=0),
            ),
            effect=PolicyEffect.DENY,
            message="Missing ref",
        )
        aps = _make_active_policy_set([rule])
        context = _make_context()

        with pytest.raises(PolicyEvaluationError, match="nonexistent_key"):
            evaluator.evaluate(context, aps)

    def test_unknown_facts_field_raises_evaluation_error(self, evaluator: RuleEvaluator) -> None:
        """Acesso a campo inexistente em facts levanta PolicyEvaluationError."""
        rule = PolicyRuleNode(
            name="deny_bad_field",
            priority=100,
            condition=ComparisonNode(
                left=FieldAccessNode(path=("facts", "nonexistent_field")),
                operator=">",
                right=LiteralNode(value=0),
            ),
            effect=PolicyEffect.DENY,
            message="Bad field",
        )
        aps = _make_active_policy_set([rule])
        context = _make_context()

        with pytest.raises(PolicyEvaluationError, match="nonexistent_field"):
            evaluator.evaluate(context, aps)

    def test_missing_policy_context_field_raises_evaluation_error(self, evaluator: RuleEvaluator) -> None:
        """Acesso a campo ausente em policy_context levanta PolicyEvaluationError."""
        rule = PolicyRuleNode(
            name="deny_missing_ctx",
            priority=100,
            condition=ComparisonNode(
                left=FieldAccessNode(path=("policy_context", "missing_field")),
                operator=">",
                right=LiteralNode(value=0),
            ),
            effect=PolicyEffect.DENY,
            message="Missing context field",
        )
        aps = _make_active_policy_set([rule])
        context = _make_context(policy_context={})

        with pytest.raises(PolicyEvaluationError, match="missing_field"):
            evaluator.evaluate(context, aps)
