"""
Integração local: evaluator slice completo.

Exercita o pipeline completo:
  compile DSL → store bundle → store snapshot → load bundle → load snapshot
  → build ActivePolicySet → evaluate com RuleEvaluator

Usa S3 mockado via moto. Não requer AWS real.

Cobre:
- Bundle compilado + snapshot + contexto → avaliação real com RuleEvaluator
- Veredito APPROVED e REJECTED com bundle real
- Semântica DENY_OVERRIDES com bundle compilado
- Integridade verificada antes da avaliação

Requisitos cobertos: 9.1, 9.3, 10.3
"""
from __future__ import annotations

import pytest

from validation_engine.domain.compiler import DSLCompiler
from validation_engine.domain.context import (
    CanonicalPosting,
    CanonicalValidationContext,
    DerivedFacts,
)
from validation_engine.domain.evaluator import EVALUATOR_VERSION, RuleEvaluator
from validation_engine.domain.models import (
    ActivePolicySet,
    BundleCompatibility,
    CompilationMetadata,
    PolicyActivationManifest,
    ReferenceSnapshot,
)
from validation_engine.domain.policy_ast import FinalVerdict
from validation_engine.infrastructure.bundle_loader import BundleLoader
from validation_engine.infrastructure.bundle_store import BundleStore
from validation_engine.infrastructure.snapshot_loader import SnapshotLoader
from validation_engine.infrastructure.snapshot_store import SnapshotStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_KMS_KEY_ID = "arn:aws:kms:us-east-1:123456789012:key/test-key-id"
_CONTEXT_SCHEMA_VERSION = "1.0"
_EVALUATOR_VERSION = EVALUATOR_VERSION

_DEFAULT_COMPAT = BundleCompatibility(
    dsl_version="1.0",
    context_schema_version=_CONTEXT_SCHEMA_VERSION,
    snapshot_schema_version="1.0",
    evaluator_min_version=_EVALUATOR_VERSION,
)

_DEFAULT_META = CompilationMetadata(
    author="integration-test",
    description="Evaluator slice integration test",
    compiled_at="2024-01-01T00:00:00Z",
    source_hash="sha256:evaluator_slice_test",
)

# DSL com regras reais para o slice de avaliação
_EVALUATOR_SLICE_DSL = """
POLICY deny_over_daily_limit PRIORITY 100
  WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > ref.daily_limit_minor
  THEN DENY "Transaction exceeds daily debit limit"

POLICY deny_blocked_account PRIORITY 90
  WHEN ANY(postings WHERE account_id IN ref.blocked_accounts)
  THEN DENY "Blocked account detected"

POLICY allow_standard_brl PRIORITY 10
  WHEN facts.posting_count >= 2
    AND COUNT(postings WHERE currency == "BRL") == facts.posting_count
  THEN ALLOW "Standard BRL flow"
"""

# Snapshot com dados de referência para os testes
_SNAPSHOT = ReferenceSnapshot(
    snapshot_version="snap_evaluator_slice_001",
    snapshot_schema_version="1.0",
    created_at="2024-01-01T00:00:00Z",
    data={
        "daily_limit_minor": 500_000,   # R$ 5.000,00
        "blocked_accounts": ("blocked_acc_001", "blocked_acc_002"),
    },
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(
    postings: list[CanonicalPosting],
    policy_context: dict | None = None,
) -> CanonicalValidationContext:
    """Constrói um contexto canônico a partir de uma lista de postings."""
    debit_total = sum(p.amount for p in postings if p.direction == "DEBIT")
    credit_total = sum(p.amount for p in postings if p.direction == "CREDIT")
    currencies = tuple(sorted({p.currency for p in postings}))
    distinct_accounts = len({p.account_id for p in postings})
    max_amount = max(p.amount for p in postings)

    facts = DerivedFacts(
        posting_count=len(postings),
        distinct_account_count=distinct_accounts,
        currencies=currencies,
        total_debits_by_currency={"BRL": debit_total} if debit_total else {},
        total_credits_by_currency={"BRL": credit_total} if credit_total else {},
        max_posting_amount=max_amount,
        has_platform_account=False,
    )
    return CanonicalValidationContext(
        tenant_id="tenant_integration_test",
        external_id="ext_evaluator_slice_001",
        operation_type="TRANSFER",
        product_code="PIX",
        channel="MOBILE",
        postings=tuple(postings),
        policy_context=policy_context or {},
        facts=facts,
        context_schema_version=_CONTEXT_SCHEMA_VERSION,
    )


def _build_active_policy_set(bundle, snapshot: ReferenceSnapshot) -> ActivePolicySet:
    """Constrói um ActivePolicySet a partir de bundle e snapshot carregados."""
    manifest = PolicyActivationManifest(
        activation_id="act_evaluator_slice_001",
        policy_scope_id="tenant_integration_test:TRANSFER:PIX:MOBILE:prod",
        artifact_hash=bundle.artifact_hash,
        snapshot_version=snapshot.snapshot_version,
        context_schema_version=_CONTEXT_SCHEMA_VERSION,
        evaluator_version=_EVALUATOR_VERSION,
        activated_at="2024-01-01T00:00:00Z",
        activated_by="integration-test",
    )
    return ActivePolicySet(
        manifest=manifest,
        bundle=bundle,
        snapshot=snapshot,
        loaded_at="2024-01-01T00:00:00Z",
        integrity_verified=True,  # Verificado pelo BundleLoader
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bundle_store(moto_s3_client, local_s3_bucket):
    return BundleStore(
        s3_client=moto_s3_client,
        bucket_name=local_s3_bucket,
        kms_key_id=_FAKE_KMS_KEY_ID,
    )


@pytest.fixture
def bundle_loader(moto_s3_client, local_s3_bucket):
    return BundleLoader(
        s3_client=moto_s3_client,
        bucket_name=local_s3_bucket,
        current_context_schema_version=_CONTEXT_SCHEMA_VERSION,
        current_evaluator_version=_EVALUATOR_VERSION,
    )


@pytest.fixture
def snapshot_store(moto_s3_client, local_s3_bucket):
    return SnapshotStore(
        s3_client=moto_s3_client,
        bucket_name=local_s3_bucket,
        kms_key_id=_FAKE_KMS_KEY_ID,
    )


@pytest.fixture
def snapshot_loader(moto_s3_client, local_s3_bucket):
    return SnapshotLoader(
        s3_client=moto_s3_client,
        bucket_name=local_s3_bucket,
        expected_snapshot_schema_version="1.0",
    )


@pytest.fixture
def compiled_bundle():
    """Bundle compilado a partir do DSL de avaliação."""
    compiler = DSLCompiler.create_default()
    return compiler.compile(
        dsl_source=_EVALUATOR_SLICE_DSL,
        policy_set_id="evaluator_slice_bundle",
        metadata=_DEFAULT_META,
        compatibility=_DEFAULT_COMPAT,
    )


@pytest.fixture
def active_policy_set(bundle_store, bundle_loader, snapshot_store, snapshot_loader, compiled_bundle):
    """
    ActivePolicySet construído a partir do pipeline completo:
    compile → store → load → build ActivePolicySet.
    """
    # Store bundle e snapshot
    bundle_store.store(compiled_bundle)
    snapshot_store.store(_SNAPSHOT)

    # Load bundle e snapshot (com verificação de integridade)
    loaded_bundle = bundle_loader.load(compiled_bundle.artifact_hash)
    loaded_snapshot = snapshot_loader.load(_SNAPSHOT.snapshot_version)

    return _build_active_policy_set(loaded_bundle, loaded_snapshot)


@pytest.fixture
def evaluator():
    return RuleEvaluator()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestLocalEvaluatorSlice:
    """
    Testa o slice completo de avaliação com bundle compilado e snapshot reais.

    Pipeline: compile DSL → store → load → build ActivePolicySet → evaluate
    """

    def test_approve_transaction_within_daily_limit(
        self, evaluator: RuleEvaluator, active_policy_set: ActivePolicySet
    ) -> None:
        """Transação dentro do limite diário deve ser APPROVED."""
        postings = [
            CanonicalPosting(account_id="acc_debit", amount=100_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=100_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        result = evaluator.evaluate(context, active_policy_set)

        assert result.decision.final_verdict == FinalVerdict.APPROVED
        assert result.decision.matched_deny_rule is None

    def test_reject_transaction_over_daily_limit(
        self, evaluator: RuleEvaluator, active_policy_set: ActivePolicySet
    ) -> None:
        """Transação acima do limite diário (R$ 5.000,00) deve ser REJECTED."""
        postings = [
            # 600.000 minor units = R$ 6.000,00 > limite de R$ 5.000,00
            CanonicalPosting(account_id="acc_debit", amount=600_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=600_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        result = evaluator.evaluate(context, active_policy_set)

        assert result.decision.final_verdict == FinalVerdict.REJECTED
        assert result.decision.matched_deny_rule == "deny_over_daily_limit"

    def test_reject_transaction_with_blocked_account(
        self, evaluator: RuleEvaluator, active_policy_set: ActivePolicySet
    ) -> None:
        """Transação com conta bloqueada deve ser REJECTED."""
        postings = [
            # blocked_acc_001 está na lista de bloqueio do snapshot
            CanonicalPosting(account_id="blocked_acc_001", amount=10_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=10_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        result = evaluator.evaluate(context, active_policy_set)

        assert result.decision.final_verdict == FinalVerdict.REJECTED
        assert result.decision.matched_deny_rule == "deny_blocked_account"

    def test_deny_overrides_allow_in_compiled_bundle(
        self, evaluator: RuleEvaluator, active_policy_set: ActivePolicySet
    ) -> None:
        """
        DENY_OVERRIDES: mesmo que allow_standard_brl case, um DENY prevalece.

        Transação com conta bloqueada E dentro do limite → REJECTED (DENY prevalece).
        """
        postings = [
            # Conta bloqueada mas dentro do limite → DENY prevalece sobre ALLOW
            CanonicalPosting(account_id="blocked_acc_002", amount=50_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=50_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        result = evaluator.evaluate(context, active_policy_set)

        # DENY (blocked account) prevalece sobre ALLOW (standard BRL)
        assert result.decision.final_verdict == FinalVerdict.REJECTED

    def test_all_rules_evaluated_in_compiled_bundle(
        self, evaluator: RuleEvaluator, active_policy_set: ActivePolicySet
    ) -> None:
        """Todas as 3 rules do bundle compilado são avaliadas e registradas."""
        postings = [
            CanonicalPosting(account_id="acc_debit", amount=10_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=10_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        result = evaluator.evaluate(context, active_policy_set)

        # O bundle tem 3 rules
        assert result.metrics.evaluated_rules == 3
        rule_names = {r.rule_name for r in result.decision.rules}
        assert "deny_over_daily_limit" in rule_names
        assert "deny_blocked_account" in rule_names
        assert "allow_standard_brl" in rule_names

    def test_evaluation_latency_is_collected(
        self, evaluator: RuleEvaluator, active_policy_set: ActivePolicySet
    ) -> None:
        """Latência de avaliação é coletada e é positiva."""
        postings = [
            CanonicalPosting(account_id="acc_debit", amount=10_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=10_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        result = evaluator.evaluate(context, active_policy_set)

        assert result.metrics.evaluation_latency_ms >= 0.0

    def test_loaded_bundle_produces_same_verdict_as_original(
        self,
        evaluator: RuleEvaluator,
        compiled_bundle,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        Bundle carregado do S3 produz o mesmo veredito que o bundle original.

        Garante que a serialização/deserialização não altera o comportamento
        semântico do evaluator.
        """
        postings = [
            CanonicalPosting(account_id="acc_debit", amount=100_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=100_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        # Avalia com o bundle carregado do S3 (via active_policy_set)
        result_loaded = evaluator.evaluate(context, active_policy_set)

        # Constrói ActivePolicySet diretamente com o bundle compilado (sem S3)
        aps_direct = _build_active_policy_set(compiled_bundle, _SNAPSHOT)
        result_direct = evaluator.evaluate(context, aps_direct)

        # Ambos devem produzir a mesma decisão semântica
        assert result_loaded.decision == result_direct.decision

    def test_evaluation_is_deterministic_with_compiled_bundle(
        self, evaluator: RuleEvaluator, active_policy_set: ActivePolicySet
    ) -> None:
        """Avaliações repetidas com o mesmo bundle e contexto produzem o mesmo veredito."""
        postings = [
            CanonicalPosting(account_id="acc_debit", amount=200_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=200_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        results = [evaluator.evaluate(context, active_policy_set) for _ in range(5)]
        verdicts = [r.decision.final_verdict for r in results]

        # Todos os vereditos devem ser iguais
        assert len(set(verdicts)) == 1
