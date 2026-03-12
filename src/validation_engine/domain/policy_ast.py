"""
AST (Abstract Syntax Tree) tipado da Policy Rule DSL.

Este módulo define a representação intermediária (IR) das policies após parsing.
O AST é imutável, tipado e serializable — serve como contrato entre o compilador
(Control Plane) e o evaluator (Data Plane).

Princípios de design:
- Todos os nós são frozen dataclasses: imutáveis e comparáveis por valor.
- O AST não contém lógica de avaliação — apenas estrutura de dados.
- Nós são explicitamente tipados para evitar ambiguidade semântica.
- A union type ASTNode permite composição recursiva segura.

Namespaces permitidos na DSL (Requisito 23.5):
- postings.*   → coleção de CanonicalPosting
- facts.*      → DerivedFacts calculados antes da avaliação
- policy_context.* → dados de contexto fornecidos pelo chamador
- ref.*        → dados do ReferenceSnapshot em memória

Proibido (Requisito 23.6):
- acesso a relógio do sistema
- aleatoriedade
- rede, disco ou qualquer API externa
- metadata arbitrário do comando

Requisitos cobertos: 23.1, 23.2, 23.3, 23.4, 23.5, 23.7, 23.8, 23.9
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Union


# ---------------------------------------------------------------------------
# Enums de semântica de avaliação
# ---------------------------------------------------------------------------


class PolicyEffect(str, Enum):
    """
    Efeito declarado de uma rule de policy.

    ALLOW: a rule classifica ou aprova a transação.
           Não sobrepõe um DENY de outra rule.
    DENY:  a rule rejeita a transação.
           Com DENY_OVERRIDES, qualquer DENY determina o veredito final.

    Requisito: 23.7
    """

    ALLOW = "ALLOW"
    DENY = "DENY"


class FinalVerdict(str, Enum):
    """
    Veredito final da avaliação de um bundle de policies.

    APPROVED: nenhuma rule DENY casou — transação aprovada.
    REJECTED: ao menos uma rule DENY casou — transação rejeitada.

    Requisito: 10.3, 10.4
    """

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class CompositionMode(str, Enum):
    """
    Estratégia de composição das rules dentro de um bundle.

    DENY_OVERRIDES: semântica padrão e única suportada.
    Se qualquer rule DENY casar, o veredito final é REJECTED,
    independentemente de quantas rules ALLOW também casarem.

    Esta semântica é segura para domínios financeiros porque
    elimina ambiguidade entre aprovações e rejeições concorrentes.

    Requisito: 10.1, 10.2, 23.8
    """

    DENY_OVERRIDES = "DENY_OVERRIDES"


# ---------------------------------------------------------------------------
# Nós folha do AST
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiteralNode:
    """
    Nó folha que representa um valor literal na DSL.

    Exemplos de uso na DSL:
        100          → LiteralNode(value=100)
        "BRL"        → LiteralNode(value="BRL")
        true         → LiteralNode(value=True)

    O tipo do valor é preservado para verificação semântica posterior.
    Tipos permitidos: int, float, str, bool.

    Requisito: 23.1
    """

    value: int | float | str | bool


@dataclass(frozen=True)
class FieldAccessNode:
    """
    Nó de acesso a um campo do contexto canônico via path tipado.

    O path é uma tupla de strings representando a navegação hierárquica
    dentro dos namespaces permitidos do CanonicalValidationContext.

    Exemplos:
        facts.posting_count         → FieldAccessNode(path=("facts", "posting_count"))
        policy_context.daily_limit  → FieldAccessNode(path=("policy_context", "daily_limit"))

    O primeiro elemento do path deve ser um namespace permitido:
    "facts", "policy_context" (não "ref" — use RefAccessNode para isso).

    Requisito: 23.5
    """

    # Tupla imutável representando o caminho de acesso, ex: ("facts", "posting_count")
    path: tuple[str, ...]


@dataclass(frozen=True)
class RefAccessNode:
    """
    Nó de acesso a dados do ReferenceSnapshot via path tipado.

    Separado de FieldAccessNode para tornar explícita a distinção entre
    dados do contexto canônico (voláteis por request) e dados do snapshot
    (imutáveis e carregados em memória).

    Exemplos:
        ref.daily_limit_minor       → RefAccessNode(path=("daily_limit_minor",))
        ref.blocked_accounts        → RefAccessNode(path=("blocked_accounts",))

    O namespace "ref" é implícito — o path não o inclui.

    Requisito: 23.5
    """

    # Caminho dentro do snapshot, sem o prefixo "ref"
    path: tuple[str, ...]


@dataclass(frozen=True)
class CollectionRefNode:
    """
    Nó de referência a uma coleção do contexto canônico.

    Atualmente, a única coleção suportada é "postings".
    Este nó é usado como operando de AggregateNode para indicar
    sobre qual coleção a agregação opera.

    Exemplo:
        SUM(postings WHERE ...) → AggregateNode(collection=CollectionRefNode("postings"), ...)

    Requisito: 23.3, 23.4
    """

    # Nome da coleção. Atualmente apenas "postings" é suportado.
    name: str


# ---------------------------------------------------------------------------
# Nós de predicado e filtro
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PredicateNode:
    """
    Nó de predicado com binding de variável para iteração sobre coleções.

    Usado em filtros de coleção (WHERE clause) para expressar condições
    sobre cada elemento da coleção com uma variável de binding explícita.

    Exemplo na DSL:
        WHERE direction == "DEBIT"
        → PredicateNode(binding="item", condition=ComparisonNode(...))

    O binding é o nome da variável que representa cada elemento da coleção
    durante a avaliação do predicado.

    Requisito: 23.4
    """

    # Nome da variável de binding para o elemento da coleção
    binding: str
    # Condição avaliada para cada elemento com o binding
    condition: ASTNode


# ---------------------------------------------------------------------------
# Nós de agregação
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AggregateNode:
    """
    Nó de agregação sobre uma coleção com filtro e projeção opcionais.

    Suporta as funções de agregação definidas no Requisito 23.3:
    - SUM:   soma dos valores projetados
    - COUNT: contagem de elementos (com ou sem filtro)
    - MIN:   valor mínimo projetado
    - MAX:   valor máximo projetado
    - ANY:   verdadeiro se ao menos um elemento satisfaz o predicado
    - ALL:   verdadeiro se todos os elementos satisfazem o predicado

    Estrutura na DSL:
        SUM(postings WHERE direction == "DEBIT" SELECT amount)
        → AggregateNode(
              function="SUM",
              collection=CollectionRefNode("postings"),
              where=PredicateNode(binding="item", condition=...),
              select=FieldAccessNode(path=("amount",))
          )

    Requisito: 23.3, 23.4
    """

    # Função de agregação: SUM, COUNT, MIN, MAX, ANY, ALL
    function: str
    # Coleção sobre a qual a agregação opera
    collection: CollectionRefNode
    # Filtro opcional (WHERE clause) — None significa sem filtro
    where: ASTNode | None = None
    # Projeção opcional (SELECT clause) — None significa elemento inteiro
    select: ASTNode | None = None


# ---------------------------------------------------------------------------
# Nós de comparação e operadores lógicos
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComparisonNode:
    """
    Nó de comparação binária entre dois operandos.

    Operadores suportados (Requisito 23.1):
    - "=="  igualdade
    - "!="  diferença
    - "<"   menor que
    - "<="  menor ou igual
    - ">"   maior que
    - ">="  maior ou igual
    - "IN"  pertencimento a coleção/lista

    Exemplos:
        facts.posting_count >= 2
        → ComparisonNode(
              left=FieldAccessNode(("facts", "posting_count")),
              operator=">=",
              right=LiteralNode(2)
          )

    Requisito: 23.1
    """

    left: ASTNode
    # Operador de comparação como string
    operator: str
    right: ASTNode


@dataclass(frozen=True)
class LogicalOpNode:
    """
    Nó de operação lógica binária (AND / OR).

    Combina dois sub-nós com semântica de curto-circuito:
    - AND: verdadeiro somente se ambos os operandos forem verdadeiros
    - OR:  verdadeiro se ao menos um operando for verdadeiro

    Exemplo:
        facts.posting_count >= 2 AND COUNT(postings WHERE currency == "BRL") == facts.posting_count
        → LogicalOpNode(
              operator="AND",
              left=ComparisonNode(...),
              right=ComparisonNode(...)
          )

    Requisito: 23.2
    """

    # Operador lógico: "AND" ou "OR"
    operator: str
    left: ASTNode
    right: ASTNode


@dataclass(frozen=True)
class NotOpNode:
    """
    Nó de negação lógica unária (NOT).

    Inverte o valor booleano do operando.

    Exemplo:
        NOT ANY(postings WHERE account_id IN ref.blocked_accounts)
        → NotOpNode(operand=AggregateNode(...))

    Requisito: 23.2
    """

    operand: ASTNode


# ---------------------------------------------------------------------------
# Nó raiz de uma rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRuleNode:
    """
    Nó raiz que representa uma rule completa de policy.

    Uma rule declara:
    - name:     identificador único da rule dentro do bundle
    - priority: ordem de avaliação (maior prioridade = avaliado primeiro)
    - condition: expressão booleana que determina se a rule casa
    - effect:   ALLOW ou DENY — consequência quando a rule casa
    - message:  descrição legível da rule para logs e auditoria

    Exemplo na DSL:
        POLICY deny_over_daily_limit PRIORITY 100
          WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > ref.daily_limit_minor
          THEN DENY "Transaction exceeds daily debit limit"

    Requisito: 23.7, 23.8
    """

    name: str
    priority: int
    condition: ASTNode
    effect: PolicyEffect
    message: str


# ---------------------------------------------------------------------------
# Union type e container raiz do AST
# ---------------------------------------------------------------------------

# ASTNode é o tipo union de todos os nós possíveis do AST.
# Usado para tipagem recursiva nos campos dos nós compostos.
# A ordem importa para legibilidade — nós folha primeiro, compostos depois.
ASTNode = Union[
    LiteralNode,
    FieldAccessNode,
    RefAccessNode,
    CollectionRefNode,
    PredicateNode,
    AggregateNode,
    ComparisonNode,
    LogicalOpNode,
    NotOpNode,
    PolicyRuleNode,
]


@dataclass(frozen=True)
class RuleAST:
    """
    Container raiz do AST de um conjunto de policies.

    Representa o resultado do parsing de um arquivo de DSL completo,
    contendo todas as rules definidas e o modo de composição declarado.

    O RuleAST é o input do SemanticAnalyzer e do PolicyCostAnalyzer,
    e o output do DSLCompiler antes da geração do RuleBundle.

    Invariantes:
    - rules não pode ser vazia (um bundle sem rules é inválido)
    - composition_mode deve ser DENY_OVERRIDES (único modo suportado)
    - nomes de rules devem ser únicos dentro do AST

    Requisito: 23.8, 23.9
    """

    # Tupla imutável de rules na ordem em que foram declaradas na DSL
    rules: tuple[PolicyRuleNode, ...]
    # Modo de composição declarado explicitamente no bundle
    composition_mode: CompositionMode = CompositionMode.DENY_OVERRIDES
