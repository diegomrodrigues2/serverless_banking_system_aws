"""
Modelos de domínio do Validation Engine.

Este módulo contém os value objects e agregados que representam os conceitos
centrais do motor de validação: ativação de policies, artefatos compilados,
resultados de avaliação e trilhas de auditoria.

Organização:
1. PolicyScope              — escopo de aplicação de uma policy
2. PolicyActivationManifest — manifesto atômico de ativação
3. BundleCompatibility      — declaração de compatibilidade do bundle
4. CompilationMetadata      — metadados de compilação do bundle
5. RuleBundle               — artefato compilado e imutável
6. ReferenceSnapshot        — snapshot imutável de dados auxiliares
7. ActivePolicySet          — conjunto materializado em memória
8. RuleMatchResult          — resultado de avaliação de uma rule individual
9. EvaluationDecision       — decisão final da avaliação
10. EvaluationMetrics       — métricas de performance da avaliação
11. EvaluationResult        — resultado completo da avaliação
12. DecisionSummary         — resumo mínimo persistido atomicamente com o JournalEntry
13. DecisionTrail           — trilha detalhada para auditoria e analytics

Todos os modelos são frozen dataclasses: imutáveis, comparáveis por valor
e seguros para uso em contextos concorrentes.

Requisitos cobertos: 4.1, 4.2, 5.1, 5.2, 6.1, 6.2, 11.1, 12.1, 12.2,
                     13.1, 13.2, 24.1, 24.2
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from validation_engine.domain.policy_ast import (
    CompositionMode,
    FinalVerdict,
    PolicyEffect,
    RuleAST,
)

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# 1. PolicyScope — escopo de aplicação de uma policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyScope:
    """
    Escopo de aplicação de uma policy.

    O escopo determina qual conjunto de rules se aplica a uma transação.
    Múltiplos tenants podem compartilhar o mesmo policy_scope_id se tiverem
    as mesmas regras de negócio.

    Campos obrigatórios (Requisito 5.2):
    - tenant_id:      identificador do tenant
    - operation_type: tipo de operação (ex: "TRANSFER", "PAYMENT", "REVERSAL")

    Campos opcionais para granularidade adicional:
    - product_code:   código do produto (ex: "PIX", "TED", "BOLETO")
    - channel:        canal de origem (ex: "MOBILE", "API", "BRANCH")
    - environment:    ambiente de execução (padrão: "prod")

    O scope_id é derivado deterministicamente dos campos para uso como
    chave de cache no PolicyRuntimeRegistry.

    Requisito: 5.1, 5.2, 5.3, 5.4, 5.5
    """

    tenant_id: str
    operation_type: str
    product_code: str | None = None
    channel: str | None = None
    # Ambiente de execução — padrão "prod" para evitar erros de configuração
    environment: str = "prod"

    @property
    def scope_id(self) -> str:
        """
        Identificador derivado deterministicamente do escopo.

        Formato: tenant_id:operation_type:product_code:channel:environment
        Wildcards (*) substituem campos opcionais não informados.

        Exemplo:
            tenantA:TRANSFER:PIX:MOBILE:prod
            tenantB:PAYMENT:*:*:prod
        """
        product = self.product_code or "*"
        channel = self.channel or "*"
        return f"{self.tenant_id}:{self.operation_type}:{product}:{channel}:{self.environment}"


# ---------------------------------------------------------------------------
# 2. PolicyActivationManifest — manifesto atômico de ativação
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyActivationManifest:
    """
    Manifesto atômico que define a combinação ativa de artefatos para um escopo.

    A ativação é atômica: bundle, snapshot e versões do runtime andam juntos.
    Nunca é válido usar artifact_hash de um manifesto com snapshot_version
    de outro manifesto — eles formam uma unidade indivisível.

    Campos obrigatórios (Requisito 4.2):
    - activation_id:          identificador único desta ativação
    - policy_scope_id:        escopo ao qual este manifesto se aplica
    - artifact_hash:          SHA-256 do RuleBundle ativo
    - snapshot_version:       versão do ReferenceSnapshot ativo
    - context_schema_version: versão do schema do CanonicalValidationContext
    - evaluator_version:      versão mínima do RuleEvaluator compatível
    - activated_at:           timestamp ISO 8601 da ativação
    - activated_by:           identidade que realizou a ativação (para auditoria)

    O histórico de manifestos é preservado no AppConfig para rollback e auditoria.

    Requisito: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6
    """

    activation_id: str
    policy_scope_id: str
    # SHA-256 do conteúdo serializado do RuleBundle (excluindo o próprio campo de hash)
    artifact_hash: str
    # Versão do ReferenceSnapshot — chave de lookup no S3
    snapshot_version: str
    # Versão do schema do CanonicalValidationContext — validada antes da avaliação
    context_schema_version: str
    # Versão mínima do RuleEvaluator compatível com este bundle
    evaluator_version: str
    # Timestamp ISO 8601 de quando esta ativação foi publicada
    activated_at: str
    # Identidade que publicou esta ativação (usuário, sistema, pipeline)
    activated_by: str


# ---------------------------------------------------------------------------
# 3. BundleCompatibility — declaração de compatibilidade do bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BundleCompatibility:
    """
    Declaração de compatibilidade de um RuleBundle com o runtime.

    Usada pelo PolicyRuntimeRegistry para validar se um bundle pode ser
    carregado com o evaluator e contexto atuais antes de ativá-lo.

    Campos (Requisito 24.2):
    - dsl_version:             versão da gramática DSL usada na compilação
    - context_schema_version:  versão do CanonicalValidationContext esperada
    - snapshot_schema_version: versão do schema do ReferenceSnapshot esperada
    - evaluator_min_version:   versão mínima do RuleEvaluator necessária

    Requisito: 24.2, 24.3
    """

    dsl_version: str
    context_schema_version: str
    snapshot_schema_version: str
    evaluator_min_version: str


# ---------------------------------------------------------------------------
# 4. CompilationMetadata — metadados de compilação do bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompilationMetadata:
    """
    Metadados de compilação de um RuleBundle.

    Preservados para auditoria, rastreabilidade e rollback.
    Não participam da avaliação — são apenas informativos.

    Campos (Requisito 24.1):
    - author:      identidade que compilou o bundle
    - description: descrição legível do conjunto de policies
    - compiled_at: timestamp ISO 8601 da compilação
    - source_hash: SHA-256 do texto fonte da DSL (para rastreabilidade)

    Requisito: 24.1
    """

    author: str
    description: str
    compiled_at: str
    # SHA-256 do texto fonte da DSL — permite rastrear qual fonte gerou este bundle
    source_hash: str


# ---------------------------------------------------------------------------
# 5. RuleBundle — artefato compilado e imutável
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleBundle:
    """
    Artefato compilado e imutável de um conjunto de policies.

    O RuleBundle é o resultado da compilação bem-sucedida de uma DSL.
    Uma vez armazenado no S3 com Object Lock (WORM), não pode ser alterado.

    Campos:
    - policy_set_id:    identificador lógico do conjunto de policies
    - artifact_hash:    SHA-256 do conteúdo serializado (excluindo este campo)
    - ast:              AST tipado das rules (para audit/debug e replay)
    - execution_plan:   plano de execução otimizado para o evaluator
    - compatibility:    declaração de compatibilidade com o runtime
    - composition_mode: estratégia de composição (DENY_OVERRIDES)
    - metadata:         metadados de compilação para auditoria

    Serialização:
    O bundle é serializado em JSON UTF-8 determinístico para armazenamento.
    O artifact_hash é calculado sobre o JSON serializado excluindo o próprio
    campo "artifact_hash" para evitar dependência circular.

    Requisito: 3.1, 3.3, 3.5, 24.1, 24.2
    """

    policy_set_id: str
    # SHA-256 do conteúdo serializado — calculado pelo DSLCompiler
    artifact_hash: str
    # AST tipado — preservado para audit, debug e replay
    ast: RuleAST
    # Plano de execução otimizado — estrutura interna do evaluator
    execution_plan: dict
    compatibility: BundleCompatibility
    composition_mode: CompositionMode
    metadata: CompilationMetadata

    def to_json(self) -> str:
        """
        Serializa o bundle para JSON UTF-8 determinístico.

        O JSON produzido é determinístico (chaves ordenadas) para garantir
        que o artifact_hash seja reproduzível independentemente da ordem
        de inserção dos campos.

        O campo artifact_hash é incluído na serialização para permitir
        verificação de integridade após carregamento do S3.
        """
        return json.dumps(self._to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, data: str) -> "RuleBundle":
        """
        Desserializa um bundle a partir de JSON.

        Usado pelo BundleLoader após carregamento do S3.
        A integridade deve ser verificada pelo chamador comparando
        o artifact_hash do manifesto com o hash calculado do conteúdo.
        """
        raw = json.loads(data)
        return cls._from_dict(raw)

    def _to_dict(self) -> dict:
        """Converte o bundle para dicionário serializável."""
        return {
            "policy_set_id": self.policy_set_id,
            "artifact_hash": self.artifact_hash,
            "ast": _ast_to_dict(self.ast),
            "execution_plan": self.execution_plan,
            "compatibility": {
                "dsl_version": self.compatibility.dsl_version,
                "context_schema_version": self.compatibility.context_schema_version,
                "snapshot_schema_version": self.compatibility.snapshot_schema_version,
                "evaluator_min_version": self.compatibility.evaluator_min_version,
            },
            "composition_mode": self.composition_mode.value,
            "metadata": {
                "author": self.metadata.author,
                "description": self.metadata.description,
                "compiled_at": self.metadata.compiled_at,
                "source_hash": self.metadata.source_hash,
            },
        }

    @classmethod
    def _from_dict(cls, raw: dict) -> "RuleBundle":
        """Reconstrói o bundle a partir de um dicionário deserializado."""
        compat_raw = raw["compatibility"]
        meta_raw = raw["metadata"]
        return cls(
            policy_set_id=raw["policy_set_id"],
            artifact_hash=raw["artifact_hash"],
            ast=_ast_from_dict(raw["ast"]),
            execution_plan=raw["execution_plan"],
            compatibility=BundleCompatibility(
                dsl_version=compat_raw["dsl_version"],
                context_schema_version=compat_raw["context_schema_version"],
                snapshot_schema_version=compat_raw["snapshot_schema_version"],
                evaluator_min_version=compat_raw["evaluator_min_version"],
            ),
            composition_mode=CompositionMode(raw["composition_mode"]),
            metadata=CompilationMetadata(
                author=meta_raw["author"],
                description=meta_raw["description"],
                compiled_at=meta_raw["compiled_at"],
                source_hash=meta_raw["source_hash"],
            ),
        )


# ---------------------------------------------------------------------------
# 6. ReferenceSnapshot — snapshot imutável de dados auxiliares
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceSnapshot:
    """
    Snapshot imutável e versionado com dados auxiliares para avaliação.

    O snapshot contém dados de referência que as policies precisam consultar
    durante a avaliação (ex: limites diários, listas de bloqueio, parâmetros
    de compliance). Esses dados são materializados em memória como parte do
    ActivePolicySet para garantir que a avaliação seja livre de I/O.

    Campos:
    - snapshot_version:       identificador único desta versão do snapshot
    - snapshot_schema_version: versão do schema dos dados (para compatibilidade)
    - created_at:             timestamp ISO 8601 de criação
    - data:                   dados de referência indexados por chave

    Tipos de dados suportados no snapshot:
    - int, str, bool:         valores escalares
    - tuple[str, ...]:        listas de strings (ex: blocked_accounts)
    - tuple[int, ...]:        listas de inteiros

    Acesso via DSL:
        ref.daily_limit_minor       → snapshot.lookup(("daily_limit_minor",))
        ref.blocked_accounts        → snapshot.lookup(("blocked_accounts",))

    Requisito: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6
    """

    snapshot_version: str
    snapshot_schema_version: str
    created_at: str
    # Dados de referência — tipos restritos para manter a DSL determinística
    data: Mapping[str, int | str | bool | tuple[str, ...] | tuple[int, ...]]

    def lookup(self, path: tuple[str, ...]) -> object:
        """
        Acessa um valor no snapshot pelo path.

        O path é uma tupla de strings representando a navegação hierárquica.
        Atualmente suporta apenas paths de profundidade 1 (chave direta).

        Retorna None se o path não existir no snapshot.

        Exemplo:
            snapshot.lookup(("daily_limit_minor",))  → 100000
            snapshot.lookup(("blocked_accounts",))   → ("acc_123", "acc_456")
        """
        if len(path) == 1:
            return self.data.get(path[0])
        # Suporte futuro para paths aninhados pode ser adicionado aqui
        # sem quebrar a interface existente
        return None


# ---------------------------------------------------------------------------
# 7. ActivePolicySet — conjunto materializado em memória
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivePolicySet:
    """
    Conjunto de policy materializado em memória para uso no hot path.

    O ActivePolicySet é o resultado do carregamento bem-sucedido de um
    manifesto: bundle e snapshot são carregados do S3, verificados por
    integridade e compatibilidade, e então mantidos em memória.

    Em steady state, o request path usa apenas este objeto — sem I/O.
    A troca de ActivePolicySet ocorre por swap atômico no PolicyRuntimeRegistry.

    Campos:
    - manifest:           manifesto que originou este conjunto
    - bundle:             RuleBundle carregado e verificado
    - snapshot:           ReferenceSnapshot carregado e verificado
    - loaded_at:          timestamp ISO 8601 de quando foi carregado
    - integrity_verified: True se o artifact_hash foi verificado com sucesso

    Invariante de segurança:
    integrity_verified deve ser True antes de qualquer avaliação.
    O PolicyRuntimeRegistry é responsável por garantir esta invariante.

    Requisito: 6.1, 6.2, 6.3, 6.4, 6.5
    """

    manifest: PolicyActivationManifest
    bundle: RuleBundle
    snapshot: ReferenceSnapshot
    # Timestamp de quando este conjunto foi carregado em memória
    loaded_at: str
    # Evidência de que a integridade foi verificada antes da ativação
    integrity_verified: bool


# ---------------------------------------------------------------------------
# 8. RuleMatchResult — resultado de avaliação de uma rule individual
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleMatchResult:
    """
    Resultado da avaliação de uma rule individual.

    Registra se a rule casou com o contexto e qual foi o efeito.
    Usado para compor o EvaluationDecision e para auditoria detalhada
    no DecisionTrail.

    Campos:
    - rule_name: nome da rule (identificador único no bundle)
    - effect:    efeito declarado da rule (ALLOW ou DENY)
    - matched:   True se a condição da rule foi satisfeita
    - priority:  prioridade da rule (para ordenação na auditoria)
    - message:   mensagem descritiva da rule

    Requisito: 10.6
    """

    rule_name: str
    effect: PolicyEffect
    matched: bool
    priority: int
    message: str


# ---------------------------------------------------------------------------
# 9. EvaluationDecision — decisão final da avaliação
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationDecision:
    """
    Decisão final da avaliação de um bundle de policies.

    Contém o veredito final e os detalhes das rules que casaram,
    incluindo qual rule DENY determinou a rejeição (se aplicável).

    Campos:
    - final_verdict:     APPROVED ou REJECTED
    - matched_deny_rule: nome da rule DENY que determinou a rejeição, ou None
    - rules:             resultados de todas as rules avaliadas

    Semântica DENY_OVERRIDES (Requisito 10.3, 10.4):
    - Se qualquer rule DENY casar → final_verdict = REJECTED
    - Se nenhuma rule DENY casar  → final_verdict = APPROVED
    - matched_deny_rule registra a primeira rule DENY que casou

    Requisito: 10.3, 10.4, 10.5, 10.6
    """

    final_verdict: FinalVerdict
    # Nome da rule DENY que determinou a rejeição — None se APPROVED
    matched_deny_rule: str | None
    # Resultados de todas as rules avaliadas (para auditoria completa)
    rules: tuple[RuleMatchResult, ...]


# ---------------------------------------------------------------------------
# 10. EvaluationMetrics — métricas de performance da avaliação
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationMetrics:
    """
    Métricas de performance coletadas durante a avaliação.

    Campos efêmeros que NÃO participam da igualdade semântica da decisão.
    Usados para observabilidade e validação de budgets de latência.

    Campos:
    - evaluation_latency_ms: tempo total de avaliação em milissegundos
    - evaluated_rules:       número de rules avaliadas

    Requisito: 9.4, 19.1, 19.4
    """

    # Latência total da avaliação em milissegundos
    evaluation_latency_ms: float
    # Número de rules avaliadas nesta execução
    evaluated_rules: int


# ---------------------------------------------------------------------------
# 11. EvaluationResult — resultado completo da avaliação
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationResult:
    """
    Resultado completo retornado pelo RuleEvaluator.

    Combina a decisão semântica (EvaluationDecision) com as métricas
    de performance (EvaluationMetrics). A separação é intencional:
    métricas não participam da igualdade semântica da decisão.

    Campos:
    - decision: decisão final com veredito e detalhes das rules
    - metrics:  métricas de performance (latência, rules avaliadas)

    Requisito: 9.3, 10.6
    """

    decision: EvaluationDecision
    metrics: EvaluationMetrics


# ---------------------------------------------------------------------------
# 12. DecisionSummary — resumo mínimo persistido atomicamente
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionSummary:
    """
    Resumo mínimo da decisão de policy persistido atomicamente com o JournalEntry.

    O DecisionSummary é o contrato de persistência do Validation Engine com o ledger.
    Ele é gravado atomicamente junto com o JournalEntry pelo LedgerEngine,
    garantindo rastreabilidade mesmo se o DecisionTrail detalhado falhar.

    Campos obrigatórios (Requisito 12.2):
    - final_verdict:         APPROVED ou REJECTED
    - policy_scope_id:       escopo da policy aplicada
    - activation_id:         identificador da ativação do manifesto
    - artifact_hash:         SHA-256 do bundle usado na avaliação
    - snapshot_version:      versão do snapshot usado na avaliação
    - evaluator_version:     versão do evaluator usado
    - input_hash:            hash do contexto canônico (para verificação de integridade)
    - matched_deny_rule:     rule DENY que rejeitou, ou None se aprovado
    - evaluation_latency_ms: latência da avaliação em milissegundos

    Invariante de persistência:
    O Validation Engine NÃO escreve diretamente no banco de dados.
    O DecisionSummary é retornado ao LedgerEngine via ValidationResult.artifacts,
    e o LedgerEngine é responsável pela persistência atômica.

    Requisito: 12.1, 12.2, 12.3, 12.4, 12.5
    """

    final_verdict: FinalVerdict
    policy_scope_id: str
    activation_id: str
    artifact_hash: str
    snapshot_version: str
    evaluator_version: str
    # Hash do CanonicalValidationContext — para verificação de integridade no replay
    # NÃO é suficiente para reconstruir o input; o replay usa o JournalEntry completo
    input_hash: str
    # Nome da rule DENY que determinou a rejeição — None se APPROVED
    matched_deny_rule: str | None
    evaluation_latency_ms: float

    def to_metadata(self) -> dict:
        """
        Converte o summary para o formato de metadata do JournalEntry.

        O resultado é armazenado no campo metadata do JournalEntry sob
        a chave "policy_validation". Isso mantém o summary acessível
        para auditoria e replay sem poluir o modelo principal do ledger.

        Exemplo de output:
        {
            "policy_validation": {
                "final_verdict": "APPROVED",
                "policy_scope_id": "tenantA:TRANSFER:PIX:*:prod",
                ...
            }
        }
        """
        return {
            "policy_validation": {
                "final_verdict": self.final_verdict.value,
                "policy_scope_id": self.policy_scope_id,
                "activation_id": self.activation_id,
                "artifact_hash": self.artifact_hash,
                "snapshot_version": self.snapshot_version,
                "evaluator_version": self.evaluator_version,
                "input_hash": self.input_hash,
                "matched_deny_rule": self.matched_deny_rule,
                "evaluation_latency_ms": self.evaluation_latency_ms,
            }
        }


# ---------------------------------------------------------------------------
# 13. DecisionTrail — trilha detalhada para auditoria e analytics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionTrail:
    """
    Trilha detalhada da avaliação para auditoria e analytics.

    O DecisionTrail é emitido de forma assíncrona e best-effort pelo
    DecisionTrailEmitter para o pipeline de analytics (Firehose → S3 → Athena).
    Falha na emissão NÃO invalida a transação aprovada.

    Diferença em relação ao DecisionSummary:
    - DecisionSummary: mínimo, persistido atomicamente, garantia de corretude
    - DecisionTrail:   expandido, assíncrono, best-effort, para analytics

    Campos adicionais em relação ao DecisionSummary:
    - external_id:  identificador externo da transação (não entry_id, pois a
                    validação ocorre antes da criação do aggregate)
    - tenant_id:    identificador do tenant
    - rules:        lista completa de todas as rules avaliadas
    - error_code:   código de erro se a avaliação falhou internamente
    - timestamp:    timestamp ISO 8601 da avaliação

    Nota sobre external_id vs entry_id:
    O trail usa external_id porque a validação ocorre antes da criação
    do JournalEntry. O entry_id ainda não existe neste momento.

    Requisito: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6
    """

    # Identificador externo da transação (fornecido pelo chamador da API)
    external_id: str
    tenant_id: str
    policy_scope_id: str
    activation_id: str
    artifact_hash: str
    snapshot_version: str
    evaluator_version: str
    input_hash: str
    final_verdict: FinalVerdict
    # Nome da rule DENY que determinou a rejeição — None se APPROVED
    matched_deny_rule: str | None
    # Resultados de todas as rules avaliadas — expandido para analytics
    rules: tuple[RuleMatchResult, ...]
    evaluation_latency_ms: float
    # Código de erro se a avaliação falhou internamente — None em caso de sucesso
    error_code: str | None
    # Timestamp ISO 8601 da avaliação
    timestamp: str

    def to_firehose_payload(self) -> dict:
        """
        Serializa o trail para o formato de payload do Firehose.

        O payload é enviado ao Firehose para ingestão no pipeline de analytics.
        O formato é JSON com campos planos para compatibilidade com Parquet/Athena.

        Particionamento no S3 (Requisito 21.4):
        O Firehose particiona por year/month/day/tenant_id/policy_scope_id
        usando os campos correspondentes do payload.
        """
        return {
            "external_id": self.external_id,
            "tenant_id": self.tenant_id,
            "policy_scope_id": self.policy_scope_id,
            "activation_id": self.activation_id,
            "artifact_hash": self.artifact_hash,
            "snapshot_version": self.snapshot_version,
            "evaluator_version": self.evaluator_version,
            "input_hash": self.input_hash,
            "final_verdict": self.final_verdict.value,
            "matched_deny_rule": self.matched_deny_rule,
            "rules": [
                {
                    "rule_name": r.rule_name,
                    "effect": r.effect.value,
                    "matched": r.matched,
                    "priority": r.priority,
                    "message": r.message,
                }
                for r in self.rules
            ],
            "evaluation_latency_ms": self.evaluation_latency_ms,
            "error_code": self.error_code,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Helpers de serialização do AST (usados por RuleBundle)
# ---------------------------------------------------------------------------


def _ast_to_dict(ast: RuleAST) -> dict:
    """
    Serializa um RuleAST para dicionário.

    Usado internamente por RuleBundle.to_json() para serialização completa.
    A serialização é determinística (chaves ordenadas) para garantir
    reprodutibilidade do artifact_hash.
    """
    from validation_engine.domain.policy_ast import (
        AggregateNode,
        CollectionRefNode,
        ComparisonNode,
        FieldAccessNode,
        LiteralNode,
        LogicalOpNode,
        NotOpNode,
        PolicyRuleNode,
        PredicateNode,
        RefAccessNode,
    )

    def node_to_dict(node: object) -> dict:
        """Converte recursivamente um nó do AST para dicionário."""
        if isinstance(node, LiteralNode):
            return {"type": "LiteralNode", "value": node.value}
        elif isinstance(node, FieldAccessNode):
            return {"type": "FieldAccessNode", "path": list(node.path)}
        elif isinstance(node, RefAccessNode):
            return {"type": "RefAccessNode", "path": list(node.path)}
        elif isinstance(node, CollectionRefNode):
            return {"type": "CollectionRefNode", "name": node.name}
        elif isinstance(node, PredicateNode):
            return {
                "type": "PredicateNode",
                "binding": node.binding,
                "condition": node_to_dict(node.condition),
            }
        elif isinstance(node, AggregateNode):
            return {
                "type": "AggregateNode",
                "function": node.function,
                "collection": node_to_dict(node.collection),
                "where": node_to_dict(node.where) if node.where else None,
                "select": node_to_dict(node.select) if node.select else None,
            }
        elif isinstance(node, ComparisonNode):
            return {
                "type": "ComparisonNode",
                "left": node_to_dict(node.left),
                "operator": node.operator,
                "right": node_to_dict(node.right),
            }
        elif isinstance(node, LogicalOpNode):
            return {
                "type": "LogicalOpNode",
                "operator": node.operator,
                "left": node_to_dict(node.left),
                "right": node_to_dict(node.right),
            }
        elif isinstance(node, NotOpNode):
            return {"type": "NotOpNode", "operand": node_to_dict(node.operand)}
        elif isinstance(node, PolicyRuleNode):
            return {
                "type": "PolicyRuleNode",
                "name": node.name,
                "priority": node.priority,
                "condition": node_to_dict(node.condition),
                "effect": node.effect.value,
                "message": node.message,
            }
        else:
            raise ValueError(f"Nó AST desconhecido para serialização: {type(node)}")

    return {
        "rules": [node_to_dict(r) for r in ast.rules],
        "composition_mode": ast.composition_mode.value,
    }


def _ast_from_dict(raw: dict) -> RuleAST:
    """
    Desserializa um RuleAST a partir de dicionário.

    Usado internamente por RuleBundle.from_json() para reconstrução completa.
    """
    from validation_engine.domain.policy_ast import (
        AggregateNode,
        CollectionRefNode,
        ComparisonNode,
        FieldAccessNode,
        LiteralNode,
        LogicalOpNode,
        NotOpNode,
        PolicyRuleNode,
        PredicateNode,
        RefAccessNode,
    )

    def dict_to_node(d: dict) -> object:
        """Reconstrói recursivamente um nó do AST a partir de dicionário."""
        node_type = d["type"]
        if node_type == "LiteralNode":
            return LiteralNode(value=d["value"])
        elif node_type == "FieldAccessNode":
            return FieldAccessNode(path=tuple(d["path"]))
        elif node_type == "RefAccessNode":
            return RefAccessNode(path=tuple(d["path"]))
        elif node_type == "CollectionRefNode":
            return CollectionRefNode(name=d["name"])
        elif node_type == "PredicateNode":
            return PredicateNode(
                binding=d["binding"],
                condition=dict_to_node(d["condition"]),
            )
        elif node_type == "AggregateNode":
            return AggregateNode(
                function=d["function"],
                collection=dict_to_node(d["collection"]),
                where=dict_to_node(d["where"]) if d.get("where") else None,
                select=dict_to_node(d["select"]) if d.get("select") else None,
            )
        elif node_type == "ComparisonNode":
            return ComparisonNode(
                left=dict_to_node(d["left"]),
                operator=d["operator"],
                right=dict_to_node(d["right"]),
            )
        elif node_type == "LogicalOpNode":
            return LogicalOpNode(
                operator=d["operator"],
                left=dict_to_node(d["left"]),
                right=dict_to_node(d["right"]),
            )
        elif node_type == "NotOpNode":
            return NotOpNode(operand=dict_to_node(d["operand"]))
        elif node_type == "PolicyRuleNode":
            return PolicyRuleNode(
                name=d["name"],
                priority=d["priority"],
                condition=dict_to_node(d["condition"]),
                effect=PolicyEffect(d["effect"]),
                message=d["message"],
            )
        else:
            raise ValueError(f"Tipo de nó AST desconhecido na desserialização: {node_type}")

    rules = tuple(dict_to_node(r) for r in raw["rules"])
    return RuleAST(
        rules=rules,
        composition_mode=CompositionMode(raw["composition_mode"]),
    )
