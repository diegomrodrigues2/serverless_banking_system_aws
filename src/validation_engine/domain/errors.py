"""
Hierarquia de erros do domínio do Validation Engine.

Todos os erros herdam de ValidationEngineError, que por sua vez herda de DomainError
do bounded context do ledger. Isso garante que erros do motor de validação sejam
tratados de forma consistente pela camada de API do subledger.

Cada erro carrega:
- code: código estruturado para identificação programática (ex: POLICY_SYNTAX_ERROR)
- message: descrição legível por humanos, exposta ao cliente da API
- http_status: código HTTP correspondente para tradução na camada de API

Mapeamento de erros para HTTP status codes:
- 400 Bad Request:  erros de autoria e compilação (sintaxe, semântica, custo, bundle inválido)
- 422 Unprocessable: rejeição de policy de negócio (transação válida estruturalmente, mas rejeitada)
- 500 Internal:     falhas internas do motor (integridade, erro de avaliação)
- 503 Unavailable:  motor ou recursos indisponíveis (engine not ready, bundle/snapshot unavailable)

Requisitos cobertos: 17.1, 17.3, 17.4, 17.5
"""

from ledger.domain.errors import DomainError


class ValidationEngineError(DomainError):
    """
    Erro base do Validation Engine.

    Todos os erros específicos do motor de validação herdam desta classe.
    Herda de DomainError para compatibilidade com o tratamento de erros
    existente na camada de API do subledger.

    Subclasses devem definir DEFAULT_CODE e DEFAULT_HTTP_STATUS como
    atributos de classe para permitir instanciação sem argumentos obrigatórios.
    """

    # Subclasses devem sobrescrever estes atributos de classe
    DEFAULT_CODE: str = "VALIDATION_ENGINE_ERROR"
    DEFAULT_MESSAGE: str = "Erro interno do motor de validação"
    DEFAULT_HTTP_STATUS: int = 500

    def __init__(self, message: str | None = None) -> None:
        # Usa a mensagem fornecida ou cai de volta para o padrão da subclasse.
        # Isso permite instanciação simples (PolicySyntaxError()) ou com
        # mensagem customizada (PolicySyntaxError("Detalhe específico")).
        resolved_message = message if message is not None else self.DEFAULT_MESSAGE
        super().__init__(
            code=self.DEFAULT_CODE,
            message=resolved_message,
            http_status=self.DEFAULT_HTTP_STATUS,
        )


# ---------------------------------------------------------------------------
# Erros de autoria e compilação (400 Bad Request)
# ---------------------------------------------------------------------------


class PolicySyntaxError(ValidationEngineError):
    """
    Erro de sintaxe na DSL de policies.

    Levantado pelo DSLCompiler quando o texto da policy não pode ser
    parseado por violação das regras gramaticais da DSL.

    Exemplos:
    - keyword inválida
    - estrutura de bloco malformada
    - operador desconhecido

    Requisito: 17.1, 17.3
    """

    DEFAULT_CODE = "POLICY_SYNTAX_ERROR"
    DEFAULT_MESSAGE = "Erro de sintaxe na DSL de policy"
    DEFAULT_HTTP_STATUS = 400


class PolicySemanticError(ValidationEngineError):
    """
    Erro semântico na policy compilada.

    Levantado pelo SemanticAnalyzer quando a policy é sintaticamente válida
    mas viola regras semânticas: tipagem incorreta, campo inválido no contexto,
    referência a namespace proibido ou construção não-determinística.

    Exemplos:
    - acesso a metadata arbitrário (proibido — apenas policy_context é permitido)
    - tipo incompatível em comparação
    - referência a função inexistente

    Requisito: 17.1, 17.3
    """

    DEFAULT_CODE = "POLICY_SEMANTIC_ERROR"
    DEFAULT_MESSAGE = "Erro semântico na policy: tipo inválido, campo proibido ou referência inválida"
    DEFAULT_HTTP_STATUS = 400


class PolicyCostBudgetExceeded(ValidationEngineError):
    """
    Bundle acima do orçamento de custo estático.

    Levantado pelo PolicyCostAnalyzer quando o bundle excede os limites
    configurados para manter previsibilidade de latência no write path.

    Limites verificados:
    - número de rules por bundle (máx: 64)
    - profundidade máxima do AST (máx: 12)
    - agregações por rule (máx: 8)
    - tamanho do DSL fonte (máx: 64 KB)
    - campos em policy_context (máx: 32)
    - scans totais por avaliação (máx: 32)

    Requisito: 17.1, 17.3
    """

    DEFAULT_CODE = "POLICY_COST_BUDGET_EXCEEDED"
    DEFAULT_MESSAGE = "Bundle excede o orçamento de custo estático permitido"
    DEFAULT_HTTP_STATUS = 400


class InvalidPolicyBundle(ValidationEngineError):
    """
    Bundle inválido ou incompatível com o runtime atual.

    Levantado quando um bundle não pode ser carregado por:
    - estrutura inválida (campos obrigatórios ausentes)
    - incompatibilidade de versão com o evaluator atual
    - context_schema_version incompatível com o contexto canônico atual

    Diferente de PolicyBundleIntegrityFailure (que trata divergência de hash),
    este erro trata incompatibilidade estrutural ou de versão.

    Requisito: 17.1, 17.4
    """

    DEFAULT_CODE = "INVALID_POLICY_BUNDLE"
    DEFAULT_MESSAGE = "Bundle inválido ou incompatível com o runtime atual"
    DEFAULT_HTTP_STATUS = 400


# ---------------------------------------------------------------------------
# Erros de rejeição de negócio (422 Unprocessable)
# ---------------------------------------------------------------------------


class PolicyRejected(ValidationEngineError):
    """
    Transação rejeitada por uma rule de policy de negócio.

    Levantado pelo RuleEvaluator quando uma ou mais rules com efeito DENY
    casam com o contexto canônico da transação. A transação é estruturalmente
    válida (passou pelos validadores do ledger), mas viola uma regra de negócio
    configurada.

    O campo message deve indicar qual rule rejeitou e por quê.

    Requisito: 17.1, 17.5
    """

    DEFAULT_CODE = "POLICY_REJECTED"
    DEFAULT_MESSAGE = "Transação rejeitada por policy de negócio"
    DEFAULT_HTTP_STATUS = 422


# ---------------------------------------------------------------------------
# Erros de disponibilidade (503 Service Unavailable)
# ---------------------------------------------------------------------------


class PolicyEngineNotReady(ValidationEngineError):
    """
    Motor de validação sem policy válida carregada.

    Levantado pelo PolicyRuntimeRegistry quando não há ActivePolicySet
    válido para o escopo solicitado e o motor nunca teve uma inicialização
    bem-sucedida (sem Last Known Good disponível).

    Semântica fail-closed: sem policy válida, a transação é rejeitada.
    O cliente deve retentar após o motor estar pronto.

    Requisito: 17.1, 17.2
    """

    DEFAULT_CODE = "POLICY_ENGINE_NOT_READY"
    DEFAULT_MESSAGE = "Motor de validação sem policy válida carregada — tente novamente em instantes"
    DEFAULT_HTTP_STATUS = 503


class PolicyBundleUnavailable(ValidationEngineError):
    """
    Bundle de policy indisponível no storage.

    Levantado pelo BundleLoader quando o bundle referenciado no manifesto
    não pode ser carregado do S3 (objeto não encontrado, timeout, erro de rede).

    Diferente de PolicyBundleIntegrityFailure (que trata divergência de hash),
    este erro trata indisponibilidade do storage.

    Requisito: 17.1, 17.3
    """

    DEFAULT_CODE = "POLICY_BUNDLE_UNAVAILABLE"
    DEFAULT_MESSAGE = "Bundle de policy indisponível — storage inacessível ou objeto não encontrado"
    DEFAULT_HTTP_STATUS = 503


class PolicySnapshotUnavailable(ValidationEngineError):
    """
    Snapshot de referência indisponível no storage.

    Levantado pelo SnapshotLoader quando o snapshot referenciado no manifesto
    não pode ser carregado do S3 (objeto não encontrado, timeout, erro de rede).

    Requisito: 17.1, 17.3
    """

    DEFAULT_CODE = "POLICY_SNAPSHOT_UNAVAILABLE"
    DEFAULT_MESSAGE = "Snapshot de referência indisponível — storage inacessível ou objeto não encontrado"
    DEFAULT_HTTP_STATUS = 503


# ---------------------------------------------------------------------------
# Erros internos do motor (500 Internal Server Error)
# ---------------------------------------------------------------------------


class PolicyBundleIntegrityFailure(ValidationEngineError):
    """
    Falha de integridade do bundle ou snapshot.

    Levantado quando o hash calculado do artefato carregado diverge do
    hash registrado no manifesto. Indica possível corrupção ou adulteração
    do artefato em storage.

    O runtime deve alarmar e rejeitar o artefato corrompido.
    O Last Known Good pode ser usado se disponível.

    Requisito: 17.1, 17.4
    """

    DEFAULT_CODE = "POLICY_BUNDLE_INTEGRITY_FAILURE"
    DEFAULT_MESSAGE = "Falha de integridade: hash do artefato diverge do manifesto"
    DEFAULT_HTTP_STATUS = 500


class PolicyEvaluationError(ValidationEngineError):
    """
    Erro interno durante a avaliação de policies.

    Levantado pelo RuleEvaluator quando ocorre uma exceção inesperada
    durante a avaliação do AST. Indica bug no evaluator ou estado
    inconsistente do bundle.

    Diferente de PolicyRejected (que é rejeição semântica esperada),
    este erro indica falha técnica interna.

    Requisito: 17.1, 17.5
    """

    DEFAULT_CODE = "POLICY_EVALUATION_ERROR"
    DEFAULT_MESSAGE = "Erro interno durante avaliação de policy"
    DEFAULT_HTTP_STATUS = 500
