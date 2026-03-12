"""
DSLCompiler, SemanticAnalyzer e DSLPrettyPrinter para a Policy Rule DSL.

Responsabilidades:
    DSLCompiler:       Parseia texto DSL → RuleAST → RuleBundle.
    SemanticAnalyzer:  Valida tipos, namespaces e construções proibidas.
    DSLPrettyPrinter:  Converte RuleAST → texto DSL válido (round-trip).

Gramática suportada:
    POLICY <name> PRIORITY <int>
      WHEN <condition>
      THEN ALLOW|DENY "<message>"

    <condition> ::= <or_expr>
    <or_expr>   ::= <and_expr> (OR <and_expr>)*
    <and_expr>  ::= <not_expr> (AND <not_expr>)*
    <not_expr>  ::= NOT <not_expr> | <comparison>
    <comparison>::= <primary> (<op> <primary>)?
    <op>        ::= == | != | < | <= | > | >= | IN
    <primary>   ::= <aggregate> | <field_access> | <literal> | ( <condition> )
    <aggregate> ::= FUNC ( postings [WHERE <predicate>] [SELECT <field>] )
    <field_access> ::= facts.<name> | policy_context.<name> | ref.<name>
    <literal>   ::= <int> | <float> | "<string>" | true | false

Requisitos cobertos: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 15.1, 15.2, 15.3,
                     15.4, 15.5, 15.6, 23.6
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from validation_engine.domain.cost_analyzer import PolicyCostAnalyzer
from validation_engine.domain.errors import PolicySemanticError, PolicySyntaxError
from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
    RuleBundle,
)
from validation_engine.domain.policy_ast import (
    AggregateNode,
    ASTNode,
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

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Token types and Lexer
# ---------------------------------------------------------------------------

# Non-deterministic function names that are explicitly forbidden (Req 23.6)
_FORBIDDEN_FUNCTIONS = frozenset({
    "NOW", "TODAY", "RANDOM", "RAND", "UUID", "CLOCK", "TIME", "DATE",
    "DATETIME", "SLEEP", "HTTP", "FETCH", "READ", "WRITE", "OPEN",
    "EXEC", "EVAL", "IMPORT",
})

# Aggregate functions allowed in the DSL
_AGGREGATE_FUNCTIONS = frozenset({"SUM", "COUNT", "MIN", "MAX", "ANY", "ALL"})

# Allowed field namespaces (first segment of a dotted path)
_ALLOWED_NAMESPACES = frozenset({"facts", "policy_context", "ref", "postings"})

# Comparison operators
_COMPARISON_OPS = frozenset({"==", "!=", "<=", ">=", "<", ">", "IN"})


@dataclass(frozen=True)
class Token:
    """
    Token produzido pelo lexer da DSL.

    Attributes:
        kind:   Tipo do token (ex: "IDENT", "INT", "STRING", "OP", "KEYWORD").
        value:  Valor textual do token.
        line:   Linha de origem (1-indexed).
        column: Coluna de origem (1-indexed).
    """

    kind: str
    value: str
    line: int
    column: int


def _tokenize(source: str) -> list[Token]:
    """
    Tokeniza o texto fonte da DSL em uma lista de Tokens.

    Reconhece:
    - Palavras-chave e identificadores (IDENT)
    - Inteiros e floats (INT, FLOAT)
    - Strings entre aspas duplas (STRING)
    - Operadores de comparação (OP)
    - Parênteses e pontuação (LPAREN, RPAREN, DOT, COMMA)
    - Ignora espaços em branco e comentários (#)

    Args:
        source: Texto fonte da DSL.

    Returns:
        Lista de tokens.

    Raises:
        PolicySyntaxError: se um caractere inválido for encontrado.
    """
    tokens: list[Token] = []
    line = 1
    col = 1
    i = 0
    n = len(source)

    while i < n:
        ch = source[i]

        # Newline
        if ch == "\n":
            line += 1
            col = 1
            i += 1
            continue

        # Whitespace
        if ch in " \t\r":
            col += 1
            i += 1
            continue

        # Comment: skip to end of line
        if ch == "#":
            while i < n and source[i] != "\n":
                i += 1
            continue

        # String literal
        if ch == '"':
            start_col = col
            i += 1
            col += 1
            buf = []
            while i < n and source[i] != '"':
                if source[i] == "\n":
                    raise PolicySyntaxError(
                        f"String não terminada na linha {line}, coluna {start_col}"
                    )
                buf.append(source[i])
                i += 1
                col += 1
            if i >= n:
                raise PolicySyntaxError(
                    f"String não terminada na linha {line}, coluna {start_col}"
                )
            i += 1  # consume closing quote
            col += 1
            tokens.append(Token("STRING", "".join(buf), line, start_col))
            continue

        # Two-character operators: <=, >=, ==, !=
        if i + 1 < n and source[i:i+2] in ("<=", ">=", "==", "!="):
            tokens.append(Token("OP", source[i:i+2], line, col))
            col += 2
            i += 2
            continue

        # Single-character operators and punctuation
        if ch in "<>":
            tokens.append(Token("OP", ch, line, col))
            col += 1
            i += 1
            continue

        if ch == "(":
            tokens.append(Token("LPAREN", ch, line, col))
            col += 1
            i += 1
            continue

        if ch == ")":
            tokens.append(Token("RPAREN", ch, line, col))
            col += 1
            i += 1
            continue

        if ch == ".":
            tokens.append(Token("DOT", ch, line, col))
            col += 1
            i += 1
            continue

        if ch == ",":
            tokens.append(Token("COMMA", ch, line, col))
            col += 1
            i += 1
            continue

        # Number literal (int or float)
        if ch.isdigit() or (ch == "-" and i + 1 < n and source[i+1].isdigit()):
            start_col = col
            buf = [ch]
            i += 1
            col += 1
            while i < n and (source[i].isdigit() or source[i] == "."):
                buf.append(source[i])
                i += 1
                col += 1
            num_str = "".join(buf)
            kind = "FLOAT" if "." in num_str else "INT"
            tokens.append(Token(kind, num_str, line, start_col))
            continue

        # Identifier or keyword
        if ch.isalpha() or ch == "_":
            start_col = col
            buf = [ch]
            i += 1
            col += 1
            while i < n and (source[i].isalnum() or source[i] == "_"):
                buf.append(source[i])
                i += 1
                col += 1
            word = "".join(buf)
            tokens.append(Token("IDENT", word, line, start_col))
            continue

        raise PolicySyntaxError(
            f"Caractere inválido '{ch}' na linha {line}, coluna {col}"
        )

    return tokens


# ---------------------------------------------------------------------------
# Recursive Descent Parser
# ---------------------------------------------------------------------------


class _Parser:
    """
    Parser de descida recursiva para a Policy Rule DSL.

    Consome uma lista de tokens e produz um RuleAST.
    Levanta PolicySyntaxError com informações de linha/coluna em caso de erro.

    Gramática implementada:
        program     ::= policy_rule+
        policy_rule ::= POLICY <name> PRIORITY <int> WHEN <condition> THEN <effect> <message>
        condition   ::= or_expr
        or_expr     ::= and_expr (OR and_expr)*
        and_expr    ::= not_expr (AND not_expr)*
        not_expr    ::= NOT not_expr | comparison
        comparison  ::= primary (op primary)?
        op          ::= == | != | < | <= | > | >= | IN
        primary     ::= aggregate | field_access | literal | ( condition )
        aggregate   ::= FUNC ( postings [WHERE predicate] [SELECT field] )
        field_access::= IDENT.IDENT[.IDENT]*
        literal     ::= INT | FLOAT | STRING | true | false
    """

    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    def _peek(self) -> Token | None:
        """Retorna o token atual sem consumir."""
        if self._pos < len(self._tokens):
            return self._tokens[self._pos]
        return None

    def _peek_value(self) -> str:
        """Retorna o valor do token atual em maiúsculas, ou '' se EOF."""
        tok = self._peek()
        return tok.value.upper() if tok else ""

    def _advance(self) -> Token:
        """Consome e retorna o token atual."""
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect_ident(self, value: str | None = None) -> Token:
        """
        Consome um token IDENT, opcionalmente verificando o valor (case-insensitive).

        Args:
            value: Valor esperado em maiúsculas. Se None, aceita qualquer IDENT.

        Returns:
            Token consumido.

        Raises:
            PolicySyntaxError: se o token não for IDENT ou não tiver o valor esperado.
        """
        tok = self._peek()
        if tok is None:
            raise PolicySyntaxError(
                f"Fim inesperado da entrada — esperado {'IDENT' if value is None else repr(value)}"
            )
        if tok.kind != "IDENT":
            raise PolicySyntaxError(
                f"Esperado identificador na linha {tok.line}, coluna {tok.column}, "
                f"encontrado '{tok.value}'"
            )
        if value is not None and tok.value.upper() != value.upper():
            raise PolicySyntaxError(
                f"Esperado '{value}' na linha {tok.line}, coluna {tok.column}, "
                f"encontrado '{tok.value}'"
            )
        return self._advance()

    def _expect_token(self, kind: str, value: str | None = None) -> Token:
        """
        Consome um token do tipo especificado, opcionalmente verificando o valor.

        Args:
            kind:  Tipo esperado do token.
            value: Valor esperado. Se None, aceita qualquer valor do tipo.

        Returns:
            Token consumido.

        Raises:
            PolicySyntaxError: se o token não corresponder ao esperado.
        """
        tok = self._peek()
        if tok is None:
            raise PolicySyntaxError(
                f"Fim inesperado da entrada — esperado {kind}"
            )
        if tok.kind != kind:
            raise PolicySyntaxError(
                f"Esperado {kind} na linha {tok.line}, coluna {tok.column}, "
                f"encontrado '{tok.value}' ({tok.kind})"
            )
        if value is not None and tok.value != value:
            raise PolicySyntaxError(
                f"Esperado '{value}' na linha {tok.line}, coluna {tok.column}, "
                f"encontrado '{tok.value}'"
            )
        return self._advance()

    def parse_program(self) -> RuleAST:
        """
        Parseia o programa completo e retorna um RuleAST.

        Um programa é uma sequência de uma ou mais policy rules.

        Returns:
            RuleAST com todas as rules parseadas.

        Raises:
            PolicySyntaxError: se a entrada estiver malformada.
        """
        rules = []
        while self._peek() is not None:
            rules.append(self._parse_policy_rule())

        if not rules:
            raise PolicySyntaxError("DSL vazia — pelo menos uma POLICY é obrigatória")

        return RuleAST(
            rules=tuple(rules),
            composition_mode=CompositionMode.DENY_OVERRIDES,
        )

    def _parse_policy_rule(self) -> PolicyRuleNode:
        """
        Parseia uma policy rule completa.

        Formato:
            POLICY <name> PRIORITY <int>
              WHEN <condition>
              THEN ALLOW|DENY "<message>"

        Returns:
            PolicyRuleNode com todos os campos preenchidos.

        Raises:
            PolicySyntaxError: se a estrutura da rule estiver malformada.
        """
        # POLICY keyword
        tok = self._peek()
        if tok is None:
            raise PolicySyntaxError("Fim inesperado — esperado POLICY")
        if tok.kind != "IDENT" or tok.value.upper() != "POLICY":
            raise PolicySyntaxError(
                f"Esperado 'POLICY' na linha {tok.line}, coluna {tok.column}, "
                f"encontrado '{tok.value}'"
            )
        self._advance()

        # Rule name
        name_tok = self._peek()
        if name_tok is None or name_tok.kind != "IDENT":
            raise PolicySyntaxError("Esperado nome da policy após POLICY")
        rule_name = name_tok.value
        self._advance()

        # PRIORITY keyword
        self._expect_ident("PRIORITY")

        # Priority value
        prio_tok = self._peek()
        if prio_tok is None or prio_tok.kind != "INT":
            raise PolicySyntaxError(
                f"Esperado inteiro após PRIORITY na linha "
                f"{prio_tok.line if prio_tok else '?'}"
            )
        priority = int(prio_tok.value)
        self._advance()

        # WHEN keyword
        self._expect_ident("WHEN")

        # Condition expression
        condition = self._parse_condition()

        # THEN keyword
        self._expect_ident("THEN")

        # Effect: ALLOW or DENY
        effect_tok = self._peek()
        if effect_tok is None or effect_tok.kind != "IDENT":
            raise PolicySyntaxError("Esperado ALLOW ou DENY após THEN")
        effect_upper = effect_tok.value.upper()
        if effect_upper not in ("ALLOW", "DENY"):
            raise PolicySyntaxError(
                f"Esperado ALLOW ou DENY na linha {effect_tok.line}, "
                f"coluna {effect_tok.column}, encontrado '{effect_tok.value}'"
            )
        effect = PolicyEffect.ALLOW if effect_upper == "ALLOW" else PolicyEffect.DENY
        self._advance()

        # Message string
        msg_tok = self._peek()
        if msg_tok is None or msg_tok.kind != "STRING":
            raise PolicySyntaxError(
                f"Esperado string de mensagem após {effect_upper}"
            )
        message = msg_tok.value
        self._advance()

        return PolicyRuleNode(
            name=rule_name,
            priority=priority,
            condition=condition,
            effect=effect,
            message=message,
        )

    def _parse_condition(self) -> ASTNode:
        """Parseia uma condição (or_expr)."""
        return self._parse_or_expr()

    def _parse_or_expr(self) -> ASTNode:
        """Parseia uma expressão OR: and_expr (OR and_expr)*"""
        left = self._parse_and_expr()
        while self._peek_value() == "OR":
            self._advance()
            right = self._parse_and_expr()
            left = LogicalOpNode(operator="OR", left=left, right=right)
        return left

    def _parse_and_expr(self) -> ASTNode:
        """Parseia uma expressão AND: not_expr (AND not_expr)*"""
        left = self._parse_not_expr()
        while self._peek_value() == "AND":
            self._advance()
            right = self._parse_not_expr()
            left = LogicalOpNode(operator="AND", left=left, right=right)
        return left

    def _parse_not_expr(self) -> ASTNode:
        """Parseia uma expressão NOT: NOT not_expr | comparison"""
        if self._peek_value() == "NOT":
            self._advance()
            operand = self._parse_not_expr()
            return NotOpNode(operand=operand)
        return self._parse_comparison()

    def _parse_comparison(self) -> ASTNode:
        """Parseia uma comparação: primary (op primary)?"""
        left = self._parse_primary()

        tok = self._peek()
        if tok is None:
            return left

        # Check for comparison operator
        if tok.kind == "OP" and tok.value in _COMPARISON_OPS:
            op = tok.value
            self._advance()
            right = self._parse_primary()
            return ComparisonNode(left=left, operator=op, right=right)

        # Check for IN keyword
        if tok.kind == "IDENT" and tok.value.upper() == "IN":
            self._advance()
            right = self._parse_primary()
            return ComparisonNode(left=left, operator="IN", right=right)

        return left

    def _parse_primary(self) -> ASTNode:
        """
        Parseia um valor primário: aggregate, field_access, literal ou ( condition ).
        """
        tok = self._peek()
        if tok is None:
            raise PolicySyntaxError("Fim inesperado — esperado expressão primária")

        # Parenthesized expression
        if tok.kind == "LPAREN":
            self._advance()
            expr = self._parse_condition()
            self._expect_token("RPAREN")
            return expr

        # Integer literal
        if tok.kind == "INT":
            self._advance()
            return LiteralNode(value=int(tok.value))

        # Float literal
        if tok.kind == "FLOAT":
            self._advance()
            return LiteralNode(value=float(tok.value))

        # String literal
        if tok.kind == "STRING":
            self._advance()
            return LiteralNode(value=tok.value)

        # Identifier: could be keyword literal, aggregate function, or field access
        if tok.kind == "IDENT":
            upper = tok.value.upper()

            # Boolean literals
            if upper == "TRUE":
                self._advance()
                return LiteralNode(value=True)
            if upper == "FALSE":
                self._advance()
                return LiteralNode(value=False)

            # Forbidden non-deterministic functions
            if upper in _FORBIDDEN_FUNCTIONS:
                raise PolicySyntaxError(
                    f"Função não-determinística proibida '{tok.value}' na linha "
                    f"{tok.line}, coluna {tok.column} — código: NON_DETERMINISTIC_FUNCTION"
                )

            # Aggregate function: FUNC(postings ...)
            if upper in _AGGREGATE_FUNCTIONS:
                return self._parse_aggregate(tok)

            # Field access: namespace.field or namespace.field.subfield
            return self._parse_field_access(tok)

        raise PolicySyntaxError(
            f"Token inesperado '{tok.value}' ({tok.kind}) na linha {tok.line}, "
            f"coluna {tok.column}"
        )

    def _parse_aggregate(self, func_tok: Token) -> AggregateNode:
        """
        Parseia uma expressão de agregação.

        Formato:
            FUNC ( postings [WHERE <predicate>] [SELECT <field>] )

        Args:
            func_tok: Token com o nome da função de agregação.

        Returns:
            AggregateNode com todos os campos preenchidos.
        """
        func_name = func_tok.value.upper()
        self._advance()  # consume function name

        self._expect_token("LPAREN")

        # Collection name — must be "postings"
        coll_tok = self._peek()
        if coll_tok is None or coll_tok.kind != "IDENT":
            raise PolicySyntaxError(
                f"Esperado nome de coleção após '{func_name}(' na linha "
                f"{func_tok.line}"
            )
        if coll_tok.value.lower() != "postings":
            raise PolicySyntaxError(
                f"Agregações só são permitidas sobre a coleção 'postings', "
                f"encontrado '{coll_tok.value}' na linha {coll_tok.line}, "
                f"coluna {coll_tok.column}"
            )
        self._advance()
        collection = CollectionRefNode(name="postings")

        # Optional WHERE clause
        where_node: ASTNode | None = None
        if self._peek_value() == "WHERE":
            self._advance()
            # Parse the predicate condition (comparison or logical expression)
            where_condition = self._parse_condition()
            where_node = PredicateNode(binding="item", condition=where_condition)

        # Optional SELECT clause
        select_node: ASTNode | None = None
        if self._peek_value() == "SELECT":
            self._advance()
            # SELECT expects a simple field name (relative to posting item)
            field_tok = self._peek()
            if field_tok is None or field_tok.kind != "IDENT":
                raise PolicySyntaxError(
                    f"Esperado nome de campo após SELECT na linha {func_tok.line}"
                )
            field_name = field_tok.value
            self._advance()
            # Represent as FieldAccessNode with single-element path (posting field)
            select_node = FieldAccessNode(path=(field_name,))

        self._expect_token("RPAREN")

        return AggregateNode(
            function=func_name,
            collection=collection,
            where=where_node,
            select=select_node,
        )

    def _parse_field_access(self, first_tok: Token) -> ASTNode:
        """
        Parseia um acesso a campo: namespace.field[.subfield]*

        Namespaces permitidos: facts, policy_context, ref, postings.
        O namespace "ref" produz RefAccessNode; os demais produzem FieldAccessNode.

        Args:
            first_tok: Primeiro token do identificador (namespace).

        Returns:
            FieldAccessNode ou RefAccessNode.

        Raises:
            PolicySyntaxError: se o namespace não for permitido.
        """
        namespace = first_tok.value
        self._advance()  # consume namespace token

        # Expect dot separator
        if self._peek() is None or self._peek().kind != "DOT":
            # Single identifier without dot — treat as bare field name
            # (used in WHERE clauses for posting fields like "direction", "amount")
            return FieldAccessNode(path=(namespace,))

        # Consume dot and field name
        self._advance()  # consume DOT
        field_tok = self._peek()
        if field_tok is None or field_tok.kind != "IDENT":
            raise PolicySyntaxError(
                f"Esperado nome de campo após '{namespace}.' na linha "
                f"{first_tok.line}, coluna {first_tok.column}"
            )
        field_name = field_tok.value
        self._advance()

        # Check for additional dot-separated segments
        path_parts = [namespace, field_name]
        while self._peek() is not None and self._peek().kind == "DOT":
            self._advance()  # consume DOT
            seg_tok = self._peek()
            if seg_tok is None or seg_tok.kind != "IDENT":
                raise PolicySyntaxError(
                    f"Esperado segmento de campo após '.' na linha {first_tok.line}"
                )
            path_parts.append(seg_tok.value)
            self._advance()

        # Produce RefAccessNode for "ref.*" paths
        if namespace.lower() == "ref":
            return RefAccessNode(path=tuple(path_parts[1:]))

        return FieldAccessNode(path=tuple(path_parts))


# ---------------------------------------------------------------------------
# SemanticAnalyzer
# ---------------------------------------------------------------------------


class SemanticAnalyzer:
    """
    Analisador semântico da Policy Rule DSL.

    Verifica:
    - Namespaces permitidos: apenas facts.*, policy_context.*, ref.*, postings.*
    - Construções não-determinísticas proibidas (relógio, aleatoriedade, rede, etc.)
    - Agregações apenas sobre a coleção "postings"
    - Compatibilidade de tipos em comparações (numérico vs numérico, string vs string)
    - Nomes de rules únicos dentro do bundle

    Requisito: 15.1, 15.2, 15.3, 15.4, 23.6
    """

    def analyze(self, ast: RuleAST) -> None:
        """
        Executa a análise semântica completa do AST.

        Args:
            ast: RuleAST a analisar.

        Raises:
            PolicySemanticError: se qualquer violação semântica for encontrada.
            PolicySyntaxError:   se construção não-determinística for detectada.
        """
        self._check_unique_rule_names(ast)
        for rule in ast.rules:
            self._analyze_node(rule.condition)

    def _check_unique_rule_names(self, ast: RuleAST) -> None:
        """
        Verifica que todos os nomes de rules são únicos no bundle.

        Args:
            ast: RuleAST a verificar.

        Raises:
            PolicySemanticError: se houver nomes duplicados.
        """
        seen: set[str] = set()
        for rule in ast.rules:
            if rule.name in seen:
                raise PolicySemanticError(
                    f"Nome de rule duplicado: '{rule.name}' — "
                    f"cada rule deve ter um nome único no bundle"
                )
            seen.add(rule.name)

    def _analyze_node(self, node: ASTNode) -> str:
        """
        Analisa um nó do AST e retorna seu tipo inferido.

        Tipos inferidos: "numeric", "string", "bool", "collection", "unknown"

        Args:
            node: Nó a analisar.

        Returns:
            Tipo inferido do nó como string.

        Raises:
            PolicySemanticError: se violação semântica for encontrada.
            PolicySyntaxError:   se construção não-determinística for detectada.
        """
        if isinstance(node, LiteralNode):
            return self._type_of_literal(node)

        elif isinstance(node, FieldAccessNode):
            return self._analyze_field_access(node)

        elif isinstance(node, RefAccessNode):
            # ref.* — tipo desconhecido em tempo de compilação (depende do snapshot)
            return "unknown"

        elif isinstance(node, CollectionRefNode):
            if node.name != "postings":
                raise PolicySemanticError(
                    f"Coleção '{node.name}' não é permitida — apenas 'postings' é suportada"
                )
            return "collection"

        elif isinstance(node, PredicateNode):
            self._analyze_node(node.condition)
            return "bool"

        elif isinstance(node, AggregateNode):
            return self._analyze_aggregate(node)

        elif isinstance(node, ComparisonNode):
            return self._analyze_comparison(node)

        elif isinstance(node, LogicalOpNode):
            left_type = self._analyze_node(node.left)
            right_type = self._analyze_node(node.right)
            # Both sides of AND/OR should be boolean-compatible
            if left_type not in ("bool", "unknown") or right_type not in ("bool", "unknown"):
                raise PolicySemanticError(
                    f"Operador lógico '{node.operator}' requer operandos booleanos, "
                    f"encontrado '{left_type}' e '{right_type}'"
                )
            return "bool"

        elif isinstance(node, NotOpNode):
            operand_type = self._analyze_node(node.operand)
            if operand_type not in ("bool", "unknown"):
                raise PolicySemanticError(
                    f"Operador NOT requer operando booleano, encontrado '{operand_type}'"
                )
            return "bool"

        elif isinstance(node, PolicyRuleNode):
            self._analyze_node(node.condition)
            return "bool"

        return "unknown"

    def _type_of_literal(self, node: LiteralNode) -> str:
        """Infere o tipo de um nó literal."""
        if isinstance(node.value, bool):
            return "bool"
        if isinstance(node.value, (int, float)):
            return "numeric"
        if isinstance(node.value, str):
            return "string"
        return "unknown"

    def _analyze_field_access(self, node: FieldAccessNode) -> str:
        """
        Valida um acesso a campo e infere seu tipo.

        Namespaces permitidos: facts, policy_context, postings (e bare field names).
        Namespace "ref" é tratado via RefAccessNode.

        Args:
            node: FieldAccessNode a analisar.

        Returns:
            Tipo inferido do campo.

        Raises:
            PolicySemanticError: se o namespace não for permitido.
        """
        if not node.path:
            raise PolicySemanticError("FieldAccessNode com path vazio")

        namespace = node.path[0].lower()

        # Single-element path: bare field name (used in WHERE/SELECT clauses)
        if len(node.path) == 1:
            # Bare field names are allowed in aggregate WHERE/SELECT contexts
            return "unknown"

        # Validate namespace
        if namespace not in _ALLOWED_NAMESPACES:
            raise PolicySemanticError(
                f"Namespace '{node.path[0]}' não é permitido na DSL — "
                f"namespaces permitidos: {', '.join(sorted(_ALLOWED_NAMESPACES))}"
            )

        # Infer type for known facts fields
        if namespace == "facts":
            return self._infer_facts_field_type(node.path[1] if len(node.path) > 1 else "")

        # policy_context and postings fields: type unknown at compile time
        return "unknown"

    def _infer_facts_field_type(self, field_name: str) -> str:
        """
        Infere o tipo de um campo do namespace facts.

        Campos numéricos conhecidos retornam "numeric".
        Campos booleanos conhecidos retornam "bool".
        Campos desconhecidos retornam "unknown".

        Args:
            field_name: Nome do campo em facts.

        Returns:
            Tipo inferido.
        """
        numeric_fields = {
            "posting_count", "distinct_account_count", "max_posting_amount",
        }
        bool_fields = {"has_platform_account"}

        if field_name in numeric_fields:
            return "numeric"
        if field_name in bool_fields:
            return "bool"
        return "unknown"

    def _analyze_aggregate(self, node: AggregateNode) -> str:
        """
        Valida um nó de agregação.

        Verifica que:
        - A coleção é "postings"
        - O WHERE (se presente) é um predicado válido
        - O SELECT (se presente) é um campo válido

        Args:
            node: AggregateNode a analisar.

        Returns:
            Tipo inferido da agregação.

        Raises:
            PolicySemanticError: se a coleção não for "postings".
        """
        if node.collection.name != "postings":
            raise PolicySemanticError(
                f"Agregações só são permitidas sobre a coleção 'postings', "
                f"encontrado '{node.collection.name}'"
            )

        if node.where is not None:
            self._analyze_node(node.where)

        if node.select is not None:
            self._analyze_node(node.select)

        # ANY/ALL return bool; SUM/COUNT/MIN/MAX return numeric
        if node.function in ("ANY", "ALL"):
            return "bool"
        return "numeric"

    def _analyze_comparison(self, node: ComparisonNode) -> str:
        """
        Valida uma comparação e verifica compatibilidade de tipos.

        Para o operador IN: left deve ser escalar, right deve ser coleção/ref.
        Para outros operadores: tipos devem ser compatíveis (ambos numéricos
        ou ambos strings, ou pelo menos um "unknown").

        Args:
            node: ComparisonNode a analisar.

        Returns:
            "bool" (comparações sempre produzem booleano).

        Raises:
            PolicySemanticError: se os tipos forem incompatíveis.
        """
        left_type = self._analyze_node(node.left)
        right_type = self._analyze_node(node.right)

        if node.operator == "IN":
            # IN operator: left is scalar, right is collection/ref — always valid
            return "bool"

        # For other operators, check type compatibility
        # Allow "unknown" on either side (runtime type checking)
        if left_type != "unknown" and right_type != "unknown":
            if left_type != right_type:
                raise PolicySemanticError(
                    f"Tipos incompatíveis na comparação '{node.operator}': "
                    f"lado esquerdo é '{left_type}', lado direito é '{right_type}'"
                )

        return "bool"


# ---------------------------------------------------------------------------
# DSLPrettyPrinter
# ---------------------------------------------------------------------------


class DSLPrettyPrinter:
    """
    Converte um RuleAST de volta para texto DSL válido.

    O texto produzido é semanticamente equivalente ao original:
    parsear a saída do pretty printer deve produzir um AST equivalente.

    Uso:
        printer = DSLPrettyPrinter()
        dsl_text = printer.print(ast)

    Requisito: 2.5, 2.6
    """

    def print(self, ast: RuleAST) -> str:
        """
        Converte um RuleAST para texto DSL.

        Args:
            ast: RuleAST a converter.

        Returns:
            Texto DSL válido representando o AST.
        """
        parts = []
        for rule in ast.rules:
            parts.append(self._print_rule(rule))
        return "\n\n".join(parts)

    def _print_rule(self, rule: PolicyRuleNode) -> str:
        """Converte uma PolicyRuleNode para texto DSL."""
        effect = rule.effect.value
        condition = self._print_node(rule.condition)
        message = rule.message.replace('"', '\\"')
        return (
            f"POLICY {rule.name} PRIORITY {rule.priority}\n"
            f"  WHEN {condition}\n"
            f"  THEN {effect} \"{message}\""
        )

    def _print_node(self, node: ASTNode) -> str:
        """
        Converte um nó do AST para texto DSL.

        Args:
            node: Nó a converter.

        Returns:
            Representação textual do nó.
        """
        if isinstance(node, LiteralNode):
            return self._print_literal(node)

        elif isinstance(node, FieldAccessNode):
            return ".".join(node.path)

        elif isinstance(node, RefAccessNode):
            return "ref." + ".".join(node.path)

        elif isinstance(node, CollectionRefNode):
            return node.name

        elif isinstance(node, PredicateNode):
            return self._print_node(node.condition)

        elif isinstance(node, AggregateNode):
            return self._print_aggregate(node)

        elif isinstance(node, ComparisonNode):
            left = self._print_node(node.left)
            right = self._print_node(node.right)
            return f"{left} {node.operator} {right}"

        elif isinstance(node, LogicalOpNode):
            left = self._print_node(node.left)
            right = self._print_node(node.right)
            # Wrap sub-expressions in parens if they are also logical ops
            # to preserve precedence unambiguously
            if isinstance(node.left, LogicalOpNode) and node.left.operator != node.operator:
                left = f"({left})"
            if isinstance(node.right, LogicalOpNode) and node.right.operator != node.operator:
                right = f"({right})"
            return f"{left}\n    {node.operator} {right}"

        elif isinstance(node, NotOpNode):
            operand = self._print_node(node.operand)
            return f"NOT {operand}"

        elif isinstance(node, PolicyRuleNode):
            return self._print_rule(node)

        return str(node)

    def _print_literal(self, node: LiteralNode) -> str:
        """Converte um LiteralNode para texto DSL."""
        if isinstance(node.value, bool):
            return "true" if node.value else "false"
        if isinstance(node.value, str):
            escaped = node.value.replace('"', '\\"')
            return f'"{escaped}"'
        return str(node.value)

    def _print_aggregate(self, node: AggregateNode) -> str:
        """Converte um AggregateNode para texto DSL."""
        parts = [node.function, "(", node.collection.name]

        if node.where is not None:
            # WHERE clause: print the predicate condition
            if isinstance(node.where, PredicateNode):
                where_str = self._print_node(node.where.condition)
            else:
                where_str = self._print_node(node.where)
            parts.append(f" WHERE {where_str}")

        if node.select is not None:
            select_str = self._print_node(node.select)
            parts.append(f" SELECT {select_str}")

        parts.append(")")
        return "".join(parts)


# ---------------------------------------------------------------------------
# DSLCompiler
# ---------------------------------------------------------------------------


class DSLCompiler:
    """
    Compilador da Policy Rule DSL.

    Orquestra o pipeline completo de compilação:
    1. Tokenização do texto fonte
    2. Parsing para RuleAST
    3. Análise semântica (SemanticAnalyzer)
    4. Análise de custo (PolicyCostAnalyzer)
    5. Geração do RuleBundle com artifact_hash

    Uso:
        compiler = DSLCompiler.create_default()
        bundle = compiler.compile(
            dsl_source="POLICY ...",
            policy_set_id="my_policies",
            metadata=CompilationMetadata(...),
            compatibility=BundleCompatibility(...),
        )

    Requisito: 2.1, 2.2, 2.3, 2.4
    """

    def __init__(
        self,
        semantic_analyzer: SemanticAnalyzer,
        cost_analyzer: PolicyCostAnalyzer,
    ) -> None:
        """
        Inicializa o compilador com os analisadores injetados.

        Args:
            semantic_analyzer: Analisador semântico a usar.
            cost_analyzer:     Analisador de custo a usar.
        """
        self._semantic_analyzer = semantic_analyzer
        self._cost_analyzer = cost_analyzer

    @classmethod
    def create_default(cls) -> "DSLCompiler":
        """
        Cria um DSLCompiler com configurações padrão.

        Usa SemanticAnalyzer e PolicyCostAnalyzer com limites padrão do design doc.

        Returns:
            DSLCompiler pronto para uso.
        """
        return cls(
            semantic_analyzer=SemanticAnalyzer(),
            cost_analyzer=PolicyCostAnalyzer(),
        )

    def compile(
        self,
        dsl_source: str,
        policy_set_id: str,
        metadata: CompilationMetadata,
        compatibility: BundleCompatibility,
    ) -> RuleBundle:
        """
        Compila texto DSL em um RuleBundle imutável.

        Pipeline:
        1. Tokeniza o texto fonte
        2. Parseia para RuleAST
        3. Executa análise semântica
        4. Executa análise de custo
        5. Gera artifact_hash (SHA-256 do conteúdo sem o campo hash)
        6. Retorna RuleBundle

        Args:
            dsl_source:    Texto fonte da DSL.
            policy_set_id: Identificador lógico do conjunto de policies.
            metadata:      Metadados de compilação (autor, descrição, etc.).
            compatibility: Declaração de compatibilidade com o runtime.

        Returns:
            RuleBundle compilado e pronto para armazenamento.

        Raises:
            PolicySyntaxError:        se o texto contiver erros de sintaxe.
            PolicySemanticError:      se o AST contiver violações semânticas.
            PolicyCostBudgetExceeded: se o bundle exceder os limites de custo.
        """
        # Step 1: Tokenize
        tokens = _tokenize(dsl_source)

        # Step 2: Parse
        parser = _Parser(tokens)
        ast = parser.parse_program()

        # Step 3: Semantic analysis
        self._semantic_analyzer.analyze(ast)

        # Step 4: Cost analysis
        self._cost_analyzer.analyze(ast, dsl_source=dsl_source)

        # Step 5: Generate artifact_hash
        # Build a preliminary bundle dict without the hash field, then hash it
        artifact_hash = _compute_artifact_hash(
            policy_set_id=policy_set_id,
            ast=ast,
            compatibility=compatibility,
            composition_mode=CompositionMode.DENY_OVERRIDES,
            metadata=metadata,
        )

        # Step 6: Build and return RuleBundle
        return RuleBundle(
            policy_set_id=policy_set_id,
            artifact_hash=artifact_hash,
            ast=ast,
            execution_plan={},
            compatibility=compatibility,
            composition_mode=CompositionMode.DENY_OVERRIDES,
            metadata=metadata,
        )

    def parse_only(self, dsl_source: str) -> RuleAST:
        """
        Parseia o texto DSL e retorna o RuleAST sem análise semântica ou de custo.

        Útil para ferramentas de diagnóstico e pretty printing.

        Args:
            dsl_source: Texto fonte da DSL.

        Returns:
            RuleAST parseado.

        Raises:
            PolicySyntaxError: se o texto contiver erros de sintaxe.
        """
        tokens = _tokenize(dsl_source)
        parser = _Parser(tokens)
        return parser.parse_program()


# ---------------------------------------------------------------------------
# Artifact hash computation
# ---------------------------------------------------------------------------


def _compute_artifact_hash(
    policy_set_id: str,
    ast: RuleAST,
    compatibility: BundleCompatibility,
    composition_mode: CompositionMode,
    metadata: CompilationMetadata,
) -> str:
    """
    Calcula o artifact_hash SHA-256 do conteúdo do bundle.

    O hash é calculado sobre o JSON serializado do bundle com o campo
    "artifact_hash" definido como string vazia "", conforme especificado
    no design doc (Requisito 3.3).

    A serialização usa sort_keys=True para garantir determinismo.

    Args:
        policy_set_id:    Identificador lógico do bundle.
        ast:              RuleAST do bundle.
        compatibility:    Compatibilidade declarada.
        composition_mode: Modo de composição.
        metadata:         Metadados de compilação.

    Returns:
        SHA-256 hexdigest do conteúdo serializado.
    """
    from validation_engine.domain.models import _ast_to_dict

    # Build the content dict WITHOUT artifact_hash field
    # The BundleLoader verifies integrity by removing this field before hashing,
    # so we must compute the hash the same way (field absent, not set to "").
    content = {
        "policy_set_id": policy_set_id,
        # artifact_hash intentionally omitted — matches BundleLoader._verify_integrity
        "ast": _ast_to_dict(ast),
        "execution_plan": {},
        "compatibility": {
            "dsl_version": compatibility.dsl_version,
            "context_schema_version": compatibility.context_schema_version,
            "snapshot_schema_version": compatibility.snapshot_schema_version,
            "evaluator_min_version": compatibility.evaluator_min_version,
        },
        "composition_mode": composition_mode.value,
        "metadata": {
            "author": metadata.author,
            "description": metadata.description,
            "compiled_at": metadata.compiled_at,
            "source_hash": metadata.source_hash,
        },
    }

    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
