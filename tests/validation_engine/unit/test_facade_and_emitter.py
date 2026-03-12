"""
Testes unitários para PolicyValidationFacade e DecisionTrailEmitter.

Verifica:
- Facade: aprovação — retorna ValidationResult.success() com artefatos
- Facade: rejeição — levanta PolicyRejected com mensagem da rule DENY
- Facade: runtime não pronto — PolicyEngineNotReady propaga corretamente
- Facade: falha do emitter — não propaga exceção (best-effort)
- Facade: comando imutável — o comando original não é mutado
- Emitter: serialização correta do trail para Firehose
- Emitter: falha de envio é capturada e logada (best-effort)
- Emitter: NoOpDecisionTrailEmitter registra trails em memória
- Emitter: FailingDecisionTrailEmitter levanta RuntimeError

Requisitos cobertos: 7.1, 7.2, 7.3, 13.4, 17.1
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping
from unittest.mock import MagicMock, patch

import pytest

from validation_engine.application.facade import PolicyValidationFacade, ValidationArtifacts
from validation_engine.domain.context import (
    CanonicalPosting,
    CanonicalValidationContext,
    DerivedFacts,
)
from validation_engine.domain.errors import PolicyEngineNotReady, PolicyRejected
from validation_engine.domain.evaluator import EVALUATOR_VERSION, RuleEvaluator
from validation_engine.domain.models import (
    ActivePolicySet,
    BundleCompatibility,
    CompilationMetadata,
    DecisionTrail,
    PolicyActivationManifest,
    ReferenceSnapshot,
    RuleBundle,
)
from validation_engine.domain.policy_ast import (
    CompositionMode,
    ComparisonNode,
    FieldAccessNode,
    FinalVerdict,
    LiteralNode,
    PolicyEffect,
    PolicyRuleNode,
    RuleAST,
)
from validation_engine.infrastructure.decision_trail_emitter import (
    FailingDecisionTrailEmitter,
    FirehoseDecisionTrailEmitter,
    NoOpDecisionTrailEmitter,
)


# ---------------------------------------------------------------------------
# Helpers de construção de fixtures
# ---------------------------------------------------------------------------


def _make_manifest(
    activation_id: str = "act_001",
    scope_id: str = "tenantA:TRANSFER:PIX:*:prod",
    artifact_hash: str = "sha256:abc123",
    snapshot_version: str = "snap_001",
) -> PolicyActivationManifest:
    return PolicyActivationManifest(
        activation_id=activation_id,
        policy_scope_id=scope_id,
        artifact_hash=artifact_hash,
        snapshot_version=snapshot_version,
        context_schema_version="1.0",
        evaluator_version=EVALUATOR_VERSION,
        activated_at="2026-01-01T00:00:00Z",
        activated_by="test",
    )


def _make_bundle(
    always_deny: bool = False,
    artifact_hash: str = "sha256:abc123",
) -> RuleBundle:
    """Cria um bundle com uma rule que sempre aprova ou sempre nega."""
    rule = PolicyRuleNode(
        name="deny_always" if always_deny else "allow_always",
        priority=100,
        condition=LiteralNode(value=always_deny),
        effect=PolicyEffect.DENY if always_deny else PolicyEffect.ALLOW,
        message="Deny rule for testing" if always_deny else "Allow rule for testing",
    )
    return RuleBundle(
        policy_set_id="test-policy-set",
        artifact_hash=artifact_hash,
        ast=RuleAST(rules=(rule,), composition_mode=CompositionMode.DENY_OVERRIDES),
        execution_plan={},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version="1.0",
            snapshot_schema_version="1.0",
            evaluator_min_version="1.0.0",
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author="test",
            description="Test bundle",
            compiled_at="2026-01-01T00:00:00Z",
            source_hash="sha256:source123",
        ),
    )


def _make_snapshot() -> ReferenceSnapshot:
    return ReferenceSnapshot(
        snapshot_version="snap_001",
        snapshot_schema_version="1.0",
        created_at="2026-01-01T00:00:00Z",
        data={"daily_limit_minor": 500000},
    )


def _make_active_policy_set(always_deny: bool = False) -> ActivePolicySet:
    manifest = _make_manifest()
    return ActivePolicySet(
        manifest=manifest,
        bundle=_make_bundle(always_deny=always_deny),
        snapshot=_make_snapshot(),
        loaded_at="2026-01-01T00:00:00Z",
        integrity_verified=True,
    )


def _make_context(
    tenant_id: str = "tenantA",
    external_id: str = "ext_001",
    operation_type: str = "TRANSFER",
    product_code: str | None = "PIX",
    channel: str | None = None,
) -> CanonicalValidationContext:
    """Cria um CanonicalValidationContext mínimo para testes."""
    postings = (
        CanonicalPosting(
            account_id="acc_debit",
            amount=10000,
            currency="BRL",
            direction="DEBIT",
        ),
        CanonicalPosting(
            account_id="acc_credit",
            amount=10000,
            currency="BRL",
            direction="CREDIT",
        ),
    )
    facts = DerivedFacts(
        posting_count=2,
        distinct_account_count=2,
        currencies=("BRL",),
        total_debits_by_currency={"BRL": 10000},
        total_credits_by_currency={"BRL": 10000},
        max_posting_amount=10000,
        has_platform_account=False,
    )
    return CanonicalValidationContext(
        tenant_id=tenant_id,
        external_id=external_id,
        operation_type=operation_type,
        product_code=product_code,
        channel=channel,
        postings=postings,
        policy_context={"daily_limit_minor": 500000},
        facts=facts,
        context_schema_version="1.0",
    )


@dataclass
class _FakeCommand:
    """Comando fake para testes — simula CreateJournalEntryCommand."""

    external_id: str = "ext_001"
    tenant_id: str = "tenantA"
    operation_type: str = "TRANSFER"
    product_code: str | None = "PIX"
    channel: str | None = None
    postings: tuple = field(default_factory=tuple)
    policy_context: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)
    # Campo extra para verificar imutabilidade
    _original_external_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_original_external_id", self.external_id)


# ---------------------------------------------------------------------------
# Fixtures de facade
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_context_builder() -> MagicMock:
    """Context builder que retorna um contexto canônico fixo."""
    builder = MagicMock()
    builder.build.return_value = _make_context()
    return builder


@pytest.fixture
def mock_registry_approved() -> MagicMock:
    """Registry que retorna um ActivePolicySet que sempre aprova."""
    registry = MagicMock()
    registry.get_active_policy_set.return_value = _make_active_policy_set(always_deny=False)
    return registry


@pytest.fixture
def mock_registry_denied() -> MagicMock:
    """Registry que retorna um ActivePolicySet que sempre nega."""
    registry = MagicMock()
    registry.get_active_policy_set.return_value = _make_active_policy_set(always_deny=True)
    return registry


@pytest.fixture
def mock_registry_not_ready() -> MagicMock:
    """Registry que levanta PolicyEngineNotReady."""
    registry = MagicMock()
    registry.get_active_policy_set.side_effect = PolicyEngineNotReady(
        "Motor sem policy válida"
    )
    return registry


@pytest.fixture
def noop_emitter() -> NoOpDecisionTrailEmitter:
    return NoOpDecisionTrailEmitter()


@pytest.fixture
def failing_emitter() -> FailingDecisionTrailEmitter:
    return FailingDecisionTrailEmitter()


def _make_facade(
    context_builder: MagicMock,
    registry: MagicMock,
    emitter: object,
) -> PolicyValidationFacade:
    return PolicyValidationFacade(
        context_builder=context_builder,
        runtime_registry=registry,
        evaluator=RuleEvaluator(),
        trail_emitter=emitter,
    )


# ---------------------------------------------------------------------------
# Testes de aprovação
# ---------------------------------------------------------------------------


class TestFacadeApproval:
    """Testa o caminho feliz: policy aprova a transação."""

    def test_validate_retorna_success_quando_aprovado(
        self,
        mock_context_builder: MagicMock,
        mock_registry_approved: MagicMock,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """validate() deve retornar ValidationResult.success() quando aprovado."""
        facade = _make_facade(mock_context_builder, mock_registry_approved, noop_emitter)
        command = _FakeCommand()

        result = facade.validate(command)

        assert result.is_valid is True

    def test_validate_emite_trail_quando_aprovado(
        self,
        mock_context_builder: MagicMock,
        mock_registry_approved: MagicMock,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """validate() deve emitir exatamente um DecisionTrail quando aprovado."""
        facade = _make_facade(mock_context_builder, mock_registry_approved, noop_emitter)
        command = _FakeCommand()

        facade.validate(command)

        assert len(noop_emitter.emitted_trails) == 1
        trail = noop_emitter.emitted_trails[0]
        assert trail.final_verdict == FinalVerdict.APPROVED
        assert trail.external_id == "ext_001"
        assert trail.tenant_id == "tenantA"

    def test_validate_trail_contem_campos_obrigatorios(
        self,
        mock_context_builder: MagicMock,
        mock_registry_approved: MagicMock,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """O DecisionTrail emitido deve conter todos os campos obrigatórios."""
        facade = _make_facade(mock_context_builder, mock_registry_approved, noop_emitter)
        facade.validate(_FakeCommand())

        trail = noop_emitter.emitted_trails[0]
        assert trail.activation_id == "act_001"
        assert trail.artifact_hash == "sha256:abc123"
        assert trail.snapshot_version == "snap_001"
        assert trail.evaluator_version == EVALUATOR_VERSION
        assert trail.input_hash.startswith("sha256:")
        assert trail.error_code is None
        assert trail.timestamp is not None


# ---------------------------------------------------------------------------
# Testes de rejeição
# ---------------------------------------------------------------------------


class TestFacadeRejection:
    """Testa o caminho de rejeição: policy nega a transação."""

    def test_validate_levanta_policy_rejected_quando_negado(
        self,
        mock_context_builder: MagicMock,
        mock_registry_denied: MagicMock,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """validate() deve levantar PolicyRejected quando uma rule DENY casa."""
        facade = _make_facade(mock_context_builder, mock_registry_denied, noop_emitter)
        command = _FakeCommand()

        with pytest.raises(PolicyRejected) as exc_info:
            facade.validate(command)

        assert "deny_always" in str(exc_info.value)

    def test_validate_emite_trail_mesmo_quando_rejeitado(
        self,
        mock_context_builder: MagicMock,
        mock_registry_denied: MagicMock,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """O DecisionTrail deve ser emitido mesmo quando a transação é rejeitada."""
        facade = _make_facade(mock_context_builder, mock_registry_denied, noop_emitter)

        with pytest.raises(PolicyRejected):
            facade.validate(_FakeCommand())

        # Trail deve ter sido emitido antes da exceção
        assert len(noop_emitter.emitted_trails) == 1
        trail = noop_emitter.emitted_trails[0]
        assert trail.final_verdict == FinalVerdict.REJECTED
        assert trail.matched_deny_rule == "deny_always"

    def test_policy_rejected_tem_codigo_correto(
        self,
        mock_context_builder: MagicMock,
        mock_registry_denied: MagicMock,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """PolicyRejected deve ter o código POLICY_REJECTED."""
        facade = _make_facade(mock_context_builder, mock_registry_denied, noop_emitter)

        with pytest.raises(PolicyRejected) as exc_info:
            facade.validate(_FakeCommand())

        assert exc_info.value.code == "POLICY_REJECTED"
        assert exc_info.value.http_status == 422


# ---------------------------------------------------------------------------
# Testes de runtime não pronto
# ---------------------------------------------------------------------------


class TestFacadeEngineNotReady:
    """Testa o comportamento fail-closed quando o motor não está pronto."""

    def test_validate_propaga_policy_engine_not_ready(
        self,
        mock_context_builder: MagicMock,
        mock_registry_not_ready: MagicMock,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """validate() deve propagar PolicyEngineNotReady sem capturar."""
        facade = _make_facade(mock_context_builder, mock_registry_not_ready, noop_emitter)

        with pytest.raises(PolicyEngineNotReady):
            facade.validate(_FakeCommand())

    def test_nenhum_trail_emitido_quando_engine_not_ready(
        self,
        mock_context_builder: MagicMock,
        mock_registry_not_ready: MagicMock,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """Nenhum trail deve ser emitido se o motor não está pronto."""
        facade = _make_facade(mock_context_builder, mock_registry_not_ready, noop_emitter)

        with pytest.raises(PolicyEngineNotReady):
            facade.validate(_FakeCommand())

        assert len(noop_emitter.emitted_trails) == 0


# ---------------------------------------------------------------------------
# Testes de isolamento de falha do emitter
# ---------------------------------------------------------------------------


class TestFacadeEmitterFailureIsolation:
    """Testa que falha do emitter não propaga ao chamador (best-effort)."""

    def test_falha_do_emitter_nao_propaga_excecao(
        self,
        mock_context_builder: MagicMock,
        mock_registry_approved: MagicMock,
        failing_emitter: FailingDecisionTrailEmitter,
    ) -> None:
        """
        Falha na emissão do DecisionTrail NÃO deve propagar exceção.

        O FailingDecisionTrailEmitter levanta RuntimeError, mas a facade
        usa FirehoseDecisionTrailEmitter que captura erros. Para testar
        o isolamento, usamos um emitter que falha mas está encapsulado
        em um FirehoseDecisionTrailEmitter com cliente mockado que falha.
        """
        # Cria um emitter Firehose com cliente que sempre falha
        mock_firehose_client = MagicMock()
        mock_firehose_client.put_record.side_effect = RuntimeError("Firehose indisponível")

        firehose_emitter = FirehoseDecisionTrailEmitter(
            firehose_client=mock_firehose_client,
            delivery_stream_name="test-stream",
        )

        facade = _make_facade(mock_context_builder, mock_registry_approved, firehose_emitter)

        # Não deve levantar exceção mesmo com Firehose falhando
        result = facade.validate(_FakeCommand())
        assert result.is_valid is True

    def test_transacao_aprovada_mesmo_com_emitter_falhando(
        self,
        mock_context_builder: MagicMock,
        mock_registry_approved: MagicMock,
    ) -> None:
        """Transação aprovada deve ser válida mesmo se o emitter falhar."""
        mock_firehose_client = MagicMock()
        mock_firehose_client.put_record.side_effect = Exception("Erro genérico")

        firehose_emitter = FirehoseDecisionTrailEmitter(
            firehose_client=mock_firehose_client,
            delivery_stream_name="test-stream",
        )

        facade = _make_facade(mock_context_builder, mock_registry_approved, firehose_emitter)
        result = facade.validate(_FakeCommand())

        assert result.is_valid is True


# ---------------------------------------------------------------------------
# Testes de imutabilidade do comando
# ---------------------------------------------------------------------------


class TestFacadeCommandImmutability:
    """Testa que o comando original não é mutado pelo pipeline."""

    def test_comando_nao_e_mutado_em_aprovacao(
        self,
        mock_context_builder: MagicMock,
        mock_registry_approved: MagicMock,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """O comando original não deve ser mutado quando aprovado."""
        facade = _make_facade(mock_context_builder, mock_registry_approved, noop_emitter)
        command = _FakeCommand(external_id="ext_imutavel_001")
        original_external_id = command.external_id

        facade.validate(command)

        # O external_id não deve ter sido alterado
        assert command.external_id == original_external_id

    def test_comando_nao_e_mutado_em_rejeicao(
        self,
        mock_context_builder: MagicMock,
        mock_registry_denied: MagicMock,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """O comando original não deve ser mutado quando rejeitado."""
        facade = _make_facade(mock_context_builder, mock_registry_denied, noop_emitter)
        command = _FakeCommand(external_id="ext_imutavel_002")
        original_external_id = command.external_id

        with pytest.raises(PolicyRejected):
            facade.validate(command)

        assert command.external_id == original_external_id

    def test_context_builder_recebe_o_comando_original(
        self,
        mock_context_builder: MagicMock,
        mock_registry_approved: MagicMock,
        noop_emitter: NoOpDecisionTrailEmitter,
    ) -> None:
        """O context builder deve receber o comando original sem modificações."""
        facade = _make_facade(mock_context_builder, mock_registry_approved, noop_emitter)
        command = _FakeCommand()

        facade.validate(command)

        # O context builder deve ter sido chamado com o comando original
        mock_context_builder.build.assert_called_once_with(command)


# ---------------------------------------------------------------------------
# Testes do DecisionTrailEmitter
# ---------------------------------------------------------------------------


class TestFirehoseDecisionTrailEmitter:
    """Testa o FirehoseDecisionTrailEmitter."""

    def _make_trail(self) -> DecisionTrail:
        """Cria um DecisionTrail mínimo para testes."""
        from validation_engine.domain.models import RuleMatchResult

        return DecisionTrail(
            external_id="ext_001",
            tenant_id="tenantA",
            policy_scope_id="tenantA:TRANSFER:PIX:*:prod",
            activation_id="act_001",
            artifact_hash="sha256:abc123",
            snapshot_version="snap_001",
            evaluator_version=EVALUATOR_VERSION,
            input_hash="sha256:inputhash",
            final_verdict=FinalVerdict.APPROVED,
            matched_deny_rule=None,
            rules=(
                RuleMatchResult(
                    rule_name="allow_always",
                    effect=PolicyEffect.ALLOW,
                    matched=True,
                    priority=100,
                    message="Allow rule",
                ),
            ),
            evaluation_latency_ms=2.5,
            error_code=None,
            timestamp="2026-01-01T00:00:00Z",
        )

    def test_emit_chama_put_record_com_payload_correto(self) -> None:
        """emit() deve chamar put_record com o payload JSON serializado."""
        mock_client = MagicMock()
        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=mock_client,
            delivery_stream_name="test-stream",
        )
        trail = self._make_trail()

        emitter.emit(trail)

        mock_client.put_record.assert_called_once()
        call_kwargs = mock_client.put_record.call_args
        assert call_kwargs[1]["DeliveryStreamName"] == "test-stream"
        record_data = call_kwargs[1]["Record"]["Data"]
        # Payload deve ser bytes UTF-8 com newline ao final
        assert isinstance(record_data, bytes)
        assert record_data.endswith(b"\n")

    def test_emit_serializa_trail_como_json_valido(self) -> None:
        """O payload enviado ao Firehose deve ser JSON válido."""
        import json as json_module

        mock_client = MagicMock()
        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=mock_client,
            delivery_stream_name="test-stream",
        )
        trail = self._make_trail()

        emitter.emit(trail)

        record_data = mock_client.put_record.call_args[1]["Record"]["Data"]
        # Remove o newline ao final antes de parsear
        parsed = json_module.loads(record_data.decode("utf-8").strip())
        assert parsed["external_id"] == "ext_001"
        assert parsed["tenant_id"] == "tenantA"
        assert parsed["final_verdict"] == "APPROVED"

    def test_emit_captura_falha_sem_propagar(self) -> None:
        """emit() deve capturar falha do Firehose sem propagar exceção."""
        mock_client = MagicMock()
        mock_client.put_record.side_effect = RuntimeError("Firehose indisponível")
        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=mock_client,
            delivery_stream_name="test-stream",
        )
        trail = self._make_trail()

        # Não deve levantar exceção
        emitter.emit(trail)

    def test_emit_loga_falha_com_contexto(self) -> None:
        """emit() deve logar a falha com contexto suficiente para diagnóstico."""
        mock_client = MagicMock()
        mock_client.put_record.side_effect = RuntimeError("Firehose indisponível")
        emitter = FirehoseDecisionTrailEmitter(
            firehose_client=mock_client,
            delivery_stream_name="test-stream",
        )
        trail = self._make_trail()

        with patch(
            "validation_engine.infrastructure.decision_trail_emitter.logger"
        ) as mock_logger:
            emitter.emit(trail)
            mock_logger.error.assert_called_once()
            log_call = mock_logger.error.call_args
            # Verifica que a mensagem de log contém informação sobre a falha
            assert "falha" in log_call[0][0].lower()


class TestNoOpDecisionTrailEmitter:
    """Testa o NoOpDecisionTrailEmitter."""

    def test_emit_registra_trail_em_memoria(self) -> None:
        """emit() deve registrar o trail na lista emitted_trails."""
        from validation_engine.domain.models import RuleMatchResult

        emitter = NoOpDecisionTrailEmitter()
        trail = DecisionTrail(
            external_id="ext_001",
            tenant_id="tenantA",
            policy_scope_id="tenantA:TRANSFER:*:*:prod",
            activation_id="act_001",
            artifact_hash="sha256:abc",
            snapshot_version="snap_001",
            evaluator_version=EVALUATOR_VERSION,
            input_hash="sha256:input",
            final_verdict=FinalVerdict.APPROVED,
            matched_deny_rule=None,
            rules=(),
            evaluation_latency_ms=1.0,
            error_code=None,
            timestamp="2026-01-01T00:00:00Z",
        )

        emitter.emit(trail)

        assert len(emitter.emitted_trails) == 1
        assert emitter.emitted_trails[0] is trail

    def test_emit_acumula_multiplos_trails(self) -> None:
        """emit() deve acumular múltiplos trails na lista."""
        emitter = NoOpDecisionTrailEmitter()

        for i in range(3):
            trail = DecisionTrail(
                external_id=f"ext_{i:03d}",
                tenant_id="tenantA",
                policy_scope_id="tenantA:TRANSFER:*:*:prod",
                activation_id="act_001",
                artifact_hash="sha256:abc",
                snapshot_version="snap_001",
                evaluator_version=EVALUATOR_VERSION,
                input_hash="sha256:input",
                final_verdict=FinalVerdict.APPROVED,
                matched_deny_rule=None,
                rules=(),
                evaluation_latency_ms=1.0,
                error_code=None,
                timestamp="2026-01-01T00:00:00Z",
            )
            emitter.emit(trail)

        assert len(emitter.emitted_trails) == 3


class TestFailingDecisionTrailEmitter:
    """Testa o FailingDecisionTrailEmitter."""

    def test_emit_levanta_runtime_error(self) -> None:
        """emit() deve levantar RuntimeError para simular falha."""
        emitter = FailingDecisionTrailEmitter()
        trail = DecisionTrail(
            external_id="ext_001",
            tenant_id="tenantA",
            policy_scope_id="tenantA:TRANSFER:*:*:prod",
            activation_id="act_001",
            artifact_hash="sha256:abc",
            snapshot_version="snap_001",
            evaluator_version=EVALUATOR_VERSION,
            input_hash="sha256:input",
            final_verdict=FinalVerdict.APPROVED,
            matched_deny_rule=None,
            rules=(),
            evaluation_latency_ms=1.0,
            error_code=None,
            timestamp="2026-01-01T00:00:00Z",
        )

        with pytest.raises(RuntimeError, match="Falha simulada"):
            emitter.emit(trail)
