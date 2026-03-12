"""
PolicyCostAnalyzer — análise estática de custo de um RuleAST.

Responsabilidade:
    Verificar se um RuleAST está dentro dos limites de custo estático
    definidos para manter previsibilidade de latência no write path.

Limites verificados (Requisito 19.1):
    - Regras por bundle:          máx 64
    - Profundidade máxima do AST: máx 12
    - Agregações por regra:       máx 8
    - Tamanho do DSL fonte:       máx 64 KB
    - Campos em policy_context:   máx 32
    - Scans totais por avaliação: máx 32

Quando qualquer limite é excedido, PolicyCostBudgetExceeded é levantado
com informações sobre qual limite foi violado e os valores atual/permitido.

Requisitos cobertos: 15.5, 15.6, 19.1
"""

from __future__ import annotations

from dataclasses import dataclass

from validation_engine.domain.errors import PolicyCostBudgetExceeded
from validation_engine.domain.policy_ast import (
    AggregateNode,
    ASTNode,
    CollectionRefNode,
    ComparisonNode,
    FieldAccessNode,
    LiteralNode,
    LogicalOpNode,
    NotOpNode,
    PolicyRuleNode,
    PredicateNode,
    RefAccessNode,
    RuleAST,
)


# ---------------------------------------------------------------------------
# Limites de custo estático
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostLimits:
    """
    Limites de custo estático para análise de um RuleAST.

    Todos os campos têm valores padrão correspondentes aos limites
    recomendados no design doc. Podem ser sobrescritos para testes
    ou configurações específicas de ambiente.

    Requisito: 19.1
    """

    # Número máximo de rules por bundle
    max_rules_per_bundle: int = 64
    # Profundidade máxima do AST (nível de aninhamento de nós)
    max_ast_depth: int = 12
    # Número máximo de agregações (SUM, COUNT, etc.) por rule
    max_aggregations_per_rule: int = 8
    # Tamanho máximo do texto fonte da DSL em bytes
    max_dsl_source_bytes: int = 64 * 1024  # 64 KB
    # Número máximo de campos em policy_context
    max_policy_context_fields: int = 32
    # Número máximo de scans de coleção por avaliação (soma de todas as rules)
    max_total_scans: int = 32


# ---------------------------------------------------------------------------
# PolicyCostAnalyzer
# ---------------------------------------------------------------------------


class PolicyCostAnalyzer:
    """
    Analisador estático de custo de um RuleAST.

    Verifica se o AST está dentro dos limites de custo configurados.
    Deve ser executado pelo DSLCompiler antes da geração do RuleBundle.

    Uso típico:
        analyzer = PolicyCostAnalyzer()
        analyzer.analyze(ast, dsl_source="POLICY ...")  # levanta se exceder limites

    Uso com limites customizados (ex: testes):
        limits = CostLimits(max_rules_per_bundle=2)
        analyzer = PolicyCostAnalyzer(limits=limits)
        analyzer.analyze(ast, dsl_source="...")

    Requisito: 15.5, 15.6, 19.1
    """

    def __init__(self, limits: CostLimits | None = None) -> None:
        """
        Inicializa o analisador com os limites de custo.

        Args:
            limits: Limites de custo. Se None, usa os valores padrão do design doc.
        """
        self._limits = limits or CostLimits()

    def analyze(self, ast: RuleAST, dsl_source: str = "") -> None:
        """
        Analisa o AST e verifica todos os limites de custo.

        Executa todas as verificações em sequência. A primeira violação
        encontrada levanta PolicyCostBudgetExceeded imediatamente.

        Args:
            ast:        RuleAST a analisar.
            dsl_source: Texto fonte da DSL (para verificação de tamanho).

        Raises:
            PolicyCostBudgetExceeded: se qualquer limite for excedido.
        """
        self._check_rule_count(ast)
        self._check_dsl_source_size(dsl_source)
        self._check_ast_depth(ast)
        self._check_aggregations_per_rule(ast)
        self._check_total_scans(ast)
        self._check_policy_context_fields(ast)

    # ---------------------------------------------------------------------------
    # Verificações individuais
    # ---------------------------------------------------------------------------

    def _check_rule_count(self, ast: RuleAST) -> None:
        """
        Verifica se o número de rules está dentro do limite.

        Um bundle com muitas rules aumenta o tempo de avaliação linearmente.
        O limite de 64 rules garante que a avaliação complete em tempo previsível.

        Args:
            ast: RuleAST a verificar.

        Raises:
            PolicyCostBudgetExceeded: se o número de rules exceder o limite.
        """
        rule_count = len(ast.rules)
        limit = self._limits.max_rules_per_bundle
        if rule_count > limit:
            raise PolicyCostBudgetExceeded(
                f"Bundle excede o limite de rules por bundle: "
                f"{rule_count} rules (máximo permitido: {limit})"
            )

    def _check_dsl_source_size(self, dsl_source: str) -> None:
        """
        Verifica se o tamanho do texto fonte da DSL está dentro do limite.

        Fontes muito grandes indicam policies excessivamente complexas
        que são difíceis de auditar e manter.

        Args:
            dsl_source: Texto fonte da DSL.

        Raises:
            PolicyCostBudgetExceeded: se o tamanho exceder o limite.
        """
        if not dsl_source:
            return
        source_bytes = len(dsl_source.encode("utf-8"))
        limit = self._limits.max_dsl_source_bytes
        if source_bytes > limit:
            raise PolicyCostBudgetExceeded(
                f"Fonte da DSL excede o limite de tamanho: "
                f"{source_bytes} bytes (máximo permitido: {limit} bytes)"
            )

    def _check_ast_depth(self, ast: RuleAST) -> None:
        """
        Verifica se a profundidade máxima do AST está dentro do limite.

        ASTs muito profundos indicam condições excessivamente aninhadas
        que são difíceis de entender e podem causar stack overflow.

        Args:
            ast: RuleAST a verificar.

        Raises:
            PolicyCostBudgetExceeded: se a profundidade exceder o limite.
        """
        limit = self._limits.max_ast_depth
        for rule in ast.rules:
            depth = _compute_node_depth(rule.condition)
            if depth > limit:
                raise PolicyCostBudgetExceeded(
                    f"Rule '{rule.name}' excede a profundidade máxima do AST: "
                    f"profundidade {depth} (máximo permitido: {limit})"
                )

    def _check_aggregations_per_rule(self, ast: RuleAST) -> None:
        """
        Verifica se o número de agregações por rule está dentro do limite.

        Cada agregação (SUM, COUNT, etc.) requer um scan da coleção de postings.
        Muitas agregações por rule aumentam o custo de avaliação.

        Args:
            ast: RuleAST a verificar.

        Raises:
            PolicyCostBudgetExceeded: se o número de agregações exceder o limite.
        """
        limit = self._limits.max_aggregations_per_rule
        for rule in ast.rules:
            agg_count = _count_aggregations(rule.condition)
            if agg_count > limit:
                raise PolicyCostBudgetExceeded(
                    f"Rule '{rule.name}' excede o limite de agregações por rule: "
                    f"{agg_count} agregações (máximo permitido: {limit})"
                )

    def _check_total_scans(self, ast: RuleAST) -> None:
        """
        Verifica se o número total de scans por avaliação está dentro do limite.

        O total de scans é a soma de todas as agregações em todas as rules.
        Cada scan percorre a coleção de postings inteira.

        Args:
            ast: RuleAST a verificar.

        Raises:
            PolicyCostBudgetExceeded: se o total de scans exceder o limite.
        """
        total_scans = sum(_count_aggregations(rule.condition) for rule in ast.rules)
        limit = self._limits.max_total_scans
        if total_scans > limit:
            raise PolicyCostBudgetExceeded(
                f"Bundle excede o limite de scans totais por avaliação: "
                f"{total_scans} scans (máximo permitido: {limit})"
            )

    def _check_policy_context_fields(self, ast: RuleAST) -> None:
        """
        Verifica se o número de campos de policy_context referenciados está dentro do limite.

        Conta os campos únicos de policy_context referenciados em todo o AST.
        Muitos campos indicam um contexto excessivamente complexo.

        Args:
            ast: RuleAST a verificar.

        Raises:
            PolicyCostBudgetExceeded: se o número de campos exceder o limite.
        """
        policy_context_fields: set[str] = set()
        for rule in ast.rules:
            _collect_policy_context_fields(rule.condition, policy_context_fields)

        field_count = len(policy_context_fields)
        limit = self._limits.max_policy_context_fields
        if field_count > limit:
            raise PolicyCostBudgetExceeded(
                f"Bundle referencia campos demais em policy_context: "
                f"{field_count} campos (máximo permitido: {limit})"
            )


# ---------------------------------------------------------------------------
# Funções auxiliares de análise do AST
# ---------------------------------------------------------------------------


def _compute_node_depth(node: ASTNode) -> int:
    """
    Calcula a profundidade máxima de um nó do AST.

    A profundidade é o número de níveis de aninhamento a partir do nó raiz.
    Nós folha (LiteralNode, FieldAccessNode, etc.) têm profundidade 1.

    Args:
        node: Nó do AST a analisar.

    Returns:
        Profundidade máxima do nó e seus descendentes.
    """
    if isinstance(node, (LiteralNode, FieldAccessNode, RefAccessNode, CollectionRefNode)):
        # Nós folha: profundidade 1
        return 1
    elif isinstance(node, PredicateNode):
        return 1 + _compute_node_depth(node.condition)
    elif isinstance(node, AggregateNode):
        # Profundidade do where e select, mais 1 para o próprio nó
        depths = [1]
        if node.where is not None:
            depths.append(1 + _compute_node_depth(node.where))
        if node.select is not None:
            depths.append(1 + _compute_node_depth(node.select))
        return max(depths)
    elif isinstance(node, ComparisonNode):
        return 1 + max(_compute_node_depth(node.left), _compute_node_depth(node.right))
    elif isinstance(node, LogicalOpNode):
        return 1 + max(_compute_node_depth(node.left), _compute_node_depth(node.right))
    elif isinstance(node, NotOpNode):
        return 1 + _compute_node_depth(node.operand)
    elif isinstance(node, PolicyRuleNode):
        return 1 + _compute_node_depth(node.condition)
    else:
        # Nó desconhecido: profundidade 1 por segurança
        return 1


def _count_aggregations(node: ASTNode) -> int:
    """
    Conta o número de nós AggregateNode em uma subárvore do AST.

    Cada AggregateNode representa um scan da coleção de postings.

    Args:
        node: Nó raiz da subárvore a analisar.

    Returns:
        Número total de AggregateNodes na subárvore.
    """
    if isinstance(node, (LiteralNode, FieldAccessNode, RefAccessNode, CollectionRefNode)):
        return 0
    elif isinstance(node, AggregateNode):
        # Conta este nó mais qualquer agregação aninhada no where/select
        count = 1
        if node.where is not None:
            count += _count_aggregations(node.where)
        if node.select is not None:
            count += _count_aggregations(node.select)
        return count
    elif isinstance(node, PredicateNode):
        return _count_aggregations(node.condition)
    elif isinstance(node, ComparisonNode):
        return _count_aggregations(node.left) + _count_aggregations(node.right)
    elif isinstance(node, LogicalOpNode):
        return _count_aggregations(node.left) + _count_aggregations(node.right)
    elif isinstance(node, NotOpNode):
        return _count_aggregations(node.operand)
    elif isinstance(node, PolicyRuleNode):
        return _count_aggregations(node.condition)
    else:
        return 0


def _collect_policy_context_fields(node: ASTNode, fields: set[str]) -> None:
    """
    Coleta todos os campos de policy_context referenciados em uma subárvore.

    Percorre o AST recursivamente e adiciona ao conjunto `fields` todos os
    campos acessados via FieldAccessNode com namespace "policy_context".

    Args:
        node:   Nó raiz da subárvore a analisar.
        fields: Conjunto mutável onde os campos encontrados são adicionados.
    """
    if isinstance(node, FieldAccessNode):
        # Verifica se o acesso é ao namespace policy_context
        if len(node.path) >= 2 and node.path[0] == "policy_context":
            fields.add(node.path[1])
    elif isinstance(node, (LiteralNode, RefAccessNode, CollectionRefNode)):
        pass  # Nós folha sem campos de policy_context
    elif isinstance(node, AggregateNode):
        if node.where is not None:
            _collect_policy_context_fields(node.where, fields)
        if node.select is not None:
            _collect_policy_context_fields(node.select, fields)
    elif isinstance(node, PredicateNode):
        _collect_policy_context_fields(node.condition, fields)
    elif isinstance(node, ComparisonNode):
        _collect_policy_context_fields(node.left, fields)
        _collect_policy_context_fields(node.right, fields)
    elif isinstance(node, LogicalOpNode):
        _collect_policy_context_fields(node.left, fields)
        _collect_policy_context_fields(node.right, fields)
    elif isinstance(node, NotOpNode):
        _collect_policy_context_fields(node.operand, fields)
    elif isinstance(node, PolicyRuleNode):
        _collect_policy_context_fields(node.condition, fields)
