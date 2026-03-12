"""
Testes unitários para o AST da Policy Rule DSL.

Verifica:
- Imutabilidade de todos os nós (frozen dataclasses)
- Igualdade estrutural por valor
- Enums: valores, membros e semântica
- Composição recursiva de nós
- RuleAST como container raiz
- Nós folha: LiteralNode, FieldAccessNode, RefAccessNode, CollectionRefNode
- Nós compostos: PredicateNode, AggregateNode, ComparisonNode, LogicalOpNode, NotOpNode
- Nó raiz de rule: PolicyRuleNode

Requisitos cobertos: 23.1, 23.2, 23.3, 23.4, 23.5, 23.7, 23.8, 23.9
"""

import pytest

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
# Fixtures reutilizáveis
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_comparison() -> ComparisonNode:
    """Comparação simples: facts.posting_count >= 2"""
    return ComparisonNode(
        left=FieldAccessNode(path=("facts", "posting_count")),
        operator=">=",
        right=LiteralNode(value=2),
    )


@pytest.fixture
def deny_rule(simple_comparison) -> PolicyRuleNode:
    """Rule DENY simples para reuso nos testes."""
    return PolicyRuleNode(
        name="deny_test",
        priority=100,
        condition=simple_comparison,
        effect=PolicyEffect.DENY,
        message="Test deny rule",
    )


@pytest.fixture
def allow_rule(simple_comparison) -> PolicyRuleNode:
    """Rule ALLOW simples para reuso nos testes."""
    return PolicyRuleNode(
        name="allow_test",
        priority=10,
        condition=simple_comparison,
        effect=PolicyEffect.ALLOW,
        message="Test allow rule",
    )


@pytest.fixture
def minimal_rule_ast(deny_rule) -> RuleAST:
    """RuleAST mínimo com uma rule DENY."""
    return RuleAST(rules=(deny_rule,))


# ---------------------------------------------------------------------------
# Testes de enums
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPolicyEffect:
    """Verifica os valores e semântica do enum PolicyEffect."""

    def test_allow_value(self):
        assert PolicyEffect.ALLOW.value == "ALLOW"

    def test_deny_value(self):
        assert PolicyEffect.DENY.value == "DENY"

    def test_enum_members_count(self):
        """Deve ter exatamente dois membros: ALLOW e DENY."""
        assert len(PolicyEffect) == 2

    def test_is_string_enum(self):
        """PolicyEffect deve ser um str enum para serialização direta."""
        assert isinstance(PolicyEffect.ALLOW, str)
        assert isinstance(PolicyEffect.DENY, str)

    def test_from_string(self):
        """Deve ser possível construir a partir de string."""
        assert PolicyEffect("ALLOW") is PolicyEffect.ALLOW
        assert PolicyEffect("DENY") is PolicyEffect.DENY


@pytest.mark.unit
class TestFinalVerdict:
    """Verifica os valores e semântica do enum FinalVerdict."""

    def test_approved_value(self):
        assert FinalVerdict.APPROVED.value == "APPROVED"

    def test_rejected_value(self):
        assert FinalVerdict.REJECTED.value == "REJECTED"

    def test_enum_members_count(self):
        assert len(FinalVerdict) == 2

    def test_is_string_enum(self):
        assert isinstance(FinalVerdict.APPROVED, str)
        assert isinstance(FinalVerdict.REJECTED, str)

    def test_from_string(self):
        assert FinalVerdict("APPROVED") is FinalVerdict.APPROVED
        assert FinalVerdict("REJECTED") is FinalVerdict.REJECTED


@pytest.mark.unit
class TestCompositionMode:
    """Verifica o enum CompositionMode."""

    def test_deny_overrides_value(self):
        assert CompositionMode.DENY_OVERRIDES.value == "DENY_OVERRIDES"

    def test_is_string_enum(self):
        assert isinstance(CompositionMode.DENY_OVERRIDES, str)

    def test_from_string(self):
        assert CompositionMode("DENY_OVERRIDES") is CompositionMode.DENY_OVERRIDES


# ---------------------------------------------------------------------------
# Testes de nós folha
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLiteralNode:
    """Verifica LiteralNode: imutabilidade, igualdade e tipos suportados."""

    def test_integer_literal(self):
        node = LiteralNode(value=42)
        assert node.value == 42

    def test_float_literal(self):
        node = LiteralNode(value=3.14)
        assert node.value == 3.14

    def test_string_literal(self):
        node = LiteralNode(value="BRL")
        assert node.value == "BRL"

    def test_bool_literal_true(self):
        node = LiteralNode(value=True)
        assert node.value is True

    def test_bool_literal_false(self):
        node = LiteralNode(value=False)
        assert node.value is False

    def test_immutability(self):
        """LiteralNode é frozen — atribuição deve levantar FrozenInstanceError."""
        node = LiteralNode(value=10)
        with pytest.raises(Exception):  # FrozenInstanceError é subclasse de AttributeError
            node.value = 99  # type: ignore[misc]

    def test_equality_by_value(self):
        """Dois LiteralNodes com mesmo valor devem ser iguais."""
        assert LiteralNode(value=10) == LiteralNode(value=10)

    def test_inequality_different_values(self):
        assert LiteralNode(value=10) != LiteralNode(value=20)

    def test_hashable(self):
        """Frozen dataclasses devem ser hashable para uso em sets/dicts."""
        node = LiteralNode(value=10)
        assert hash(node) == hash(LiteralNode(value=10))


@pytest.mark.unit
class TestFieldAccessNode:
    """Verifica FieldAccessNode: path como tupla imutável."""

    def test_single_level_path(self):
        node = FieldAccessNode(path=("facts",))
        assert node.path == ("facts",)

    def test_two_level_path(self):
        node = FieldAccessNode(path=("facts", "posting_count"))
        assert node.path == ("facts", "posting_count")

    def test_policy_context_path(self):
        node = FieldAccessNode(path=("policy_context", "daily_limit"))
        assert node.path == ("policy_context", "daily_limit")

    def test_immutability(self):
        node = FieldAccessNode(path=("facts", "posting_count"))
        with pytest.raises(Exception):
            node.path = ("other",)  # type: ignore[misc]

    def test_equality_by_value(self):
        assert FieldAccessNode(path=("facts", "posting_count")) == FieldAccessNode(
            path=("facts", "posting_count")
        )

    def test_inequality_different_paths(self):
        assert FieldAccessNode(path=("facts", "posting_count")) != FieldAccessNode(
            path=("facts", "max_posting_amount")
        )

    def test_path_is_tuple(self):
        """O path deve ser uma tupla, não uma lista."""
        node = FieldAccessNode(path=("facts", "posting_count"))
        assert isinstance(node.path, tuple)


@pytest.mark.unit
class TestRefAccessNode:
    """Verifica RefAccessNode: separação explícita do namespace ref.*"""

    def test_single_key_path(self):
        node = RefAccessNode(path=("daily_limit_minor",))
        assert node.path == ("daily_limit_minor",)

    def test_blocked_accounts_path(self):
        node = RefAccessNode(path=("blocked_accounts",))
        assert node.path == ("blocked_accounts",)

    def test_immutability(self):
        node = RefAccessNode(path=("daily_limit_minor",))
        with pytest.raises(Exception):
            node.path = ("other",)  # type: ignore[misc]

    def test_equality_by_value(self):
        assert RefAccessNode(path=("daily_limit_minor",)) == RefAccessNode(
            path=("daily_limit_minor",)
        )

    def test_distinct_from_field_access_node(self):
        """RefAccessNode e FieldAccessNode com mesmo path devem ser diferentes."""
        ref = RefAccessNode(path=("daily_limit_minor",))
        field = FieldAccessNode(path=("daily_limit_minor",))
        assert ref != field


@pytest.mark.unit
class TestCollectionRefNode:
    """Verifica CollectionRefNode: referência à coleção postings."""

    def test_postings_collection(self):
        node = CollectionRefNode(name="postings")
        assert node.name == "postings"

    def test_immutability(self):
        node = CollectionRefNode(name="postings")
        with pytest.raises(Exception):
            node.name = "other"  # type: ignore[misc]

    def test_equality_by_value(self):
        assert CollectionRefNode(name="postings") == CollectionRefNode(name="postings")

    def test_inequality_different_names(self):
        assert CollectionRefNode(name="postings") != CollectionRefNode(name="entries")


# ---------------------------------------------------------------------------
# Testes de nós compostos
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPredicateNode:
    """Verifica PredicateNode: binding e condição aninhada."""

    def test_basic_predicate(self):
        condition = ComparisonNode(
            left=FieldAccessNode(path=("direction",)),
            operator="==",
            right=LiteralNode(value="DEBIT"),
        )
        node = PredicateNode(binding="item", condition=condition)
        assert node.binding == "item"
        assert node.condition == condition

    def test_immutability(self):
        condition = LiteralNode(value=True)
        node = PredicateNode(binding="item", condition=condition)
        with pytest.raises(Exception):
            node.binding = "other"  # type: ignore[misc]

    def test_equality_by_value(self):
        condition = LiteralNode(value=True)
        assert PredicateNode(binding="item", condition=condition) == PredicateNode(
            binding="item", condition=condition
        )

    def test_inequality_different_binding(self):
        condition = LiteralNode(value=True)
        assert PredicateNode(binding="item", condition=condition) != PredicateNode(
            binding="posting", condition=condition
        )


@pytest.mark.unit
class TestAggregateNode:
    """Verifica AggregateNode: funções, coleção, where e select opcionais."""

    def test_sum_without_filter(self):
        node = AggregateNode(
            function="SUM",
            collection=CollectionRefNode(name="postings"),
            select=FieldAccessNode(path=("amount",)),
        )
        assert node.function == "SUM"
        assert node.where is None
        assert node.select == FieldAccessNode(path=("amount",))

    def test_count_with_filter(self):
        where = PredicateNode(
            binding="item",
            condition=ComparisonNode(
                left=FieldAccessNode(path=("currency",)),
                operator="==",
                right=LiteralNode(value="BRL"),
            ),
        )
        node = AggregateNode(
            function="COUNT",
            collection=CollectionRefNode(name="postings"),
            where=where,
        )
        assert node.function == "COUNT"
        assert node.where == where
        assert node.select is None

    def test_any_aggregate(self):
        node = AggregateNode(
            function="ANY",
            collection=CollectionRefNode(name="postings"),
        )
        assert node.function == "ANY"

    def test_all_aggregate(self):
        node = AggregateNode(
            function="ALL",
            collection=CollectionRefNode(name="postings"),
        )
        assert node.function == "ALL"

    def test_supported_functions(self):
        """Verifica que todas as funções do Requisito 23.3 são representáveis."""
        supported = ["SUM", "COUNT", "MIN", "MAX", "ANY", "ALL"]
        collection = CollectionRefNode(name="postings")
        for func in supported:
            node = AggregateNode(function=func, collection=collection)
            assert node.function == func

    def test_immutability(self):
        node = AggregateNode(
            function="SUM",
            collection=CollectionRefNode(name="postings"),
        )
        with pytest.raises(Exception):
            node.function = "COUNT"  # type: ignore[misc]

    def test_equality_by_value(self):
        collection = CollectionRefNode(name="postings")
        assert AggregateNode(function="SUM", collection=collection) == AggregateNode(
            function="SUM", collection=collection
        )


@pytest.mark.unit
class TestComparisonNode:
    """Verifica ComparisonNode: operadores e operandos."""

    def test_greater_than_or_equal(self, simple_comparison):
        assert simple_comparison.operator == ">="
        assert simple_comparison.left == FieldAccessNode(path=("facts", "posting_count"))
        assert simple_comparison.right == LiteralNode(value=2)

    def test_supported_operators(self):
        """Verifica que todos os operadores do Requisito 23.1 são representáveis."""
        operators = ["==", "!=", "<", "<=", ">", ">=", "IN"]
        left = FieldAccessNode(path=("facts", "posting_count"))
        right = LiteralNode(value=1)
        for op in operators:
            node = ComparisonNode(left=left, operator=op, right=right)
            assert node.operator == op

    def test_immutability(self, simple_comparison):
        with pytest.raises(Exception):
            simple_comparison.operator = "=="  # type: ignore[misc]

    def test_equality_by_value(self):
        left = LiteralNode(value=1)
        right = LiteralNode(value=2)
        assert ComparisonNode(left=left, operator="<", right=right) == ComparisonNode(
            left=left, operator="<", right=right
        )

    def test_inequality_different_operator(self):
        left = LiteralNode(value=1)
        right = LiteralNode(value=2)
        assert ComparisonNode(left=left, operator="<", right=right) != ComparisonNode(
            left=left, operator=">", right=right
        )


@pytest.mark.unit
class TestLogicalOpNode:
    """Verifica LogicalOpNode: AND e OR com sub-nós."""

    def test_and_operator(self, simple_comparison):
        node = LogicalOpNode(
            operator="AND",
            left=simple_comparison,
            right=simple_comparison,
        )
        assert node.operator == "AND"

    def test_or_operator(self, simple_comparison):
        node = LogicalOpNode(
            operator="OR",
            left=simple_comparison,
            right=simple_comparison,
        )
        assert node.operator == "OR"

    def test_nested_logical_ops(self, simple_comparison):
        """Deve suportar composição recursiva de operadores lógicos."""
        inner = LogicalOpNode(operator="AND", left=simple_comparison, right=simple_comparison)
        outer = LogicalOpNode(operator="OR", left=inner, right=simple_comparison)
        assert isinstance(outer.left, LogicalOpNode)
        assert outer.left.operator == "AND"

    def test_immutability(self, simple_comparison):
        node = LogicalOpNode(operator="AND", left=simple_comparison, right=simple_comparison)
        with pytest.raises(Exception):
            node.operator = "OR"  # type: ignore[misc]

    def test_equality_by_value(self, simple_comparison):
        node_a = LogicalOpNode(operator="AND", left=simple_comparison, right=simple_comparison)
        node_b = LogicalOpNode(operator="AND", left=simple_comparison, right=simple_comparison)
        assert node_a == node_b


@pytest.mark.unit
class TestNotOpNode:
    """Verifica NotOpNode: negação unária."""

    def test_not_wraps_comparison(self, simple_comparison):
        node = NotOpNode(operand=simple_comparison)
        assert node.operand == simple_comparison

    def test_not_wraps_aggregate(self):
        aggregate = AggregateNode(
            function="ANY",
            collection=CollectionRefNode(name="postings"),
        )
        node = NotOpNode(operand=aggregate)
        assert node.operand == aggregate

    def test_double_negation(self, simple_comparison):
        """NOT(NOT(x)) deve ser representável."""
        inner = NotOpNode(operand=simple_comparison)
        outer = NotOpNode(operand=inner)
        assert isinstance(outer.operand, NotOpNode)

    def test_immutability(self, simple_comparison):
        node = NotOpNode(operand=simple_comparison)
        with pytest.raises(Exception):
            node.operand = LiteralNode(value=True)  # type: ignore[misc]

    def test_equality_by_value(self, simple_comparison):
        assert NotOpNode(operand=simple_comparison) == NotOpNode(operand=simple_comparison)


# ---------------------------------------------------------------------------
# Testes de PolicyRuleNode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPolicyRuleNode:
    """Verifica PolicyRuleNode: campos, imutabilidade e igualdade."""

    def test_deny_rule_fields(self, deny_rule):
        assert deny_rule.name == "deny_test"
        assert deny_rule.priority == 100
        assert deny_rule.effect == PolicyEffect.DENY
        assert deny_rule.message == "Test deny rule"

    def test_allow_rule_fields(self, allow_rule):
        assert allow_rule.name == "allow_test"
        assert allow_rule.priority == 10
        assert allow_rule.effect == PolicyEffect.ALLOW

    def test_immutability(self, deny_rule):
        with pytest.raises(Exception):
            deny_rule.name = "other"  # type: ignore[misc]

    def test_equality_by_value(self, simple_comparison):
        rule_a = PolicyRuleNode(
            name="rule_x",
            priority=50,
            condition=simple_comparison,
            effect=PolicyEffect.DENY,
            message="msg",
        )
        rule_b = PolicyRuleNode(
            name="rule_x",
            priority=50,
            condition=simple_comparison,
            effect=PolicyEffect.DENY,
            message="msg",
        )
        assert rule_a == rule_b

    def test_inequality_different_name(self, simple_comparison):
        rule_a = PolicyRuleNode(
            name="rule_a",
            priority=50,
            condition=simple_comparison,
            effect=PolicyEffect.DENY,
            message="msg",
        )
        rule_b = PolicyRuleNode(
            name="rule_b",
            priority=50,
            condition=simple_comparison,
            effect=PolicyEffect.DENY,
            message="msg",
        )
        assert rule_a != rule_b

    def test_inequality_different_effect(self, simple_comparison):
        deny = PolicyRuleNode(
            name="rule",
            priority=50,
            condition=simple_comparison,
            effect=PolicyEffect.DENY,
            message="msg",
        )
        allow = PolicyRuleNode(
            name="rule",
            priority=50,
            condition=simple_comparison,
            effect=PolicyEffect.ALLOW,
            message="msg",
        )
        assert deny != allow


# ---------------------------------------------------------------------------
# Testes de RuleAST
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRuleAST:
    """Verifica RuleAST: container raiz, imutabilidade e composição."""

    def test_single_rule(self, deny_rule):
        ast = RuleAST(rules=(deny_rule,))
        assert len(ast.rules) == 1
        assert ast.rules[0] == deny_rule

    def test_multiple_rules(self, deny_rule, allow_rule):
        ast = RuleAST(rules=(deny_rule, allow_rule))
        assert len(ast.rules) == 2

    def test_default_composition_mode(self, deny_rule):
        """O modo padrão deve ser DENY_OVERRIDES (Requisito 23.8)."""
        ast = RuleAST(rules=(deny_rule,))
        assert ast.composition_mode == CompositionMode.DENY_OVERRIDES

    def test_explicit_composition_mode(self, deny_rule):
        ast = RuleAST(
            rules=(deny_rule,),
            composition_mode=CompositionMode.DENY_OVERRIDES,
        )
        assert ast.composition_mode == CompositionMode.DENY_OVERRIDES

    def test_rules_is_tuple(self, deny_rule):
        """rules deve ser uma tupla imutável."""
        ast = RuleAST(rules=(deny_rule,))
        assert isinstance(ast.rules, tuple)

    def test_immutability(self, deny_rule):
        ast = RuleAST(rules=(deny_rule,))
        with pytest.raises(Exception):
            ast.rules = ()  # type: ignore[misc]

    def test_equality_by_value(self, deny_rule):
        ast_a = RuleAST(rules=(deny_rule,))
        ast_b = RuleAST(rules=(deny_rule,))
        assert ast_a == ast_b

    def test_inequality_different_rules(self, deny_rule, allow_rule):
        ast_a = RuleAST(rules=(deny_rule,))
        ast_b = RuleAST(rules=(allow_rule,))
        assert ast_a != ast_b

    def test_rule_order_matters(self, deny_rule, allow_rule):
        """A ordem das rules no AST deve ser preservada."""
        ast_a = RuleAST(rules=(deny_rule, allow_rule))
        ast_b = RuleAST(rules=(allow_rule, deny_rule))
        assert ast_a != ast_b


# ---------------------------------------------------------------------------
# Testes de composição complexa (Requisito 23.9)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestASTComposition:
    """
    Verifica composição recursiva de nós do AST.

    Representa o exemplo da DSL do design:
        POLICY deny_over_daily_limit PRIORITY 100
          WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > ref.daily_limit_minor
          THEN DENY "Transaction exceeds daily debit limit"
    """

    def test_deny_over_daily_limit_rule(self):
        """Constrói a rule deny_over_daily_limit do exemplo do design."""
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
            right=RefAccessNode(path=("daily_limit_minor",)),
        )
        rule = PolicyRuleNode(
            name="deny_over_daily_limit",
            priority=100,
            condition=condition,
            effect=PolicyEffect.DENY,
            message="Transaction exceeds daily debit limit",
        )
        assert rule.name == "deny_over_daily_limit"
        assert rule.effect == PolicyEffect.DENY
        assert isinstance(rule.condition, ComparisonNode)
        assert isinstance(rule.condition.left, AggregateNode)
        assert rule.condition.left.function == "SUM"
        assert isinstance(rule.condition.right, RefAccessNode)

    def test_deny_blocked_account_rule(self):
        """
        Constrói a rule deny_blocked_account do exemplo do design:
            WHEN ANY(postings WHERE account_id IN ref.blocked_accounts)
        """
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
        rule = PolicyRuleNode(
            name="deny_blocked_account",
            priority=90,
            condition=condition,
            effect=PolicyEffect.DENY,
            message="Blocked account",
        )
        assert rule.effect == PolicyEffect.DENY
        assert isinstance(rule.condition, AggregateNode)
        assert rule.condition.function == "ANY"

    def test_allow_standard_brl_rule(self):
        """
        Constrói a rule allow_standard_brl do exemplo do design:
            WHEN facts.posting_count >= 2
              AND COUNT(postings WHERE currency == "BRL") == facts.posting_count
        """
        condition = LogicalOpNode(
            operator="AND",
            left=ComparisonNode(
                left=FieldAccessNode(path=("facts", "posting_count")),
                operator=">=",
                right=LiteralNode(value=2),
            ),
            right=ComparisonNode(
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
                operator="==",
                right=FieldAccessNode(path=("facts", "posting_count")),
            ),
        )
        rule = PolicyRuleNode(
            name="allow_standard_brl",
            priority=10,
            condition=condition,
            effect=PolicyEffect.ALLOW,
            message="Standard BRL flow",
        )
        assert rule.effect == PolicyEffect.ALLOW
        assert isinstance(rule.condition, LogicalOpNode)
        assert rule.condition.operator == "AND"

    def test_full_ast_with_multiple_rules(self):
        """Constrói um RuleAST completo com as três rules do exemplo do design."""
        deny_limit = PolicyRuleNode(
            name="deny_over_daily_limit",
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
            message="Transaction exceeds daily debit limit",
        )
        deny_blocked = PolicyRuleNode(
            name="deny_blocked_account",
            priority=90,
            condition=AggregateNode(
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
            ),
            effect=PolicyEffect.DENY,
            message="Blocked account",
        )
        allow_brl = PolicyRuleNode(
            name="allow_standard_brl",
            priority=10,
            condition=LiteralNode(value=True),
            effect=PolicyEffect.ALLOW,
            message="Standard BRL flow",
        )
        ast = RuleAST(rules=(deny_limit, deny_blocked, allow_brl))
        assert len(ast.rules) == 3
        assert ast.composition_mode == CompositionMode.DENY_OVERRIDES
        # Verifica que a ordem é preservada
        assert ast.rules[0].name == "deny_over_daily_limit"
        assert ast.rules[1].name == "deny_blocked_account"
        assert ast.rules[2].name == "allow_standard_brl"
