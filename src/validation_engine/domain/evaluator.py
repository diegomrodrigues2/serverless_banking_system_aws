"""
RuleEvaluator — avaliador puro e determinístico de policies.

Este módulo implementa o tree-walking interpreter que avalia um RuleAST
sobre um CanonicalValidationContext e um ActivePolicySet.

Princípios de design:
- Função pura: depende exclusivamente de context e active_policy_set.
- Zero I/O: nenhuma operação de rede, disco ou relógio durante a avaliação.
- Determinismo: mesmos inputs → mesma EvaluationDecision (Requisito 9.3).
- Métricas separadas: latência é coletada mas não participa da decisão semântica.
- Fail-closed: bundles inválidos ou incompatíveis são rejeitados com erro estruturado.

Semântica DENY_OVERRIDES (Requisito 10.2, 10.3, 10.4):
- Rules são avaliadas em ordem decrescente de prioridade.
- Se qualquer rule DENY casar → veredito final = REJECTED.
- Se nenhuma rule DENY casar → veredito final = APPROVED.
- Rules ALLOW podem existir para rastreabilidade, mas não sobrepõem DENY.

Namespaces resolvidos pelo evaluator (Requisito 9.1):
- facts.*          → CanonicalValidationContext.facts (DerivedFacts)
- policy_context.* → CanonicalValidationContext.policy_context
- ref.*            → ActivePolicySet.snapshot.lookup(path)
- postings.*       → CanonicalValidationContext.postings (coleção)

Requisitos cobertos: 9.1, 9.2, 9.3, 9.5, 10.2, 10.3, 10.4, 10.5, 10.6
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from validation_engine.domain.errors import InvalidPolicyBundle, PolicyEvaluationError
from validation_engine.domain.models import (
    ActivePolicySet,
    EvaluationDecision,
    EvaluationMetrics,
    EvaluationResult,
    RuleMatchResult,
)
from validation_engine.domain.policy_ast import (
    AggregateNode,
    ASTNode,
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

if TYPE_CHECKING:
    from validation_engine.domain.context import CanonicalValidationContext

# Versão atual do evaluator — validada contra evaluator_min_version do bundle.
EVALUATOR_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Helpers de compatibilidade de versão
# ---------------------------------------------------------------------------


def _parse_version(version_str: str) -> tuple[int, ...]:
    """
    Parseia uma string de versão semver simplificada para tupla comparável.

    Exemplo:
        "1.2.3" → (1, 2, 3)
        "1.0"   → (1, 0)
    """
    try:
        return tuple(int(part) for part in version_str.split("."))
    except (ValueError, AttributeError):
        return (0,)


def _is_version_compatible(current: str, minimum_required: str) -> bool:
    """
    Verifica se a versão atual satisfaz o requisito mínimo.

    Usa comparação lexicográfica de tuplas de inteiros (semver simplificado).
    current >= minimum_required → compatível.
    """
    return _parse_version(current) >= _parse_version(minimum_required)


# ---------------------------------------------------------------------------
# Contexto de avaliação interno (escopo de uma rule)
# ---------------------------------------------------------------------------


@dataclass
class _EvalScope:
    """
    Escopo de avaliação interno passado recursivamente pelo tree-walker.

    Carrega o contexto canônico, o snapshot e um binding opcional de variável
    para avaliação de predicados dentro de coleções (WHERE clause).

    O binding_name e binding_value são usados quando o evaluator está
    iterando sobre uma coleção e precisa resolver referências ao elemento
    corrente (ex: "item.amount" dentro de um SUM(...WHERE...SELECT...)).
    """

    context: "CanonicalValidationContext"
    active_policy_set: ActivePolicySet
    # Nome da variável de binding corrente (ex: "item") — None fora de coleções
    binding_name: str | None = None
    # Valor do elemento corrente da coleção — None fora de coleções
    binding_value: Any = None

    def with_binding(self, name: str, value: Any) -> "_EvalScope":
        """Retorna um novo escopo com o binding atualizado (imutável por convenção)."""
        return _EvalScope(
            context=self.context,
            active_policy_set=self.active_policy_set,
            binding_name=name,
            binding_value=value,
        )


# ---------------------------------------------------------------------------
# RuleEvaluator — avaliador principal
# ---------------------------------------------------------------------------


class RuleEvaluator:
    """
    Avaliador puro e determinístico de policies.

    Implementa um tree-walking interpreter sobre o RuleAST do bundle.
    A avaliação é uma função pura: depende apenas de context e active_policy_set.

    Uso:
        evaluator = RuleEvaluator()
        result = evaluator.evaluate(context, active_policy_set)

    O resultado contém:
    - decision: EvaluationDecision com veredito final e detalhes das rules
    - metrics:  EvaluationMetrics com latência (não participa da decisão semântica)

    Requisito: 9.1, 9.2, 9.3, 9.5, 10.2, 10.3, 10.4, 10.5, 10.6
    """

    def evaluate(
        self,
        context: "CanonicalValidationContext",
        active_policy_set: ActivePolicySet,
    ) -> EvaluationResult:
        """
        Avalia o bundle de policies sobre o contexto canônico.

        Pipeline:
        1. Valida compatibilidade do bundle com o evaluator e o contexto.
        2. Avalia cada rule em ordem decrescente de prioridade.
        3. Aplica semântica DENY_OVERRIDES para determinar o veredito final.
        4. Coleta métricas de latência (separadas da decisão semântica).

        Args:
            context:           Contexto canônico da transação.
            active_policy_set: Conjunto de policy materializado em memória.

        Returns:
            EvaluationResult com decisão e métricas.

        Raises:
            InvalidPolicyBundle:  se o bundle for incompatível com o evaluator
                                  ou com o context_schema_version do contexto.
            PolicyEvaluationError: se ocorrer erro inesperado durante a avaliação.
        """
        # Mede latência total da avaliação (não participa da decisão semântica)
        start_time = time.monotonic()

        try:
            self._validate_bundle_compatibility(context, active_policy_set)
            decision = self._evaluate_rules(context, active_policy_set)
        except InvalidPolicyBundle:
            # Re-levanta erros de compatibilidade sem encapsular
            raise
        except Exception as exc:
            # Encapsula qualquer erro inesperado como PolicyEvaluationError
            raise PolicyEvaluationError(
                f"Erro inesperado durante avaliação de policy: {exc}"
            ) from exc

        elapsed_ms = (time.monotonic() - start_time) * 1000.0

        return EvaluationResult(
            decision=decision,
            metrics=EvaluationMetrics(
                evaluation_latency_ms=elapsed_ms,
                evaluated_rules=len(decision.rules),
            ),
        )

    # ------------------------------------------------------------------
    # Validação de compatibilidade
    # ------------------------------------------------------------------

    def _validate_bundle_compatibility(
        self,
        context: "CanonicalValidationContext",
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        Valida que o bundle é compatível com o evaluator atual e o contexto.

        Verificações realizadas:
        1. evaluator_min_version: versão atual do evaluator >= mínimo exigido pelo bundle.
        2. context_schema_version: versão do contexto == versão esperada pelo bundle.
        3. integrity_verified: o ActivePolicySet deve ter integridade verificada.

        Raises:
            InvalidPolicyBundle: se qualquer verificação falhar.
        """
        bundle = active_policy_set.bundle
        compat = bundle.compatibility

        # Verifica que a integridade do ActivePolicySet foi confirmada antes da avaliação
        if not active_policy_set.integrity_verified:
            raise InvalidPolicyBundle(
                "ActivePolicySet sem integridade verificada — bundle rejeitado por segurança"
            )

        # Verifica compatibilidade de versão do evaluator
        if not _is_version_compatible(EVALUATOR_VERSION, compat.evaluator_min_version):
            raise InvalidPolicyBundle(
                f"Evaluator versão {EVALUATOR_VERSION!r} é inferior ao mínimo exigido "
                f"pelo bundle: {compat.evaluator_min_version!r}"
            )

        # Verifica compatibilidade do context_schema_version
        if context.context_schema_version != compat.context_schema_version:
            raise InvalidPolicyBundle(
                f"context_schema_version do contexto ({context.context_schema_version!r}) "
                f"incompatível com o bundle ({compat.context_schema_version!r})"
            )

        # Verifica que o composition_mode é suportado
        if bundle.composition_mode != CompositionMode.DENY_OVERRIDES:
            raise InvalidPolicyBundle(
                f"Composition mode {bundle.composition_mode!r} não suportado. "
                "Apenas DENY_OVERRIDES é suportado nesta versão."
            )

    # ------------------------------------------------------------------
    # Avaliação das rules (semântica DENY_OVERRIDES)
    # ------------------------------------------------------------------

    def _evaluate_rules(
        self,
        context: "CanonicalValidationContext",
        active_policy_set: ActivePolicySet,
    ) -> EvaluationDecision:
        """
        Avalia todas as rules do bundle e aplica semântica DENY_OVERRIDES.

        Rules são avaliadas em ordem decrescente de prioridade.
        A primeira rule DENY que casar determina o veredito REJECTED.
        Se nenhuma rule DENY casar, o veredito é APPROVED.

        Todas as rules são avaliadas (sem short-circuit por DENY) para
        garantir que o DecisionTrail contenha o resultado completo de
        todas as rules — necessário para auditoria e analytics.

        Requisito: 10.3, 10.4, 10.5, 10.6
        """
        ast: RuleAST = active_policy_set.bundle.ast
        scope = _EvalScope(context=context, active_policy_set=active_policy_set)

        # Ordena rules por prioridade decrescente para avaliação determinística
        sorted_rules = sorted(ast.rules, key=lambda r: r.priority, reverse=True)

        rule_results: list[RuleMatchResult] = []
        matched_deny_rule: str | None = None

        for rule_node in sorted_rules:
            matched = self._evaluate_condition(rule_node.condition, scope)
            rule_results.append(
                RuleMatchResult(
                    rule_name=rule_node.name,
                    effect=rule_node.effect,
                    matched=matched,
                    priority=rule_node.priority,
                    message=rule_node.message,
                )
            )
            # Registra a primeira rule DENY que casou (para o DecisionTrail)
            if matched and rule_node.effect == PolicyEffect.DENY and matched_deny_rule is None:
                matched_deny_rule = rule_node.name

        # Aplica DENY_OVERRIDES: qualquer DENY casado → REJECTED
        final_verdict = (
            FinalVerdict.REJECTED if matched_deny_rule is not None else FinalVerdict.APPROVED
        )

        return EvaluationDecision(
            final_verdict=final_verdict,
            matched_deny_rule=matched_deny_rule,
            rules=tuple(rule_results),
        )

    # ------------------------------------------------------------------
    # Tree-walking interpreter
    # ------------------------------------------------------------------

    def _evaluate_condition(self, node: ASTNode, scope: _EvalScope) -> bool:
        """
        Avalia um nó do AST como condição booleana.

        Delega para o método de avaliação de valor e converte para bool.
        Usado para avaliar a condição de uma rule e predicados de filtro.
        """
        result = self._eval_node(node, scope)
        return bool(result)

    def _eval_node(self, node: ASTNode, scope: _EvalScope) -> Any:
        """
        Avalia um nó do AST e retorna seu valor.

        Dispatcher central do tree-walking interpreter.
        Cada tipo de nó tem seu próprio método de avaliação.

        Raises:
            PolicyEvaluationError: se o tipo de nó for desconhecido.
        """
        if isinstance(node, LiteralNode):
            return self._eval_literal(node)
        elif isinstance(node, FieldAccessNode):
            return self._eval_field_access(node, scope)
        elif isinstance(node, RefAccessNode):
            return self._eval_ref_access(node, scope)
        elif isinstance(node, ComparisonNode):
            return self._eval_comparison(node, scope)
        elif isinstance(node, LogicalOpNode):
            return self._eval_logical_op(node, scope)
        elif isinstance(node, NotOpNode):
            return self._eval_not_op(node, scope)
        elif isinstance(node, AggregateNode):
            return self._eval_aggregate(node, scope)
        elif isinstance(node, CollectionRefNode):
            # CollectionRefNode sozinho retorna a coleção inteira
            return self._resolve_collection(node, scope)
        elif isinstance(node, PredicateNode):
            # PredicateNode avaliado diretamente (fora de AggregateNode) usa o binding corrente
            return self._evaluate_condition(node.condition, scope)
        else:
            raise PolicyEvaluationError(
                f"Tipo de nó AST desconhecido durante avaliação: {type(node).__name__}"
            )

    # ------------------------------------------------------------------
    # Avaliação de nós folha
    # ------------------------------------------------------------------

    def _eval_literal(self, node: LiteralNode) -> Any:
        """Retorna o valor literal diretamente."""
        return node.value

    def _eval_field_access(self, node: FieldAccessNode, scope: _EvalScope) -> Any:
        """
        Resolve um acesso a campo do contexto canônico.

        Namespaces suportados:
        - "facts"          → DerivedFacts do contexto
        - "policy_context" → Mapping[str, str|int|bool] do contexto

        Para acesso a campos de um elemento de coleção dentro de um predicado
        (ex: "amount" dentro de SUM(postings WHERE ... SELECT amount)),
        o binding corrente é usado quando o path tem profundidade 1 e
        corresponde ao nome do binding.

        Raises:
            PolicyEvaluationError: se o namespace for desconhecido ou o campo não existir.
        """
        path = node.path
        context = scope.context

        if not path:
            raise PolicyEvaluationError("FieldAccessNode com path vazio")

        namespace = path[0]

        # Acesso a fatos derivados: facts.posting_count, facts.currencies, etc.
        if namespace == "facts":
            return self._resolve_facts_field(path[1:], context.facts)

        # Acesso a dados de contexto de policy: policy_context.daily_limit, etc.
        if namespace == "policy_context":
            if len(path) < 2:
                raise PolicyEvaluationError("policy_context requer ao menos um campo: policy_context.<campo>")
            field_name = path[1]
            value = context.policy_context.get(field_name)
            if value is None:
                raise PolicyEvaluationError(
                    f"Campo {field_name!r} não encontrado em policy_context"
                )
            return value

        # Acesso a campo do elemento corrente de coleção (dentro de predicado/select)
        # Ex: dentro de SUM(postings WHERE direction == "DEBIT" SELECT amount),
        # "amount" resolve para o campo do CanonicalPosting corrente.
        if scope.binding_name is not None and namespace == scope.binding_name:
            return self._resolve_posting_field(path[1:], scope.binding_value)

        # Acesso direto a campo de posting sem binding explícito (path de profundidade 1)
        # Suporta referências como "amount", "direction", "currency" dentro de predicados
        if scope.binding_value is not None and len(path) == 1:
            return self._resolve_posting_field(path, scope.binding_value)

        raise PolicyEvaluationError(
            f"Namespace desconhecido ou inacessível: {namespace!r}. "
            "Namespaces permitidos: facts, policy_context, ref (via RefAccessNode)"
        )

    def _resolve_facts_field(self, path: tuple[str, ...], facts: Any) -> Any:
        """
        Resolve um campo do DerivedFacts pelo path.

        Suporta acesso direto a atributos do dataclass DerivedFacts.
        """
        if not path:
            raise PolicyEvaluationError("Acesso a 'facts' requer um campo: facts.<campo>")
        field_name = path[0]
        if not hasattr(facts, field_name):
            raise PolicyEvaluationError(
                f"Campo {field_name!r} não encontrado em DerivedFacts. "
                f"Campos disponíveis: posting_count, distinct_account_count, currencies, "
                f"total_debits_by_currency, total_credits_by_currency, max_posting_amount, "
                f"has_platform_account"
            )
        return getattr(facts, field_name)

    def _resolve_posting_field(self, path: tuple[str, ...], posting: Any) -> Any:
        """
        Resolve um campo de um CanonicalPosting pelo path.

        Usado dentro de predicados e projeções de coleção.
        """
        if not path:
            return posting
        field_name = path[0]
        if not hasattr(posting, field_name):
            raise PolicyEvaluationError(
                f"Campo {field_name!r} não encontrado em CanonicalPosting. "
                f"Campos disponíveis: account_id, amount, currency, direction, account_type"
            )
        return getattr(posting, field_name)

    def _eval_ref_access(self, node: RefAccessNode, scope: _EvalScope) -> Any:
        """
        Resolve um acesso ao ReferenceSnapshot via path.

        O snapshot está em memória como parte do ActivePolicySet.
        Nenhum I/O é realizado durante a resolução.

        Raises:
            PolicyEvaluationError: se o path não existir no snapshot.
        """
        snapshot = scope.active_policy_set.snapshot
        value = snapshot.lookup(node.path)
        if value is None:
            raise PolicyEvaluationError(
                f"Chave {node.path!r} não encontrada no ReferenceSnapshot"
            )
        return value

    # ------------------------------------------------------------------
    # Avaliação de operadores de comparação
    # ------------------------------------------------------------------

    def _eval_comparison(self, node: ComparisonNode, scope: _EvalScope) -> bool:
        """
        Avalia uma comparação binária entre dois operandos.

        Operadores suportados: ==, !=, <, <=, >, >=, IN

        O operador IN verifica se o valor esquerdo está contido na
        coleção direita (tuple, list ou qualquer iterável).

        Raises:
            PolicyEvaluationError: se o operador for desconhecido.
        """
        left_val = self._eval_node(node.left, scope)
        right_val = self._eval_node(node.right, scope)
        op = node.operator

        if op == "==":
            return left_val == right_val
        elif op == "!=":
            return left_val != right_val
        elif op == "<":
            return left_val < right_val
        elif op == "<=":
            return left_val <= right_val
        elif op == ">":
            return left_val > right_val
        elif op == ">=":
            return left_val >= right_val
        elif op == "IN":
            # Verifica pertencimento: left IN right (right deve ser iterável)
            try:
                return left_val in right_val
            except TypeError as exc:
                raise PolicyEvaluationError(
                    f"Operador IN requer operando direito iterável, "
                    f"mas recebeu {type(right_val).__name__!r}"
                ) from exc
        else:
            raise PolicyEvaluationError(
                f"Operador de comparação desconhecido: {op!r}. "
                "Operadores suportados: ==, !=, <, <=, >, >=, IN"
            )

    # ------------------------------------------------------------------
    # Avaliação de operadores lógicos
    # ------------------------------------------------------------------

    def _eval_logical_op(self, node: LogicalOpNode, scope: _EvalScope) -> bool:
        """
        Avalia uma operação lógica binária (AND / OR) com curto-circuito.

        AND: retorna False imediatamente se o operando esquerdo for False.
        OR:  retorna True imediatamente se o operando esquerdo for True.

        O curto-circuito é seguro aqui porque os nós do AST são puros
        (sem efeitos colaterais), então a ordem de avaliação não afeta
        a correção semântica.

        Raises:
            PolicyEvaluationError: se o operador for desconhecido.
        """
        op = node.operator

        if op == "AND":
            # Curto-circuito: não avalia o lado direito se o esquerdo for False
            return self._evaluate_condition(node.left, scope) and self._evaluate_condition(node.right, scope)
        elif op == "OR":
            # Curto-circuito: não avalia o lado direito se o esquerdo for True
            return self._evaluate_condition(node.left, scope) or self._evaluate_condition(node.right, scope)
        else:
            raise PolicyEvaluationError(
                f"Operador lógico desconhecido: {op!r}. Operadores suportados: AND, OR"
            )

    def _eval_not_op(self, node: NotOpNode, scope: _EvalScope) -> bool:
        """Avalia a negação lógica do operando."""
        return not self._evaluate_condition(node.operand, scope)

    # ------------------------------------------------------------------
    # Avaliação de agregações sobre coleções
    # ------------------------------------------------------------------

    def _resolve_collection(
        self, node: CollectionRefNode, scope: _EvalScope
    ) -> tuple:
        """
        Resolve uma referência de coleção para a coleção concreta.

        Atualmente suporta apenas "postings".

        Raises:
            PolicyEvaluationError: se a coleção for desconhecida.
        """
        if node.name == "postings":
            return scope.context.postings
        raise PolicyEvaluationError(
            f"Coleção desconhecida: {node.name!r}. Coleções suportadas: postings"
        )

    def _eval_aggregate(self, node: AggregateNode, scope: _EvalScope) -> Any:
        """
        Avalia uma função de agregação sobre uma coleção com filtro e projeção opcionais.

        Pipeline de avaliação:
        1. Resolve a coleção (ex: postings).
        2. Aplica o filtro WHERE (se presente) — itera com binding de variável.
        3. Aplica a projeção SELECT (se presente) — extrai o campo de cada elemento.
        4. Aplica a função de agregação (SUM, COUNT, MIN, MAX, ANY, ALL).

        Funções suportadas (Requisito 23.3):
        - SUM:   soma dos valores projetados (requer SELECT numérico)
        - COUNT: contagem de elementos filtrados
        - MIN:   valor mínimo projetado
        - MAX:   valor máximo projetado
        - ANY:   True se ao menos um elemento satisfaz o filtro
        - ALL:   True se todos os elementos satisfazem o filtro

        Raises:
            PolicyEvaluationError: se a função for desconhecida ou os tipos forem incompatíveis.
        """
        collection = self._resolve_collection(node.collection, scope)
        function = node.function.upper()

        # Determina o nome do binding para iteração
        # Se o WHERE usa um PredicateNode com binding explícito, usa esse nome
        binding_name = "item"
        if isinstance(node.where, PredicateNode):
            binding_name = node.where.binding

        # Filtra a coleção aplicando o predicado WHERE com binding de variável
        filtered = self._apply_where_filter(collection, node.where, binding_name, scope)

        # ANY e ALL operam sobre a coleção filtrada sem projeção
        if function == "ANY":
            return len(filtered) > 0
        if function == "ALL":
            return len(filtered) == len(collection)

        # COUNT não requer projeção
        if function == "COUNT":
            return len(filtered)

        # SUM, MIN, MAX requerem projeção para extrair valores numéricos
        projected = self._apply_select_projection(filtered, node.select, binding_name, scope)

        if function == "SUM":
            return self._aggregate_sum(projected)
        elif function == "MIN":
            return self._aggregate_min(projected)
        elif function == "MAX":
            return self._aggregate_max(projected)
        else:
            raise PolicyEvaluationError(
                f"Função de agregação desconhecida: {function!r}. "
                "Funções suportadas: SUM, COUNT, MIN, MAX, ANY, ALL"
            )

    def _apply_where_filter(
        self,
        collection: tuple,
        where_node: ASTNode | None,
        binding_name: str,
        scope: _EvalScope,
    ) -> list:
        """
        Filtra a coleção aplicando o predicado WHERE.

        Para cada elemento da coleção, cria um escopo com o binding
        do elemento e avalia a condição do predicado.

        Se where_node for None, retorna todos os elementos.
        """
        if where_node is None:
            return list(collection)

        filtered = []
        for element in collection:
            # Cria escopo com binding do elemento corrente para resolução de campos
            element_scope = scope.with_binding(binding_name, element)

            # Avalia a condição do predicado (ou qualquer nó booleano)
            if isinstance(where_node, PredicateNode):
                condition_result = self._evaluate_condition(where_node.condition, element_scope)
            else:
                condition_result = self._evaluate_condition(where_node, element_scope)

            if condition_result:
                filtered.append(element)

        return filtered

    def _apply_select_projection(
        self,
        collection: list,
        select_node: ASTNode | None,
        binding_name: str,
        scope: _EvalScope,
    ) -> list:
        """
        Projeta a coleção extraindo o campo especificado pelo SELECT.

        Para cada elemento, cria um escopo com o binding e avalia o nó SELECT.
        Se select_node for None, retorna os elementos sem projeção.
        """
        if select_node is None:
            return collection

        projected = []
        for element in collection:
            element_scope = scope.with_binding(binding_name, element)
            value = self._eval_node(select_node, element_scope)
            projected.append(value)

        return projected

    def _aggregate_sum(self, values: list) -> Any:
        """Soma os valores da lista. Requer valores numéricos."""
        if not values:
            return 0
        try:
            return sum(values)
        except TypeError as exc:
            raise PolicyEvaluationError(
                f"SUM requer valores numéricos, mas recebeu tipos incompatíveis: {exc}"
            ) from exc

    def _aggregate_min(self, values: list) -> Any:
        """Retorna o valor mínimo da lista."""
        if not values:
            raise PolicyEvaluationError("MIN aplicado a coleção vazia")
        try:
            return min(values)
        except TypeError as exc:
            raise PolicyEvaluationError(f"MIN requer valores comparáveis: {exc}") from exc

    def _aggregate_max(self, values: list) -> Any:
        """Retorna o valor máximo da lista."""
        if not values:
            raise PolicyEvaluationError("MAX aplicado a coleção vazia")
        try:
            return max(values)
        except TypeError as exc:
            raise PolicyEvaluationError(f"MAX requer valores comparáveis: {exc}") from exc
