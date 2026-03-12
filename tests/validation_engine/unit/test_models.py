"""
Testes unitários para os modelos de domínio do Validation Engine.

Verifica:
- Imutabilidade de todos os modelos (frozen dataclasses)
- Igualdade estrutural por valor
- PolicyScope.scope_id: derivação determinística
- RuleBundle: serialização to_json / desserialização from_json (round-trip)
- ReferenceSnapshot.lookup: acesso por path
- DecisionSummary.to_metadata: formato de payload para o JournalEntry
- DecisionTrail.to_firehose_payload: formato de payload para o Firehose
- ActivePolicySet: campos e invariantes

Requisitos cobertos: 4.1, 4.2, 5.1, 5.2, 6.1, 6.2, 11.1, 12.1, 12.2,
                     13.1, 13.2, 24.1, 24.2
"""

import json

import pytest

from validation_engine.domain.models import (
    ActivePolicySet,
    BundleCompatibility,
    CompilationMetadata,
    DecisionSummary,
    DecisionTrail,
    EvaluationDecision,
    EvaluationMetrics,
    EvaluationResult,
    PolicyActivationManifest,
    PolicyScope,
    ReferenceSnapshot,
    RuleBundle,
    RuleMatchResult,
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


# ---------------------------------------------------------------------------
# Fixtures reutilizáveis
# ---------------------------------------------------------------------------


@pytest.fixture
def bundle_compatibility() -> BundleCompatibility:
    return BundleCompatibility(
        dsl_version="1.0",
        context_schema_version="1.0",
        snapshot_schema_version="1.0",
        evaluator_min_version="1.0.0",
    )


@pytest.fixture
def compilation_metadata() -> CompilationMetadata:
    return CompilationMetadata(
        author="test-author",
        description="Test bundle",
        compiled_at="2026-03-11T00:00:00Z",
        source_hash="sha256:abc123",
    )


@pytest.fixture
def simple_rule_ast() -> RuleAST:
    """AST mínimo com uma rule DENY simples."""
    rule = PolicyRuleNode(
        name="deny_test",
        priority=100,
        condition=ComparisonNode(
            left=FieldAccessNode(path=("facts", "posting_count")),
            operator=">=",
            right=LiteralNode(value=2),
        ),
        effect=PolicyEffect.DENY,
        message="Test deny",
    )
    return RuleAST(rules=(rule,))


@pytest.fixture
def rule_bundle(simple_rule_ast, bundle_compatibility, compilation_metadata) -> RuleBundle:
    return RuleBundle(
        policy_set_id="test-policy-set",
        artifact_hash="sha256:deadbeef",
        ast=simple_rule_ast,
        execution_plan={"version": 1, "steps": []},
        compatibility=bundle_compatibility,
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=compilation_metadata,
    )


@pytest.fixture
def reference_snapshot() -> ReferenceSnapshot:
    return ReferenceSnapshot(
        snapshot_version="snap_001",
        snapshot_schema_version="1.0",
        created_at="2026-03-11T00:00:00Z",
        data={
            "daily_limit_minor": 100000,
            "blocked_accounts": ("acc_bad_1", "acc_bad_2"),
            "is_feature_enabled": True,
        },
    )


@pytest.fixture
def manifest() -> PolicyActivationManifest:
    return PolicyActivationManifest(
        activation_id="act_001",
        policy_scope_id="tenantA:TRANSFER:PIX:*:prod",
        artifact_hash="sha256:deadbeef",
        snapshot_version="snap_001",
        context_schema_version="1.0",
        evaluator_version="1.0.0",
        activated_at="2026-03-11T00:00:00Z",
        activated_by="ci-pipeline",
    )


@pytest.fixture
def active_policy_set(manifest, rule_bundle, reference_snapshot) -> ActivePolicySet:
    return ActivePolicySet(
        manifest=manifest,
        bundle=rule_bundle,
        snapshot=reference_snapshot,
        loaded_at="2026-03-11T00:00:00Z",
        integrity_verified=True,
    )


@pytest.fixture
def rule_match_deny() -> RuleMatchResult:
    return RuleMatchResult(
        rule_name="deny_test",
        effect=PolicyEffect.DENY,
        matched=True,
        priority=100,
        message="Test deny",
    )


@pytest.fixture
def rule_match_allow() -> RuleMatchResult:
    return RuleMatchResult(
        rule_name="allow_test",
        effect=PolicyEffect.ALLOW,
        matched=True,
        priority=10,
        message="Test allow",
    )


@pytest.fixture
def decision_summary() -> DecisionSummary:
    return DecisionSummary(
        final_verdict=FinalVerdict.APPROVED,
        policy_scope_id="tenantA:TRANSFER:PIX:*:prod",
        activation_id="act_001",
        artifact_hash="sha256:deadbeef",
        snapshot_version="snap_001",
        evaluator_version="1.0.0",
        input_hash="sha256:inputhash",
        matched_deny_rule=None,
        evaluation_latency_ms=3.5,
    )


@pytest.fixture
def decision_trail(rule_match_deny, rule_match_allow) -> DecisionTrail:
    return DecisionTrail(
        external_id="ext_txn_001",
        tenant_id="tenantA",
        policy_scope_id="tenantA:TRANSFER:PIX:*:prod",
        activation_id="act_001",
        artifact_hash="sha256:deadbeef",
        snapshot_version="snap_001",
        evaluator_version="1.0.0",
        input_hash="sha256:inputhash",
        final_verdict=FinalVerdict.REJECTED,
        matched_deny_rule="deny_test",
        rules=(rule_match_deny, rule_match_allow),
        evaluation_latency_ms=4.2,
        error_code=None,
        timestamp="2026-03-11T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# PolicyScope
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPolicyScope:
    """Verifica PolicyScope: campos, scope_id e imutabilidade."""

    def test_required_fields(self):
        scope = PolicyScope(tenant_id="tenantA", operation_type="TRANSFER")
        assert scope.tenant_id == "tenantA"
        assert scope.operation_type == "TRANSFER"

    def test_optional_fields_default_to_none(self):
        scope = PolicyScope(tenant_id="tenantA", operation_type="TRANSFER")
        assert scope.product_code is None
        assert scope.channel is None

    def test_default_environment_is_prod(self):
        scope = PolicyScope(tenant_id="tenantA", operation_type="TRANSFER")
        assert scope.environment == "prod"

    def test_scope_id_with_all_fields(self):
        scope = PolicyScope(
            tenant_id="tenantA",
            operation_type="TRANSFER",
            product_code="PIX",
            channel="MOBILE",
            environment="prod",
        )
        assert scope.scope_id == "tenantA:TRANSFER:PIX:MOBILE:prod"

    def test_scope_id_with_wildcards_for_optional_fields(self):
        """Campos opcionais ausentes devem gerar wildcard '*' no scope_id."""
        scope = PolicyScope(tenant_id="tenantA", operation_type="TRANSFER")
        assert scope.scope_id == "tenantA:TRANSFER:*:*:prod"

    def test_scope_id_is_deterministic(self):
        """Dois escopos com mesmos campos devem gerar o mesmo scope_id."""
        scope_a = PolicyScope(tenant_id="tenantA", operation_type="TRANSFER")
        scope_b = PolicyScope(tenant_id="tenantA", operation_type="TRANSFER")
        assert scope_a.scope_id == scope_b.scope_id

    def test_scope_id_differs_by_tenant(self):
        scope_a = PolicyScope(tenant_id="tenantA", operation_type="TRANSFER")
        scope_b = PolicyScope(tenant_id="tenantB", operation_type="TRANSFER")
        assert scope_a.scope_id != scope_b.scope_id

    def test_scope_id_differs_by_operation(self):
        scope_a = PolicyScope(tenant_id="tenantA", operation_type="TRANSFER")
        scope_b = PolicyScope(tenant_id="tenantA", operation_type="PAYMENT")
        assert scope_a.scope_id != scope_b.scope_id

    def test_immutability(self):
        scope = PolicyScope(tenant_id="tenantA", operation_type="TRANSFER")
        with pytest.raises(Exception):
            scope.tenant_id = "tenantB"  # type: ignore[misc]

    def test_equality_by_value(self):
        scope_a = PolicyScope(tenant_id="tenantA", operation_type="TRANSFER")
        scope_b = PolicyScope(tenant_id="tenantA", operation_type="TRANSFER")
        assert scope_a == scope_b


# ---------------------------------------------------------------------------
# PolicyActivationManifest
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPolicyActivationManifest:
    """Verifica PolicyActivationManifest: campos obrigatórios e imutabilidade."""

    def test_all_required_fields(self, manifest):
        assert manifest.activation_id == "act_001"
        assert manifest.policy_scope_id == "tenantA:TRANSFER:PIX:*:prod"
        assert manifest.artifact_hash == "sha256:deadbeef"
        assert manifest.snapshot_version == "snap_001"
        assert manifest.context_schema_version == "1.0"
        assert manifest.evaluator_version == "1.0.0"
        assert manifest.activated_at == "2026-03-11T00:00:00Z"
        assert manifest.activated_by == "ci-pipeline"

    def test_immutability(self, manifest):
        with pytest.raises(Exception):
            manifest.activation_id = "other"  # type: ignore[misc]

    def test_equality_by_value(self, manifest):
        manifest_copy = PolicyActivationManifest(
            activation_id="act_001",
            policy_scope_id="tenantA:TRANSFER:PIX:*:prod",
            artifact_hash="sha256:deadbeef",
            snapshot_version="snap_001",
            context_schema_version="1.0",
            evaluator_version="1.0.0",
            activated_at="2026-03-11T00:00:00Z",
            activated_by="ci-pipeline",
        )
        assert manifest == manifest_copy

    def test_inequality_different_activation_id(self, manifest):
        other = PolicyActivationManifest(
            activation_id="act_002",
            policy_scope_id=manifest.policy_scope_id,
            artifact_hash=manifest.artifact_hash,
            snapshot_version=manifest.snapshot_version,
            context_schema_version=manifest.context_schema_version,
            evaluator_version=manifest.evaluator_version,
            activated_at=manifest.activated_at,
            activated_by=manifest.activated_by,
        )
        assert manifest != other


# ---------------------------------------------------------------------------
# BundleCompatibility e CompilationMetadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleCompatibility:
    def test_fields(self, bundle_compatibility):
        assert bundle_compatibility.dsl_version == "1.0"
        assert bundle_compatibility.context_schema_version == "1.0"
        assert bundle_compatibility.snapshot_schema_version == "1.0"
        assert bundle_compatibility.evaluator_min_version == "1.0.0"

    def test_immutability(self, bundle_compatibility):
        with pytest.raises(Exception):
            bundle_compatibility.dsl_version = "2.0"  # type: ignore[misc]

    def test_equality_by_value(self, bundle_compatibility):
        other = BundleCompatibility(
            dsl_version="1.0",
            context_schema_version="1.0",
            snapshot_schema_version="1.0",
            evaluator_min_version="1.0.0",
        )
        assert bundle_compatibility == other


@pytest.mark.unit
class TestCompilationMetadata:
    def test_fields(self, compilation_metadata):
        assert compilation_metadata.author == "test-author"
        assert compilation_metadata.description == "Test bundle"
        assert compilation_metadata.compiled_at == "2026-03-11T00:00:00Z"
        assert compilation_metadata.source_hash == "sha256:abc123"

    def test_immutability(self, compilation_metadata):
        with pytest.raises(Exception):
            compilation_metadata.author = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RuleBundle — serialização e round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRuleBundle:
    """Verifica RuleBundle: campos, imutabilidade e round-trip JSON."""

    def test_fields(self, rule_bundle, simple_rule_ast, bundle_compatibility, compilation_metadata):
        assert rule_bundle.policy_set_id == "test-policy-set"
        assert rule_bundle.artifact_hash == "sha256:deadbeef"
        assert rule_bundle.ast == simple_rule_ast
        assert rule_bundle.compatibility == bundle_compatibility
        assert rule_bundle.composition_mode == CompositionMode.DENY_OVERRIDES
        assert rule_bundle.metadata == compilation_metadata

    def test_immutability(self, rule_bundle):
        with pytest.raises(Exception):
            rule_bundle.artifact_hash = "sha256:other"  # type: ignore[misc]

    def test_equality_by_value(self, rule_bundle, simple_rule_ast, bundle_compatibility, compilation_metadata):
        other = RuleBundle(
            policy_set_id="test-policy-set",
            artifact_hash="sha256:deadbeef",
            ast=simple_rule_ast,
            execution_plan={"version": 1, "steps": []},
            compatibility=bundle_compatibility,
            composition_mode=CompositionMode.DENY_OVERRIDES,
            metadata=compilation_metadata,
        )
        assert rule_bundle == other

    def test_to_json_produces_valid_json(self, rule_bundle):
        """to_json deve produzir JSON válido e parseável."""
        json_str = rule_bundle.to_json()
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)

    def test_to_json_contains_required_fields(self, rule_bundle):
        """O JSON deve conter todos os campos obrigatórios do bundle."""
        parsed = json.loads(rule_bundle.to_json())
        assert "policy_set_id" in parsed
        assert "artifact_hash" in parsed
        assert "ast" in parsed
        assert "execution_plan" in parsed
        assert "compatibility" in parsed
        assert "composition_mode" in parsed
        assert "metadata" in parsed

    def test_to_json_is_deterministic(self, rule_bundle):
        """Duas chamadas a to_json devem produzir o mesmo resultado."""
        assert rule_bundle.to_json() == rule_bundle.to_json()

    def test_from_json_round_trip(self, rule_bundle):
        """from_json(to_json(bundle)) deve reconstruir um bundle igual ao original."""
        json_str = rule_bundle.to_json()
        reconstructed = RuleBundle.from_json(json_str)
        assert reconstructed == rule_bundle

    def test_round_trip_preserves_artifact_hash(self, rule_bundle):
        reconstructed = RuleBundle.from_json(rule_bundle.to_json())
        assert reconstructed.artifact_hash == rule_bundle.artifact_hash

    def test_round_trip_preserves_composition_mode(self, rule_bundle):
        reconstructed = RuleBundle.from_json(rule_bundle.to_json())
        assert reconstructed.composition_mode == CompositionMode.DENY_OVERRIDES

    def test_round_trip_preserves_ast_structure(self, rule_bundle):
        """O AST reconstruído deve ser estruturalmente igual ao original."""
        reconstructed = RuleBundle.from_json(rule_bundle.to_json())
        assert reconstructed.ast == rule_bundle.ast

    def test_round_trip_preserves_rule_effect(self, rule_bundle):
        """O efeito das rules deve ser preservado no round-trip."""
        reconstructed = RuleBundle.from_json(rule_bundle.to_json())
        original_rule = rule_bundle.ast.rules[0]
        reconstructed_rule = reconstructed.ast.rules[0]
        assert reconstructed_rule.effect == original_rule.effect

    def test_round_trip_preserves_compatibility(self, rule_bundle):
        reconstructed = RuleBundle.from_json(rule_bundle.to_json())
        assert reconstructed.compatibility == rule_bundle.compatibility

    def test_round_trip_preserves_metadata(self, rule_bundle):
        reconstructed = RuleBundle.from_json(rule_bundle.to_json())
        assert reconstructed.metadata == rule_bundle.metadata

    def test_json_keys_are_sorted(self, rule_bundle):
        """O JSON deve ter chaves ordenadas para determinismo do hash."""
        json_str = rule_bundle.to_json()
        parsed = json.loads(json_str)
        keys = list(parsed.keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# ReferenceSnapshot — lookup
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReferenceSnapshot:
    """Verifica ReferenceSnapshot: campos, lookup e imutabilidade."""

    def test_fields(self, reference_snapshot):
        assert reference_snapshot.snapshot_version == "snap_001"
        assert reference_snapshot.snapshot_schema_version == "1.0"
        assert reference_snapshot.created_at == "2026-03-11T00:00:00Z"

    def test_lookup_integer_value(self, reference_snapshot):
        result = reference_snapshot.lookup(("daily_limit_minor",))
        assert result == 100000

    def test_lookup_tuple_value(self, reference_snapshot):
        result = reference_snapshot.lookup(("blocked_accounts",))
        assert result == ("acc_bad_1", "acc_bad_2")

    def test_lookup_bool_value(self, reference_snapshot):
        result = reference_snapshot.lookup(("is_feature_enabled",))
        assert result is True

    def test_lookup_missing_key_returns_none(self, reference_snapshot):
        result = reference_snapshot.lookup(("nonexistent_key",))
        assert result is None

    def test_lookup_empty_path_returns_none(self, reference_snapshot):
        """Path vazio não deve levantar exceção — retorna None."""
        result = reference_snapshot.lookup(())
        assert result is None

    def test_immutability(self, reference_snapshot):
        with pytest.raises(Exception):
            reference_snapshot.snapshot_version = "snap_002"  # type: ignore[misc]

    def test_equality_by_value(self, reference_snapshot):
        other = ReferenceSnapshot(
            snapshot_version="snap_001",
            snapshot_schema_version="1.0",
            created_at="2026-03-11T00:00:00Z",
            data={
                "daily_limit_minor": 100000,
                "blocked_accounts": ("acc_bad_1", "acc_bad_2"),
                "is_feature_enabled": True,
            },
        )
        assert reference_snapshot == other


# ---------------------------------------------------------------------------
# ActivePolicySet
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestActivePolicySet:
    """Verifica ActivePolicySet: campos, invariante de integridade e imutabilidade."""

    def test_fields(self, active_policy_set, manifest, rule_bundle, reference_snapshot):
        assert active_policy_set.manifest == manifest
        assert active_policy_set.bundle == rule_bundle
        assert active_policy_set.snapshot == reference_snapshot
        assert active_policy_set.loaded_at == "2026-03-11T00:00:00Z"
        assert active_policy_set.integrity_verified is True

    def test_integrity_verified_flag(self, active_policy_set):
        """integrity_verified deve ser True para um conjunto válido."""
        assert active_policy_set.integrity_verified is True

    def test_integrity_not_verified_is_representable(self, manifest, rule_bundle, reference_snapshot):
        """Deve ser possível criar um ActivePolicySet com integrity_verified=False."""
        unverified = ActivePolicySet(
            manifest=manifest,
            bundle=rule_bundle,
            snapshot=reference_snapshot,
            loaded_at="2026-03-11T00:00:00Z",
            integrity_verified=False,
        )
        assert unverified.integrity_verified is False

    def test_immutability(self, active_policy_set):
        with pytest.raises(Exception):
            active_policy_set.integrity_verified = False  # type: ignore[misc]

    def test_equality_by_value(self, active_policy_set, manifest, rule_bundle, reference_snapshot):
        other = ActivePolicySet(
            manifest=manifest,
            bundle=rule_bundle,
            snapshot=reference_snapshot,
            loaded_at="2026-03-11T00:00:00Z",
            integrity_verified=True,
        )
        assert active_policy_set == other


# ---------------------------------------------------------------------------
# RuleMatchResult
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRuleMatchResult:
    def test_deny_match_fields(self, rule_match_deny):
        assert rule_match_deny.rule_name == "deny_test"
        assert rule_match_deny.effect == PolicyEffect.DENY
        assert rule_match_deny.matched is True
        assert rule_match_deny.priority == 100
        assert rule_match_deny.message == "Test deny"

    def test_allow_match_fields(self, rule_match_allow):
        assert rule_match_allow.effect == PolicyEffect.ALLOW
        assert rule_match_allow.matched is True

    def test_unmatched_rule(self):
        result = RuleMatchResult(
            rule_name="deny_unmatched",
            effect=PolicyEffect.DENY,
            matched=False,
            priority=50,
            message="Did not match",
        )
        assert result.matched is False

    def test_immutability(self, rule_match_deny):
        with pytest.raises(Exception):
            rule_match_deny.matched = False  # type: ignore[misc]

    def test_equality_by_value(self, rule_match_deny):
        other = RuleMatchResult(
            rule_name="deny_test",
            effect=PolicyEffect.DENY,
            matched=True,
            priority=100,
            message="Test deny",
        )
        assert rule_match_deny == other


# ---------------------------------------------------------------------------
# EvaluationDecision, EvaluationMetrics, EvaluationResult
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvaluationDecision:
    def test_rejected_decision(self, rule_match_deny, rule_match_allow):
        decision = EvaluationDecision(
            final_verdict=FinalVerdict.REJECTED,
            matched_deny_rule="deny_test",
            rules=(rule_match_deny, rule_match_allow),
        )
        assert decision.final_verdict == FinalVerdict.REJECTED
        assert decision.matched_deny_rule == "deny_test"
        assert len(decision.rules) == 2

    def test_approved_decision(self, rule_match_allow):
        decision = EvaluationDecision(
            final_verdict=FinalVerdict.APPROVED,
            matched_deny_rule=None,
            rules=(rule_match_allow,),
        )
        assert decision.final_verdict == FinalVerdict.APPROVED
        assert decision.matched_deny_rule is None

    def test_immutability(self, rule_match_deny):
        decision = EvaluationDecision(
            final_verdict=FinalVerdict.REJECTED,
            matched_deny_rule="deny_test",
            rules=(rule_match_deny,),
        )
        with pytest.raises(Exception):
            decision.final_verdict = FinalVerdict.APPROVED  # type: ignore[misc]

    def test_rules_is_tuple(self, rule_match_deny):
        decision = EvaluationDecision(
            final_verdict=FinalVerdict.REJECTED,
            matched_deny_rule="deny_test",
            rules=(rule_match_deny,),
        )
        assert isinstance(decision.rules, tuple)


@pytest.mark.unit
class TestEvaluationMetrics:
    def test_fields(self):
        metrics = EvaluationMetrics(evaluation_latency_ms=5.3, evaluated_rules=3)
        assert metrics.evaluation_latency_ms == 5.3
        assert metrics.evaluated_rules == 3

    def test_immutability(self):
        metrics = EvaluationMetrics(evaluation_latency_ms=5.3, evaluated_rules=3)
        with pytest.raises(Exception):
            metrics.evaluated_rules = 10  # type: ignore[misc]

    def test_metrics_do_not_affect_decision_equality(self, rule_match_deny):
        """
        Métricas são efêmeras e não participam da igualdade semântica da decisão.
        Dois EvaluationDecision com mesmos campos devem ser iguais independentemente
        das métricas associadas.
        """
        decision_a = EvaluationDecision(
            final_verdict=FinalVerdict.REJECTED,
            matched_deny_rule="deny_test",
            rules=(rule_match_deny,),
        )
        decision_b = EvaluationDecision(
            final_verdict=FinalVerdict.REJECTED,
            matched_deny_rule="deny_test",
            rules=(rule_match_deny,),
        )
        # Decisões iguais com métricas diferentes
        result_a = EvaluationResult(
            decision=decision_a,
            metrics=EvaluationMetrics(evaluation_latency_ms=1.0, evaluated_rules=1),
        )
        result_b = EvaluationResult(
            decision=decision_b,
            metrics=EvaluationMetrics(evaluation_latency_ms=99.0, evaluated_rules=1),
        )
        # As decisões são iguais
        assert result_a.decision == result_b.decision
        # Os resultados completos diferem porque as métricas diferem
        assert result_a != result_b


@pytest.mark.unit
class TestEvaluationResult:
    def test_fields(self, rule_match_deny):
        decision = EvaluationDecision(
            final_verdict=FinalVerdict.REJECTED,
            matched_deny_rule="deny_test",
            rules=(rule_match_deny,),
        )
        metrics = EvaluationMetrics(evaluation_latency_ms=3.0, evaluated_rules=1)
        result = EvaluationResult(decision=decision, metrics=metrics)
        assert result.decision == decision
        assert result.metrics == metrics

    def test_immutability(self, rule_match_deny):
        decision = EvaluationDecision(
            final_verdict=FinalVerdict.APPROVED,
            matched_deny_rule=None,
            rules=(rule_match_deny,),
        )
        metrics = EvaluationMetrics(evaluation_latency_ms=1.0, evaluated_rules=1)
        result = EvaluationResult(decision=decision, metrics=metrics)
        with pytest.raises(Exception):
            result.decision = decision  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DecisionSummary — to_metadata payload
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDecisionSummary:
    """Verifica DecisionSummary: campos, imutabilidade e payload to_metadata."""

    def test_approved_summary_fields(self, decision_summary):
        assert decision_summary.final_verdict == FinalVerdict.APPROVED
        assert decision_summary.policy_scope_id == "tenantA:TRANSFER:PIX:*:prod"
        assert decision_summary.activation_id == "act_001"
        assert decision_summary.artifact_hash == "sha256:deadbeef"
        assert decision_summary.snapshot_version == "snap_001"
        assert decision_summary.evaluator_version == "1.0.0"
        assert decision_summary.input_hash == "sha256:inputhash"
        assert decision_summary.matched_deny_rule is None
        assert decision_summary.evaluation_latency_ms == 3.5

    def test_rejected_summary_has_deny_rule(self):
        summary = DecisionSummary(
            final_verdict=FinalVerdict.REJECTED,
            policy_scope_id="tenantA:TRANSFER:PIX:*:prod",
            activation_id="act_001",
            artifact_hash="sha256:deadbeef",
            snapshot_version="snap_001",
            evaluator_version="1.0.0",
            input_hash="sha256:inputhash",
            matched_deny_rule="deny_over_daily_limit",
            evaluation_latency_ms=2.1,
        )
        assert summary.final_verdict == FinalVerdict.REJECTED
        assert summary.matched_deny_rule == "deny_over_daily_limit"

    def test_immutability(self, decision_summary):
        with pytest.raises(Exception):
            decision_summary.final_verdict = FinalVerdict.REJECTED  # type: ignore[misc]

    def test_to_metadata_structure(self, decision_summary):
        """to_metadata deve retornar dict com chave 'policy_validation'."""
        metadata = decision_summary.to_metadata()
        assert "policy_validation" in metadata
        pv = metadata["policy_validation"]
        assert isinstance(pv, dict)

    def test_to_metadata_contains_all_required_fields(self, decision_summary):
        """O payload deve conter todos os campos do Requisito 12.2."""
        pv = decision_summary.to_metadata()["policy_validation"]
        required_fields = [
            "final_verdict",
            "policy_scope_id",
            "activation_id",
            "artifact_hash",
            "snapshot_version",
            "evaluator_version",
            "input_hash",
            "matched_deny_rule",
            "evaluation_latency_ms",
        ]
        for field_name in required_fields:
            assert field_name in pv, f"Campo ausente no payload: {field_name}"

    def test_to_metadata_final_verdict_is_string(self, decision_summary):
        """final_verdict no payload deve ser string, não enum."""
        pv = decision_summary.to_metadata()["policy_validation"]
        assert isinstance(pv["final_verdict"], str)
        assert pv["final_verdict"] == "APPROVED"

    def test_to_metadata_approved_has_null_deny_rule(self, decision_summary):
        pv = decision_summary.to_metadata()["policy_validation"]
        assert pv["matched_deny_rule"] is None

    def test_to_metadata_rejected_has_deny_rule_name(self):
        summary = DecisionSummary(
            final_verdict=FinalVerdict.REJECTED,
            policy_scope_id="tenantA:TRANSFER:PIX:*:prod",
            activation_id="act_001",
            artifact_hash="sha256:deadbeef",
            snapshot_version="snap_001",
            evaluator_version="1.0.0",
            input_hash="sha256:inputhash",
            matched_deny_rule="deny_over_daily_limit",
            evaluation_latency_ms=2.1,
        )
        pv = summary.to_metadata()["policy_validation"]
        assert pv["matched_deny_rule"] == "deny_over_daily_limit"
        assert pv["final_verdict"] == "REJECTED"

    def test_to_metadata_latency_is_numeric(self, decision_summary):
        pv = decision_summary.to_metadata()["policy_validation"]
        assert isinstance(pv["evaluation_latency_ms"], (int, float))

    def test_equality_by_value(self, decision_summary):
        other = DecisionSummary(
            final_verdict=FinalVerdict.APPROVED,
            policy_scope_id="tenantA:TRANSFER:PIX:*:prod",
            activation_id="act_001",
            artifact_hash="sha256:deadbeef",
            snapshot_version="snap_001",
            evaluator_version="1.0.0",
            input_hash="sha256:inputhash",
            matched_deny_rule=None,
            evaluation_latency_ms=3.5,
        )
        assert decision_summary == other


# ---------------------------------------------------------------------------
# DecisionTrail — to_firehose_payload
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDecisionTrail:
    """Verifica DecisionTrail: campos, imutabilidade e payload Firehose."""

    def test_fields(self, decision_trail):
        assert decision_trail.external_id == "ext_txn_001"
        assert decision_trail.tenant_id == "tenantA"
        assert decision_trail.final_verdict == FinalVerdict.REJECTED
        assert decision_trail.matched_deny_rule == "deny_test"
        assert len(decision_trail.rules) == 2
        assert decision_trail.error_code is None
        assert decision_trail.timestamp == "2026-03-11T00:00:00Z"

    def test_rules_is_tuple(self, decision_trail):
        assert isinstance(decision_trail.rules, tuple)

    def test_immutability(self, decision_trail):
        with pytest.raises(Exception):
            decision_trail.final_verdict = FinalVerdict.APPROVED  # type: ignore[misc]

    def test_to_firehose_payload_structure(self, decision_trail):
        """to_firehose_payload deve retornar um dicionário plano."""
        payload = decision_trail.to_firehose_payload()
        assert isinstance(payload, dict)

    def test_to_firehose_payload_contains_all_required_fields(self, decision_trail):
        """O payload deve conter todos os campos do Requisito 13.2."""
        payload = decision_trail.to_firehose_payload()
        required_fields = [
            "external_id",
            "tenant_id",
            "policy_scope_id",
            "activation_id",
            "artifact_hash",
            "snapshot_version",
            "evaluator_version",
            "input_hash",
            "final_verdict",
            "matched_deny_rule",
            "rules",
            "evaluation_latency_ms",
            "error_code",
            "timestamp",
        ]
        for field_name in required_fields:
            assert field_name in payload, f"Campo ausente no payload Firehose: {field_name}"

    def test_to_firehose_payload_final_verdict_is_string(self, decision_trail):
        """final_verdict no payload deve ser string para compatibilidade com Parquet."""
        payload = decision_trail.to_firehose_payload()
        assert isinstance(payload["final_verdict"], str)
        assert payload["final_verdict"] == "REJECTED"

    def test_to_firehose_payload_rules_is_list_of_dicts(self, decision_trail):
        """rules no payload deve ser lista de dicionários para serialização JSON."""
        payload = decision_trail.to_firehose_payload()
        assert isinstance(payload["rules"], list)
        for rule_dict in payload["rules"]:
            assert isinstance(rule_dict, dict)
            assert "rule_name" in rule_dict
            assert "effect" in rule_dict
            assert "matched" in rule_dict
            assert "priority" in rule_dict
            assert "message" in rule_dict

    def test_to_firehose_payload_rule_effect_is_string(self, decision_trail):
        """effect de cada rule no payload deve ser string."""
        payload = decision_trail.to_firehose_payload()
        for rule_dict in payload["rules"]:
            assert isinstance(rule_dict["effect"], str)

    def test_to_firehose_payload_approved_trail(self, rule_match_allow):
        """Trail de aprovação deve ter matched_deny_rule=None e error_code=None."""
        trail = DecisionTrail(
            external_id="ext_001",
            tenant_id="tenantA",
            policy_scope_id="tenantA:TRANSFER:*:*:prod",
            activation_id="act_001",
            artifact_hash="sha256:abc",
            snapshot_version="snap_001",
            evaluator_version="1.0.0",
            input_hash="sha256:input",
            final_verdict=FinalVerdict.APPROVED,
            matched_deny_rule=None,
            rules=(rule_match_allow,),
            evaluation_latency_ms=2.0,
            error_code=None,
            timestamp="2026-03-11T00:00:00Z",
        )
        payload = trail.to_firehose_payload()
        assert payload["final_verdict"] == "APPROVED"
        assert payload["matched_deny_rule"] is None
        assert payload["error_code"] is None

    def test_to_firehose_payload_with_error_code(self, rule_match_deny):
        """Trail com erro interno deve incluir error_code no payload."""
        trail = DecisionTrail(
            external_id="ext_002",
            tenant_id="tenantA",
            policy_scope_id="tenantA:TRANSFER:*:*:prod",
            activation_id="act_001",
            artifact_hash="sha256:abc",
            snapshot_version="snap_001",
            evaluator_version="1.0.0",
            input_hash="sha256:input",
            final_verdict=FinalVerdict.REJECTED,
            matched_deny_rule=None,
            rules=(rule_match_deny,),
            evaluation_latency_ms=1.0,
            error_code="POLICY_EVALUATION_ERROR",
            timestamp="2026-03-11T00:00:00Z",
        )
        payload = trail.to_firehose_payload()
        assert payload["error_code"] == "POLICY_EVALUATION_ERROR"

    def test_to_firehose_payload_is_json_serializable(self, decision_trail):
        """O payload deve ser serializável para JSON sem erros."""
        payload = decision_trail.to_firehose_payload()
        json_str = json.dumps(payload)
        assert isinstance(json_str, str)
        assert len(json_str) > 0

    def test_equality_by_value(self, decision_trail, rule_match_deny, rule_match_allow):
        other = DecisionTrail(
            external_id="ext_txn_001",
            tenant_id="tenantA",
            policy_scope_id="tenantA:TRANSFER:PIX:*:prod",
            activation_id="act_001",
            artifact_hash="sha256:deadbeef",
            snapshot_version="snap_001",
            evaluator_version="1.0.0",
            input_hash="sha256:inputhash",
            final_verdict=FinalVerdict.REJECTED,
            matched_deny_rule="deny_test",
            rules=(rule_match_deny, rule_match_allow),
            evaluation_latency_ms=4.2,
            error_code=None,
            timestamp="2026-03-11T00:00:00Z",
        )
        assert decision_trail == other
