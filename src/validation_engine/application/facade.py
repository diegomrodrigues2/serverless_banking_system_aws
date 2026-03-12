"""
PolicyValidationFacade — ponto de entrada do Data Plane do Validation Engine.

Responsabilidade:
    Orquestrar o pipeline completo de validação de policy para uma transação:
    1. Construir o contexto canônico a partir do comando.
    2. Resolver o escopo de policy a partir do contexto.
    3. Obter o ActivePolicySet em memória (sem I/O no steady state).
    4. Avaliar as rules de forma pura e determinística.
    5. Construir o DecisionSummary (persistência atômica com o JournalEntry).
    6. Construir o DecisionTrail (auditoria expandida).
    7. Emitir o DecisionTrail de forma best-effort (falha não invalida transação).
    8. Retornar ValidationResult com artefatos explícitos.

Invariantes de design (Requisito 7.5):
    - O comando original NÃO é mutado em nenhum ponto do pipeline.
    - Falha de emissão do DecisionTrail NÃO propaga exceção ao chamador.
    - Sem ActivePolicySet válido → PolicyEngineNotReady (fail-closed).
    - Rejeição por policy → PolicyRejected com código POLICY_REJECTED.

Integração com o ledger (Requisito 7.1, 7.4):
    A facade implementa o protocolo ValidationStrategy do ledger.
    É injetada na ValidationChain após os validadores estruturais.
    O DecisionSummary é retornado via ValidationArtifacts para persistência
    atômica pelo LedgerEngine — a facade NÃO escreve no banco de dados.

Requisitos cobertos: 7.1, 7.2, 7.3, 7.5, 12.1, 12.3, 13.1, 13.3, 13.4, 17.1
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from validation_engine.domain.errors import PolicyEngineNotReady, PolicyRejected
from validation_engine.domain.evaluator import EVALUATOR_VERSION, RuleEvaluator
from validation_engine.domain.models import (
    DecisionSummary,
    DecisionTrail,
    PolicyScope,
)
from validation_engine.domain.policy_ast import FinalVerdict

if TYPE_CHECKING:
    from validation_engine.application.context_builder import (
        CanonicalValidationContextBuilder,
    )
    from validation_engine.application.runtime_registry import PolicyRuntimeRegistry
    from validation_engine.domain.context import CanonicalValidationContext
    from validation_engine.domain.models import ActivePolicySet, EvaluationResult
    from validation_engine.infrastructure.decision_trail_emitter import (
        DecisionTrailEmitter,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ValidationArtifacts — artefatos retornados ao LedgerEngine
# ---------------------------------------------------------------------------


class ValidationArtifacts:
    """
    DEPRECATED — Use ledger.domain.validators.ValidationArtifacts instead.

    This class is kept temporarily for backward compatibility during the
    transition. New code should import ValidationArtifacts from the ledger
    domain validators module.

    Requisito: 12.3
    """

    def __init__(self, decision_summary: DecisionSummary | None = None) -> None:
        """
        Inicializa os artefatos com o DecisionSummary opcional.

        Args:
            decision_summary: Resumo mínimo da decisão de policy.
                              None se a validação não produziu summary
                              (ex: motor não pronto, erro interno).
        """
        self.decision_summary = decision_summary


# ---------------------------------------------------------------------------
# PolicyValidationFacade
# ---------------------------------------------------------------------------


class PolicyValidationFacade:
    """
    Fachada do Data Plane do Validation Engine.

    Orquestra o pipeline completo de validação de policy e implementa
    o protocolo ValidationStrategy do ledger para integração com a
    ValidationChain.

    O pipeline é executado de forma síncrona no hot path do write path.
    Em steady state, o único I/O é a emissão best-effort do DecisionTrail
    (que ocorre após a decisão e não bloqueia o retorno ao chamador).

    Uso:
        facade = PolicyValidationFacade(
            context_builder=DefaultCanonicalValidationContextBuilder(),
            runtime_registry=registry,
            evaluator=RuleEvaluator(),
            trail_emitter=FirehoseDecisionTrailEmitter(...),
        )

        # Integração com ValidationChain do ledger:
        chain = ValidationChain([
            ZeroSumValidator(),
            MinorUnitsValidator(),
            TransactionLimitValidator(),
            TenantIsolationValidator(),
            facade,  # após validadores estruturais
        ])

    Requisito: 7.1, 7.2, 7.3, 7.5, 12.1, 12.3, 13.1, 13.3, 13.4, 17.1
    """

    def __init__(
        self,
        context_builder: "CanonicalValidationContextBuilder",
        runtime_registry: "PolicyRuntimeRegistry",
        evaluator: RuleEvaluator,
        trail_emitter: "DecisionTrailEmitter",
    ) -> None:
        """
        Inicializa a facade com seus colaboradores.

        Todos os colaboradores são injetados para facilitar testes e
        permitir substituição de implementações (ex: emitter no-op em testes).

        Args:
            context_builder:  Constrói o CanonicalValidationContext a partir do comando.
            runtime_registry: Mantém o ActivePolicySet em memória por escopo.
            evaluator:        Avalia as rules de forma pura e determinística.
            trail_emitter:    Emite o DecisionTrail de forma best-effort.
        """
        self._context_builder = context_builder
        self._runtime_registry = runtime_registry
        self._evaluator = evaluator
        self._trail_emitter = trail_emitter

    def validate(self, command: object) -> "ValidationResult":
        """
        Executa o pipeline completo de validação de policy para o comando.

        Pipeline:
        1. Construir CanonicalValidationContext (sem I/O).
        2. Resolver PolicyScope a partir do contexto.
        3. Obter ActivePolicySet em memória (sem I/O em steady state).
        4. Avaliar rules (função pura, sem I/O).
        5. Construir DecisionSummary e DecisionTrail.
        6. Emitir DecisionTrail best-effort (falha não propaga).
        7. Retornar ValidationResult com artefatos.

        O comando NÃO é mutado em nenhum ponto do pipeline.

        Args:
            command: CreateJournalEntryCommand do ledger.

        Returns:
            ValidationResult com DecisionSummary nos artefatos se aprovado.

        Raises:
            PolicyEngineNotReady: se não há ActivePolicySet válido (fail-closed).
            PolicyRejected:       se uma rule DENY casou com o contexto.
            PolicyEvaluationError: se ocorreu erro interno durante avaliação.
        """
        # Passo 1: Construir contexto canônico — sem I/O, sem mutação do comando.
        context = self._context_builder.build(command)

        # Passo 2: Resolver escopo de policy a partir do contexto.
        scope = self._resolve_scope(context)

        # Passo 3: Obter ActivePolicySet em memória.
        # Em steady state: leitura de dicionário sem I/O.
        # Em cold start: pode disparar bootstrap (com I/O).
        # Levanta PolicyEngineNotReady se não há policy válida (fail-closed).
        active_policy_set = self._runtime_registry.get_active_policy_set(scope.scope_id)

        # Passo 4: Avaliar rules — função pura, sem I/O.
        evaluation_result = self._evaluator.evaluate(context, active_policy_set)

        # Passo 5: Calcular input_hash para rastreabilidade e replay.
        input_hash = self._compute_input_hash(context)

        # Passo 6: Construir DecisionSummary (persistência atômica com JournalEntry).
        decision_summary = self._build_decision_summary(
            context=context,
            active_policy_set=active_policy_set,
            evaluation_result=evaluation_result,
            input_hash=input_hash,
        )

        # Passo 7: Construir DecisionTrail (auditoria expandida, best-effort).
        decision_trail = self._build_decision_trail(
            context=context,
            active_policy_set=active_policy_set,
            evaluation_result=evaluation_result,
            input_hash=input_hash,
        )

        # Passo 8: Emitir DecisionTrail de forma best-effort.
        # Falha de emissão NÃO invalida a transação (Requisito 13.4).
        self._trail_emitter.emit(decision_trail)

        # Passo 9: Verificar veredito e retornar ou levantar exceção.
        return self._build_result(
            evaluation_result=evaluation_result,
            decision_summary=decision_summary,
            context=context,
        )

    # ------------------------------------------------------------------
    # Resolução de escopo
    # ------------------------------------------------------------------

    def _resolve_scope(self, context: "CanonicalValidationContext") -> PolicyScope:
        """
        Resolve o PolicyScope a partir do contexto canônico.

        O escopo é derivado dos campos de identificação do contexto:
        tenant_id, operation_type, product_code, channel.
        O environment é fixo em "prod" por padrão — pode ser configurado
        via variável de ambiente em implementações futuras.

        Args:
            context: Contexto canônico da transação.

        Returns:
            PolicyScope com os campos de identificação do contexto.
        """
        return PolicyScope(
            tenant_id=context.tenant_id,
            operation_type=context.operation_type,
            product_code=context.product_code,
            channel=context.channel,
        )

    # ------------------------------------------------------------------
    # Cálculo do input_hash
    # ------------------------------------------------------------------

    def _compute_input_hash(self, context: "CanonicalValidationContext") -> str:
        """
        Calcula o SHA-256 do contexto canônico para rastreabilidade e replay.

        O input_hash é usado para verificação de integridade no replay —
        NÃO é suficiente para reconstruir o input. O replay usa o JournalEntry
        completo persistido no ledger.

        A serialização é determinística (chaves ordenadas, tipos canônicos)
        para garantir que o mesmo contexto produza sempre o mesmo hash.

        Args:
            context: Contexto canônico da transação.

        Returns:
            String "sha256:<hex>" com o hash do contexto.
        """
        # Serializa apenas os campos que participam da decisão semântica.
        # Campos de identificação (tenant_id, external_id) são incluídos
        # para correlação, mas não afetam a avaliação das rules.
        context_dict = {
            "tenant_id": context.tenant_id,
            "external_id": context.external_id,
            "operation_type": context.operation_type,
            "product_code": context.product_code,
            "channel": context.channel,
            "postings": [
                {
                    "account_id": p.account_id,
                    "amount": p.amount,
                    "currency": p.currency,
                    "direction": p.direction,
                    "account_type": p.account_type,
                }
                for p in context.postings
            ],
            "policy_context": dict(context.policy_context),
            "context_schema_version": context.context_schema_version,
        }
        serialized = json.dumps(context_dict, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    # ------------------------------------------------------------------
    # Construção do DecisionSummary
    # ------------------------------------------------------------------

    def _build_decision_summary(
        self,
        context: "CanonicalValidationContext",
        active_policy_set: "ActivePolicySet",
        evaluation_result: "EvaluationResult",
        input_hash: str,
    ) -> DecisionSummary:
        """
        Constrói o DecisionSummary para persistência atômica com o JournalEntry.

        O summary contém o mínimo necessário para rastreabilidade e replay:
        veredito, escopo, identificadores de artefatos e métricas de latência.

        Args:
            context:           Contexto canônico da transação.
            active_policy_set: Conjunto de policy usado na avaliação.
            evaluation_result: Resultado da avaliação das rules.
            input_hash:        Hash SHA-256 do contexto canônico.

        Returns:
            DecisionSummary imutável pronto para persistência.
        """
        manifest = active_policy_set.manifest
        decision = evaluation_result.decision
        metrics = evaluation_result.metrics

        return DecisionSummary(
            final_verdict=decision.final_verdict,
            policy_scope_id=manifest.policy_scope_id,
            activation_id=manifest.activation_id,
            artifact_hash=manifest.artifact_hash,
            snapshot_version=manifest.snapshot_version,
            evaluator_version=EVALUATOR_VERSION,
            input_hash=input_hash,
            matched_deny_rule=decision.matched_deny_rule,
            evaluation_latency_ms=metrics.evaluation_latency_ms,
        )

    # ------------------------------------------------------------------
    # Construção do DecisionTrail
    # ------------------------------------------------------------------

    def _build_decision_trail(
        self,
        context: "CanonicalValidationContext",
        active_policy_set: "ActivePolicySet",
        evaluation_result: "EvaluationResult",
        input_hash: str,
    ) -> DecisionTrail:
        """
        Constrói o DecisionTrail expandido para auditoria e analytics.

        O trail contém todos os campos do summary mais a lista completa
        de rules avaliadas e o timestamp da avaliação.

        Nota sobre external_id vs entry_id:
        O trail usa external_id porque a validação ocorre antes da criação
        do JournalEntry — o entry_id ainda não existe neste momento.

        Args:
            context:           Contexto canônico da transação.
            active_policy_set: Conjunto de policy usado na avaliação.
            evaluation_result: Resultado da avaliação das rules.
            input_hash:        Hash SHA-256 do contexto canônico.

        Returns:
            DecisionTrail imutável pronto para emissão ao Firehose.
        """
        manifest = active_policy_set.manifest
        decision = evaluation_result.decision
        metrics = evaluation_result.metrics
        timestamp = datetime.now(tz=timezone.utc).isoformat()

        return DecisionTrail(
            external_id=context.external_id,
            tenant_id=context.tenant_id,
            policy_scope_id=manifest.policy_scope_id,
            activation_id=manifest.activation_id,
            artifact_hash=manifest.artifact_hash,
            snapshot_version=manifest.snapshot_version,
            evaluator_version=EVALUATOR_VERSION,
            input_hash=input_hash,
            final_verdict=decision.final_verdict,
            matched_deny_rule=decision.matched_deny_rule,
            rules=decision.rules,
            evaluation_latency_ms=metrics.evaluation_latency_ms,
            error_code=None,
            timestamp=timestamp,
        )

    # ------------------------------------------------------------------
    # Construção do resultado final
    # ------------------------------------------------------------------

    def _build_result(
        self,
        evaluation_result: "EvaluationResult",
        decision_summary: DecisionSummary,
        context: "CanonicalValidationContext",
    ) -> "ValidationResult":
        """
        Constrói o ValidationResult com base no veredito da avaliação.

        Se APPROVED: retorna ValidationResult.success() com artefatos.
        Se REJECTED: levanta PolicyRejected com a mensagem da rule DENY.

        O ValidationResult carrega o DecisionSummary nos artefatos para
        que o LedgerEngine possa persistir atomicamente com o JournalEntry.

        Args:
            evaluation_result: Resultado da avaliação das rules.
            decision_summary:  Summary construído para persistência.
            context:           Contexto canônico (para logging).

        Returns:
            ValidationResult com artefatos se APPROVED.

        Raises:
            PolicyRejected: se o veredito for REJECTED.
        """
        from ledger.domain.validators import ValidationArtifacts, ValidationResult

        decision = evaluation_result.decision

        if decision.final_verdict == FinalVerdict.APPROVED:
            logger.info(
                "policy validation aprovada",
                extra={
                    "external_id": context.external_id,
                    "tenant_id": context.tenant_id,
                    "policy_scope_id": decision_summary.policy_scope_id,
                    "activation_id": decision_summary.activation_id,
                    "artifact_hash": decision_summary.artifact_hash,
                    "snapshot_version": decision_summary.snapshot_version,
                    "final_verdict": decision.final_verdict.value,
                    "evaluation_latency_ms": decision_summary.evaluation_latency_ms,
                    "operation": "policy_validation",
                },
            )
            # Retorna ValidationResult com artefatos para persistência pelo LedgerEngine.
            # O DecisionSummary é carregado nos artefatos para que o LedgerEngine
            # possa persistir atomicamente com o JournalEntry (Requisito 12.3, 12.4).
            artifacts = ValidationArtifacts(decision_summary=decision_summary)
            return ValidationResult.success(artifacts=artifacts)

        # Veredito REJECTED — levanta PolicyRejected com a mensagem da rule DENY.
        deny_rule = decision.matched_deny_rule or "unknown_rule"
        deny_message = self._find_deny_message(evaluation_result, deny_rule)

        logger.info(
            "policy validation rejeitada",
            extra={
                "external_id": context.external_id,
                "tenant_id": context.tenant_id,
                "policy_scope_id": decision_summary.policy_scope_id,
                "activation_id": decision_summary.activation_id,
                "artifact_hash": decision_summary.artifact_hash,
                "matched_deny_rule": deny_rule,
                "final_verdict": decision.final_verdict.value,
                "evaluation_latency_ms": decision_summary.evaluation_latency_ms,
                "operation": "policy_validation",
            },
        )

        raise PolicyRejected(
            f"Transação rejeitada pela rule '{deny_rule}': {deny_message}"
        )

    def _find_deny_message(
        self, evaluation_result: "EvaluationResult", deny_rule_name: str
    ) -> str:
        """
        Encontra a mensagem da rule DENY que determinou a rejeição.

        Percorre os resultados das rules para encontrar a mensagem
        associada à rule DENY que casou.

        Args:
            evaluation_result: Resultado da avaliação com todos os resultados de rules.
            deny_rule_name:    Nome da rule DENY que determinou a rejeição.

        Returns:
            Mensagem descritiva da rule DENY, ou string vazia se não encontrada.
        """
        for rule_result in evaluation_result.decision.rules:
            if rule_result.rule_name == deny_rule_name and rule_result.matched:
                return rule_result.message
        return ""
