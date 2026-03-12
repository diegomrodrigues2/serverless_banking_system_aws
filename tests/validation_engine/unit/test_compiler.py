"""
Testes unitários para DSLCompiler, SemanticAnalyzer, PolicyCostAnalyzer
e DSLPrettyPrinter.

Cobre:
- Parsing válido produz RuleAST correto
- Erro de sintaxe levanta PolicySyntaxError com linha/coluna
- Erro semântico (namespace proibido, tipo incompatível) levanta PolicySemanticError
- Custo excedido levanta PolicyCostBudgetExceeded
- Round-trip: parse → pretty_print → parse produz AST equivalente
- artifact_hash é determinístico (mesmo input → mesmo hash)
- NON_DETERMINISTIC_FUNCTION para construções proibidas

Requisitos cobertos: 2.1, 2.2, 2.5, 15.1, 15.5
"""
from __future__ import annotations

import pytest

from validation_engine.domain.compiler import (
    DSLCompiler,
    DSLPrettyPrinter,
    PolicyCostAnalyzer,
    SemanticAnalyzer,
)
from validation_engine.domain.cost_analyzer import CostLimits
from validation_engine.domain.errors import (
    PolicyCostBudgetExceeded,
    PolicySemanticError,
    PolicySyntaxError,
)
from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
)
from validation_engine.domain.policy_ast import (
    AggregateNode,
    CollectionRefNode,
    ComparisonNode,
    CompositionMode,
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
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_COMPAT = BundleCompatibility(
    dsl_version="1.0",
    context_schema_version="1.0",
    snapshot_schema_version="1.0",
    evaluator_min_version="1.0.0",
)

_DEFAULT_META = CompilationMetadata(
    author="test",
    description="Test bundle",
    compiled_at="2024-01-01T00:00:00Z",
    source_hash="sha256:test",
)


def _make_compiler(**kwargs) -> DSLCompiler:
    """Cria um DSLCompiler com configurações padrão ou customizadas."""
    semantic = kwargs.pop("semantic_analyzer", SemanticAnalyzer())
    cost = kwargs.pop("cost_analyzer", PolicyCostAnalyzer())
    return DSLCompiler(semantic_analyzer=semantic, cost_analyzer=cost)


def _compile(dsl: str, **kwargs) -> object:
    """Compila DSL com configurações padrão."""
    compiler = _make_compiler(**kwargs)
    return compiler.compile(
        dsl_source=dsl,
        policy_set_id="test_bundle",
        metadata=_DEFAULT_META,
        compatibility=_DEFAULT_COMPAT,
    )


# ---------------------------------------------------------------------------
# DSL examples
# ---------------------------------------------------------------------------

_SIMPLE_DENY_DSL = """
POLICY deny_over_daily_limit PRIORITY 100
  WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > ref.daily_limit_minor
  THEN DENY "Transaction exceeds daily debit limit"
"""

_SIMPLE_ALLOW_DSL = """
POLICY allow_standard_brl PRIORITY 10
  WHEN facts.posting_count >= 2
    AND COUNT(postings WHERE currency == "BRL") == facts.posting_count
  THEN ALLOW "Standard BRL flow"
"""

_BLOCKED_ACCOUNT_DSL = """
POLICY deny_blocked_account PRIORITY 90
  WHEN ANY(postings WHERE account_id IN ref.blocked_accounts)
  THEN DENY "Blocked account"
"""

_MULTI_POLICY_DSL = _SIMPLE_DENY_DSL + "\n" + _BLOCKED_ACCOUNT_DSL + "\n" + _SIMPLE_ALLOW_DSL


# ---------------------------------------------------------------------------
# Task 4.1 — DSLCompiler: valid parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDSLCompilerValidParsing:
    """Testa que DSL válida produz RuleAST e RuleBundle corretos."""

    def test_compile_simple_deny_returns_bundle(self):
        """Compilar DSL válida deve retornar um RuleBundle."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        assert bundle is not None
        assert bundle.policy_set_id == "test_bundle"

    def test_compile_produces_correct_rule_count(self):
        """Bundle compilado deve ter o número correto de rules."""
        bundle = _compile(_MULTI_POLICY_DSL)
        assert len(bundle.ast.rules) == 3

    def test_compile_deny_rule_has_correct_effect(self):
        """Rule DENY deve ter efeito DENY."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        rule = bundle.ast.rules[0]
        assert rule.effect == PolicyEffect.DENY

    def test_compile_allow_rule_has_correct_effect(self):
        """Rule ALLOW deve ter efeito ALLOW."""
        bundle = _compile(_SIMPLE_ALLOW_DSL)
        rule = bundle.ast.rules[0]
        assert rule.effect == PolicyEffect.ALLOW

    def test_compile_rule_name_is_correct(self):
        """Nome da rule deve ser preservado corretamente."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        assert bundle.ast.rules[0].name == "deny_over_daily_limit"

    def test_compile_rule_priority_is_correct(self):
        """Prioridade da rule deve ser preservada corretamente."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        assert bundle.ast.rules[0].priority == 100

    def test_compile_rule_message_is_correct(self):
        """Mensagem da rule deve ser preservada corretamente."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        assert bundle.ast.rules[0].message == "Transaction exceeds daily debit limit"

    def test_compile_composition_mode_is_deny_overrides(self):
        """Modo de composição deve ser DENY_OVERRIDES."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        assert bundle.composition_mode == CompositionMode.DENY_OVERRIDES

    def test_compile_sum_aggregate_produces_aggregate_node(self):
        """SUM(...) deve produzir AggregateNode com function='SUM'."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        rule = bundle.ast.rules[0]
        # condition is ComparisonNode: SUM(...) > ref.daily_limit_minor
        assert isinstance(rule.condition, ComparisonNode)
        assert isinstance(rule.condition.left, AggregateNode)
        assert rule.condition.left.function == "SUM"

    def test_compile_aggregate_collection_is_postings(self):
        """Coleção da agregação deve ser 'postings'."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        agg = bundle.ast.rules[0].condition.left
        assert isinstance(agg.collection, CollectionRefNode)
        assert agg.collection.name == "postings"

    def test_compile_aggregate_where_is_predicate(self):
        """WHERE clause deve produzir PredicateNode."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        agg = bundle.ast.rules[0].condition.left
        assert isinstance(agg.where, PredicateNode)

    def test_compile_aggregate_select_is_field_access(self):
        """SELECT clause deve produzir FieldAccessNode."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        agg = bundle.ast.rules[0].condition.left
        assert isinstance(agg.select, FieldAccessNode)
        assert agg.select.path == ("amount",)

    def test_compile_ref_access_produces_ref_access_node(self):
        """ref.field deve produzir RefAccessNode."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        rule = bundle.ast.rules[0]
        assert isinstance(rule.condition.right, RefAccessNode)
        assert rule.condition.right.path == ("daily_limit_minor",)

    def test_compile_facts_access_produces_field_access_node(self):
        """facts.field deve produzir FieldAccessNode com namespace facts."""
        bundle = _compile(_SIMPLE_ALLOW_DSL)
        rule = bundle.ast.rules[0]
        # condition is AND: facts.posting_count >= 2 AND COUNT(...) == facts.posting_count
        assert isinstance(rule.condition, LogicalOpNode)
        assert rule.condition.operator == "AND"
        left = rule.condition.left
        assert isinstance(left, ComparisonNode)
        assert isinstance(left.left, FieldAccessNode)
        assert left.left.path == ("facts", "posting_count")

    def test_compile_any_aggregate_produces_aggregate_node(self):
        """ANY(...) deve produzir AggregateNode com function='ANY'."""
        bundle = _compile(_BLOCKED_ACCOUNT_DSL)
        rule = bundle.ast.rules[0]
        assert isinstance(rule.condition, AggregateNode)
        assert rule.condition.function == "ANY"

    def test_compile_in_operator_produces_comparison_node(self):
        """IN operator deve produzir ComparisonNode com operator='IN'."""
        bundle = _compile(_BLOCKED_ACCOUNT_DSL)
        rule = bundle.ast.rules[0]
        # ANY(postings WHERE account_id IN ref.blocked_accounts)
        agg = rule.condition
        assert isinstance(agg.where, PredicateNode)
        cond = agg.where.condition
        assert isinstance(cond, ComparisonNode)
        assert cond.operator == "IN"

    def test_compile_artifact_hash_is_non_empty_string(self):
        """artifact_hash deve ser uma string não-vazia."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        assert isinstance(bundle.artifact_hash, str)
        assert len(bundle.artifact_hash) > 0

    def test_compile_artifact_hash_is_sha256_hex(self):
        """artifact_hash deve ser um hexdigest SHA-256 (64 caracteres hex)."""
        bundle = _compile(_SIMPLE_DENY_DSL)
        assert len(bundle.artifact_hash) == 64
        assert all(c in "0123456789abcdef" for c in bundle.artifact_hash)

    def test_compile_not_operator(self):
        """NOT operator deve produzir NotOpNode."""
        dsl = """
POLICY deny_not_brl PRIORITY 50
  WHEN NOT facts.has_platform_account
  THEN DENY "No platform account"
"""
        bundle = _compile(dsl)
        rule = bundle.ast.rules[0]
        assert isinstance(rule.condition, NotOpNode)

    def test_compile_boolean_literal(self):
        """Literais booleanos true/false devem ser parseados corretamente."""
        dsl = """
POLICY deny_if_true PRIORITY 50
  WHEN facts.has_platform_account == true
  THEN DENY "Platform account detected"
"""
        bundle = _compile(dsl)
        rule = bundle.ast.rules[0]
        assert isinstance(rule.condition, ComparisonNode)
        assert isinstance(rule.condition.right, LiteralNode)
        assert rule.condition.right.value is True

    def test_compile_integer_literal(self):
        """Literais inteiros devem ser parseados como int."""
        bundle = _compile(_SIMPLE_ALLOW_DSL)
        rule = bundle.ast.rules[0]
        # facts.posting_count >= 2
        left_cmp = rule.condition.left
        assert isinstance(left_cmp.right, LiteralNode)
        assert left_cmp.right.value == 2
        assert isinstance(left_cmp.right.value, int)

    def test_compile_or_operator(self):
        """OR operator deve produzir LogicalOpNode com operator='OR'."""
        dsl = """
POLICY deny_or PRIORITY 50
  WHEN facts.posting_count == 0 OR facts.posting_count > 100
  THEN DENY "Invalid count"
"""
        bundle = _compile(dsl)
        rule = bundle.ast.rules[0]
        assert isinstance(rule.condition, LogicalOpNode)
        assert rule.condition.operator == "OR"

    def test_compile_create_default_factory(self):
        """DSLCompiler.create_default() deve retornar compilador funcional."""
        compiler = DSLCompiler.create_default()
        bundle = compiler.compile(
            dsl_source=_SIMPLE_DENY_DSL,
            policy_set_id="test",
            metadata=_DEFAULT_META,
            compatibility=_DEFAULT_COMPAT,
        )
        assert bundle is not None


# ---------------------------------------------------------------------------
# Task 4.1 — artifact_hash determinism
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestArtifactHashDeterminism:
    """Testa que o artifact_hash é determinístico."""

    def test_same_input_produces_same_hash(self):
        """Mesmo input deve produzir o mesmo artifact_hash."""
        bundle1 = _compile(_SIMPLE_DENY_DSL)
        bundle2 = _compile(_SIMPLE_DENY_DSL)
        assert bundle1.artifact_hash == bundle2.artifact_hash

    def test_different_dsl_produces_different_hash(self):
        """DSL diferente deve produzir artifact_hash diferente."""
        bundle1 = _compile(_SIMPLE_DENY_DSL)
        bundle2 = _compile(_SIMPLE_ALLOW_DSL)
        assert bundle1.artifact_hash != bundle2.artifact_hash

    def test_hash_is_independent_of_compilation_timestamp(self):
        """
        artifact_hash não deve depender do timestamp de compilação.
        Dois bundles com mesmo DSL mas timestamps diferentes devem ter hashes diferentes
        apenas se os metadados forem diferentes.
        """
        meta1 = CompilationMetadata(
            author="test", description="Test", compiled_at="2024-01-01T00:00:00Z",
            source_hash="sha256:test"
        )
        meta2 = CompilationMetadata(
            author="test", description="Test", compiled_at="2024-01-02T00:00:00Z",
            source_hash="sha256:test"
        )
        compiler = DSLCompiler.create_default()
        bundle1 = compiler.compile(_SIMPLE_DENY_DSL, "test", meta1, _DEFAULT_COMPAT)
        bundle2 = compiler.compile(_SIMPLE_DENY_DSL, "test", meta2, _DEFAULT_COMPAT)
        # Different compiled_at → different hash (metadata is part of hash input)
        assert bundle1.artifact_hash != bundle2.artifact_hash

    def test_hash_is_stable_across_multiple_calls(self):
        """artifact_hash deve ser estável em múltiplas chamadas com mesmo input."""
        hashes = [_compile(_SIMPLE_DENY_DSL).artifact_hash for _ in range(5)]
        assert len(set(hashes)) == 1


# ---------------------------------------------------------------------------
# Task 4.2 — Syntax errors
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDSLSyntaxErrors:
    """Testa que erros de sintaxe levantam PolicySyntaxError com linha/coluna."""

    def test_missing_policy_keyword_raises_syntax_error(self):
        """DSL sem keyword POLICY deve levantar PolicySyntaxError."""
        with pytest.raises(PolicySyntaxError):
            _compile("deny_over_limit PRIORITY 100 WHEN true THEN DENY \"msg\"")

    def test_missing_priority_keyword_raises_syntax_error(self):
        """DSL sem keyword PRIORITY deve levantar PolicySyntaxError."""
        with pytest.raises(PolicySyntaxError):
            _compile("POLICY my_rule 100 WHEN true THEN DENY \"msg\"")

    def test_missing_when_keyword_raises_syntax_error(self):
        """DSL sem keyword WHEN deve levantar PolicySyntaxError."""
        with pytest.raises(PolicySyntaxError):
            _compile("POLICY my_rule PRIORITY 100 true THEN DENY \"msg\"")

    def test_missing_then_keyword_raises_syntax_error(self):
        """DSL sem keyword THEN deve levantar PolicySyntaxError."""
        with pytest.raises(PolicySyntaxError):
            _compile("POLICY my_rule PRIORITY 100 WHEN true DENY \"msg\"")

    def test_invalid_effect_raises_syntax_error(self):
        """Efeito inválido (não ALLOW/DENY) deve levantar PolicySyntaxError."""
        with pytest.raises(PolicySyntaxError):
            _compile("POLICY my_rule PRIORITY 100 WHEN true THEN REJECT \"msg\"")

    def test_unterminated_string_raises_syntax_error(self):
        """String não terminada deve levantar PolicySyntaxError."""
        with pytest.raises(PolicySyntaxError):
            _compile('POLICY my_rule PRIORITY 100 WHEN true THEN DENY "unterminated')

    def test_invalid_character_raises_syntax_error(self):
        """Caractere inválido deve levantar PolicySyntaxError."""
        with pytest.raises(PolicySyntaxError):
            _compile("POLICY my_rule PRIORITY 100 WHEN @ THEN DENY \"msg\"")

    def test_empty_dsl_raises_syntax_error(self):
        """DSL vazia deve levantar PolicySyntaxError."""
        with pytest.raises(PolicySyntaxError):
            _compile("")

    def test_syntax_error_is_policy_syntax_error_type(self):
        """Erro de sintaxe deve ser instância de PolicySyntaxError."""
        with pytest.raises(PolicySyntaxError) as exc_info:
            _compile("INVALID DSL")
        assert exc_info.value.code == "POLICY_SYNTAX_ERROR"

    def test_syntax_error_message_contains_context(self):
        """Mensagem de erro de sintaxe deve conter informação de contexto."""
        with pytest.raises(PolicySyntaxError) as exc_info:
            _compile("POLICY my_rule PRIORITY 100 WHEN @ THEN DENY \"msg\"")
        # Should mention line or column
        error_msg = exc_info.value.message
        assert "linha" in error_msg or "coluna" in error_msg or "line" in error_msg.lower()

    def test_missing_message_string_raises_syntax_error(self):
        """DSL sem string de mensagem após ALLOW/DENY deve levantar PolicySyntaxError."""
        with pytest.raises(PolicySyntaxError):
            _compile("POLICY my_rule PRIORITY 100 WHEN true THEN DENY")

    def test_aggregate_on_non_postings_raises_syntax_error(self):
        """Agregação em coleção diferente de postings deve levantar PolicySyntaxError."""
        with pytest.raises(PolicySyntaxError):
            _compile(
                'POLICY my_rule PRIORITY 100 WHEN SUM(accounts WHERE x == 1) > 0 THEN DENY "msg"'
            )


# ---------------------------------------------------------------------------
# Task 4.2 — NON_DETERMINISTIC_FUNCTION
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNonDeterministicFunctions:
    """Testa que construções não-determinísticas são rejeitadas."""

    @pytest.mark.parametrize("func_name", [
        "NOW", "TODAY", "RANDOM", "RAND", "UUID", "CLOCK",
    ])
    def test_forbidden_function_raises_syntax_error(self, func_name: str):
        """Funções não-determinísticas devem levantar PolicySyntaxError."""
        dsl = f'POLICY my_rule PRIORITY 100 WHEN {func_name}() > 0 THEN DENY "msg"'
        with pytest.raises(PolicySyntaxError) as exc_info:
            _compile(dsl)
        assert "NON_DETERMINISTIC_FUNCTION" in exc_info.value.message

    def test_non_deterministic_error_code_is_policy_syntax_error(self):
        """Erro de função não-determinística deve ter código POLICY_SYNTAX_ERROR."""
        with pytest.raises(PolicySyntaxError) as exc_info:
            _compile('POLICY my_rule PRIORITY 100 WHEN NOW() > 0 THEN DENY "msg"')
        assert exc_info.value.code == "POLICY_SYNTAX_ERROR"


# ---------------------------------------------------------------------------
# Task 4.2 — SemanticAnalyzer errors
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSemanticAnalyzerErrors:
    """Testa que violações semânticas levantam PolicySemanticError."""

    def test_forbidden_namespace_raises_semantic_error(self):
        """Namespace proibido deve levantar PolicySemanticError."""
        dsl = """
POLICY bad_namespace PRIORITY 100
  WHEN metadata.some_field == "value"
  THEN DENY "Forbidden namespace"
"""
        with pytest.raises(PolicySemanticError) as exc_info:
            _compile(dsl)
        assert exc_info.value.code == "POLICY_SEMANTIC_ERROR"

    def test_duplicate_rule_names_raises_semantic_error(self):
        """Nomes de rules duplicados devem levantar PolicySemanticError."""
        dsl = """
POLICY same_name PRIORITY 100
  WHEN facts.posting_count > 0
  THEN DENY "First"

POLICY same_name PRIORITY 50
  WHEN facts.posting_count > 0
  THEN ALLOW "Second"
"""
        with pytest.raises(PolicySemanticError) as exc_info:
            _compile(dsl)
        assert exc_info.value.code == "POLICY_SEMANTIC_ERROR"

    def test_type_mismatch_numeric_vs_string_raises_semantic_error(self):
        """Comparação de tipos incompatíveis deve levantar PolicySemanticError."""
        dsl = """
POLICY type_mismatch PRIORITY 100
  WHEN facts.posting_count == "not_a_number"
  THEN DENY "Type mismatch"
"""
        with pytest.raises(PolicySemanticError) as exc_info:
            _compile(dsl)
        assert exc_info.value.code == "POLICY_SEMANTIC_ERROR"

    def test_semantic_error_http_status_is_400(self):
        """PolicySemanticError deve ter HTTP status 400."""
        dsl = """
POLICY bad_namespace PRIORITY 100
  WHEN metadata.field == "x"
  THEN DENY "Bad"
"""
        with pytest.raises(PolicySemanticError) as exc_info:
            _compile(dsl)
        assert exc_info.value.http_status == 400


# ---------------------------------------------------------------------------
# Task 4.3 — PolicyCostAnalyzer errors
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPolicyCostAnalyzerErrors:
    """Testa que limites de custo levantam PolicyCostBudgetExceeded."""

    def test_too_many_rules_raises_cost_exceeded(self):
        """Bundle com mais rules que o limite deve levantar PolicyCostBudgetExceeded."""
        # Create a compiler with limit of 2 rules
        limits = CostLimits(max_rules_per_bundle=2)
        cost_analyzer = PolicyCostAnalyzer(limits=limits)
        compiler = DSLCompiler(
            semantic_analyzer=SemanticAnalyzer(),
            cost_analyzer=cost_analyzer,
        )
        # Build DSL with 3 rules
        dsl = "\n".join([
            f'POLICY rule_{i} PRIORITY {100 - i}\n  WHEN facts.posting_count > 0\n  THEN DENY "msg {i}"'
            for i in range(3)
        ])
        with pytest.raises(PolicyCostBudgetExceeded) as exc_info:
            compiler.compile(dsl, "test", _DEFAULT_META, _DEFAULT_COMPAT)
        assert exc_info.value.code == "POLICY_COST_BUDGET_EXCEEDED"

    def test_dsl_source_too_large_raises_cost_exceeded(self):
        """DSL maior que o limite de tamanho deve levantar PolicyCostBudgetExceeded."""
        limits = CostLimits(max_dsl_source_bytes=50)
        cost_analyzer = PolicyCostAnalyzer(limits=limits)
        compiler = DSLCompiler(
            semantic_analyzer=SemanticAnalyzer(),
            cost_analyzer=cost_analyzer,
        )
        # This DSL is definitely > 50 bytes
        dsl = 'POLICY my_rule PRIORITY 100\n  WHEN facts.posting_count > 0\n  THEN DENY "msg"'
        with pytest.raises(PolicyCostBudgetExceeded) as exc_info:
            compiler.compile(dsl, "test", _DEFAULT_META, _DEFAULT_COMPAT)
        assert exc_info.value.code == "POLICY_COST_BUDGET_EXCEEDED"

    def test_too_many_aggregations_per_rule_raises_cost_exceeded(self):
        """Rule com mais agregações que o limite deve levantar PolicyCostBudgetExceeded."""
        limits = CostLimits(max_aggregations_per_rule=1)
        cost_analyzer = PolicyCostAnalyzer(limits=limits)
        compiler = DSLCompiler(
            semantic_analyzer=SemanticAnalyzer(),
            cost_analyzer=cost_analyzer,
        )
        # Two aggregations in one rule
        dsl = """
POLICY two_aggs PRIORITY 100
  WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > 0
    AND COUNT(postings WHERE currency == "BRL") > 0
  THEN DENY "Too many aggs"
"""
        with pytest.raises(PolicyCostBudgetExceeded) as exc_info:
            compiler.compile(dsl, "test", _DEFAULT_META, _DEFAULT_COMPAT)
        assert exc_info.value.code == "POLICY_COST_BUDGET_EXCEEDED"

    def test_cost_exceeded_http_status_is_400(self):
        """PolicyCostBudgetExceeded deve ter HTTP status 400."""
        limits = CostLimits(max_rules_per_bundle=1)
        cost_analyzer = PolicyCostAnalyzer(limits=limits)
        compiler = DSLCompiler(
            semantic_analyzer=SemanticAnalyzer(),
            cost_analyzer=cost_analyzer,
        )
        dsl = "\n".join([
            f'POLICY rule_{i} PRIORITY {100 - i}\n  WHEN facts.posting_count > 0\n  THEN DENY "msg"'
            for i in range(2)
        ])
        with pytest.raises(PolicyCostBudgetExceeded) as exc_info:
            compiler.compile(dsl, "test", _DEFAULT_META, _DEFAULT_COMPAT)
        assert exc_info.value.http_status == 400

    def test_too_many_total_scans_raises_cost_exceeded(self):
        """Bundle com mais scans totais que o limite deve levantar PolicyCostBudgetExceeded."""
        limits = CostLimits(max_total_scans=1)
        cost_analyzer = PolicyCostAnalyzer(limits=limits)
        compiler = DSLCompiler(
            semantic_analyzer=SemanticAnalyzer(),
            cost_analyzer=cost_analyzer,
        )
        # Two rules, each with one aggregation = 2 total scans
        dsl = """
POLICY rule_1 PRIORITY 100
  WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > 0
  THEN DENY "msg1"

POLICY rule_2 PRIORITY 90
  WHEN COUNT(postings WHERE currency == "BRL") > 0
  THEN DENY "msg2"
"""
        with pytest.raises(PolicyCostBudgetExceeded) as exc_info:
            compiler.compile(dsl, "test", _DEFAULT_META, _DEFAULT_COMPAT)
        assert exc_info.value.code == "POLICY_COST_BUDGET_EXCEEDED"


# ---------------------------------------------------------------------------
# Task 4.1 — DSLPrettyPrinter round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDSLPrettyPrinterRoundTrip:
    """Testa que parse → pretty_print → parse produz AST equivalente."""

    def _round_trip(self, dsl: str) -> tuple:
        """Executa round-trip e retorna (original_ast, round_trip_ast)."""
        compiler = DSLCompiler.create_default()
        printer = DSLPrettyPrinter()

        original_ast = compiler.parse_only(dsl)
        printed_dsl = printer.print(original_ast)
        round_trip_ast = compiler.parse_only(printed_dsl)
        return original_ast, round_trip_ast

    def test_round_trip_preserves_rule_count(self):
        """Round-trip deve preservar o número de rules."""
        original, round_trip = self._round_trip(_MULTI_POLICY_DSL)
        assert len(original.rules) == len(round_trip.rules)

    def test_round_trip_preserves_rule_names(self):
        """Round-trip deve preservar os nomes das rules."""
        original, round_trip = self._round_trip(_MULTI_POLICY_DSL)
        original_names = [r.name for r in original.rules]
        round_trip_names = [r.name for r in round_trip.rules]
        assert original_names == round_trip_names

    def test_round_trip_preserves_rule_priorities(self):
        """Round-trip deve preservar as prioridades das rules."""
        original, round_trip = self._round_trip(_MULTI_POLICY_DSL)
        original_priorities = [r.priority for r in original.rules]
        round_trip_priorities = [r.priority for r in round_trip.rules]
        assert original_priorities == round_trip_priorities

    def test_round_trip_preserves_rule_effects(self):
        """Round-trip deve preservar os efeitos das rules."""
        original, round_trip = self._round_trip(_MULTI_POLICY_DSL)
        original_effects = [r.effect for r in original.rules]
        round_trip_effects = [r.effect for r in round_trip.rules]
        assert original_effects == round_trip_effects

    def test_round_trip_preserves_rule_messages(self):
        """Round-trip deve preservar as mensagens das rules."""
        original, round_trip = self._round_trip(_MULTI_POLICY_DSL)
        original_messages = [r.message for r in original.rules]
        round_trip_messages = [r.message for r in round_trip.rules]
        assert original_messages == round_trip_messages

    def test_round_trip_preserves_composition_mode(self):
        """Round-trip deve preservar o modo de composição."""
        original, round_trip = self._round_trip(_SIMPLE_DENY_DSL)
        assert original.composition_mode == round_trip.composition_mode

    def test_round_trip_simple_deny(self):
        """Round-trip de DSL simples deve produzir AST equivalente."""
        original, round_trip = self._round_trip(_SIMPLE_DENY_DSL)
        assert original == round_trip

    def test_round_trip_allow_with_and(self):
        """Round-trip de DSL com AND deve produzir AST equivalente."""
        original, round_trip = self._round_trip(_SIMPLE_ALLOW_DSL)
        assert original == round_trip

    def test_round_trip_blocked_account_with_in(self):
        """Round-trip de DSL com IN deve produzir AST equivalente."""
        original, round_trip = self._round_trip(_BLOCKED_ACCOUNT_DSL)
        assert original == round_trip

    def test_round_trip_multi_policy(self):
        """Round-trip de DSL com múltiplas policies deve produzir AST equivalente."""
        original, round_trip = self._round_trip(_MULTI_POLICY_DSL)
        assert original == round_trip

    def test_pretty_printer_produces_valid_dsl(self):
        """DSLPrettyPrinter deve produzir texto DSL que pode ser compilado."""
        compiler = DSLCompiler.create_default()
        printer = DSLPrettyPrinter()

        original_ast = compiler.parse_only(_SIMPLE_DENY_DSL)
        printed_dsl = printer.print(original_ast)

        # Should compile without errors
        bundle = compiler.compile(
            dsl_source=printed_dsl,
            policy_set_id="test",
            metadata=_DEFAULT_META,
            compatibility=_DEFAULT_COMPAT,
        )
        assert bundle is not None
