"""
Testes de integração AWS dev — evaluator slice.

Carrega bundle e snapshot reais do S3 dev e avalia localmente com o RuleEvaluator.
Usa recursos AWS REAIS (S3 real com KMS). NÃO usa moto ou qualquer mock.

Pré-requisitos:
    - VALIDATION_ENGINE_TEST_BUCKET: bucket S3 dedicado para testes
    - VALIDATION_ENGINE_TEST_KMS_KEY_ARN: ARN da chave KMS (opcional)
    - AWS_REGION: região AWS (padrão: us-east-1)
    - Credenciais AWS válidas com permissão de leitura/escrita no bucket

Estratégia de isolamento:
    O run_id (UUID único por sessão) é embutido no policy_set_id dos bundles
    e no snapshot_version dos snapshots. Isso garante que testes de sessões
    diferentes não colidem entre si.

Cleanup:
    Os objetos S3 criados são deletados ao final do módulo via fixture de cleanup.
    O cleanup é best-effort: falhas são logadas mas não falham os testes.

Requisitos cobertos: 9.1, 9.3, 11.2
"""
from __future__ import annotations

import logging
import os
from collections.abc import Generator

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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTEXT_SCHEMA_VERSION = "1.0"
KMS_KEY_ARN_ENV_VAR = "VALIDATION_ENGINE_TEST_KMS_KEY_ARN"

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
        tenant_id="tenant_aws_dev_test",
        external_id="ext_aws_evaluator_slice_001",
        operation_type="TRANSFER",
        product_code="PIX",
        channel="MOBILE",
        postings=tuple(postings),
        policy_context=policy_context or {},
        facts=facts,
        context_schema_version=CONTEXT_SCHEMA_VERSION,
    )


def _build_active_policy_set(bundle, snapshot: ReferenceSnapshot) -> ActivePolicySet:
    """Constrói um ActivePolicySet a partir de bundle e snapshot carregados do S3 real."""
    manifest = PolicyActivationManifest(
        activation_id="act_aws_evaluator_slice_001",
        policy_scope_id="tenant_aws_dev_test:TRANSFER:PIX:MOBILE:dev",
        artifact_hash=bundle.artifact_hash,
        snapshot_version=snapshot.snapshot_version,
        context_schema_version=CONTEXT_SCHEMA_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        activated_at="2024-01-01T00:00:00Z",
        activated_by="aws-dev-integration-test",
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


@pytest.fixture(scope="module")
def kms_key_id() -> str:
    """ARN da chave KMS para testes AWS dev. Usa placeholder se não definido."""
    return os.environ.get(
        KMS_KEY_ARN_ENV_VAR,
        "arn:aws:kms:us-east-1:123456789012:key/test-key-placeholder",
    )


@pytest.fixture(scope="module")
def aws_bundle_store(aws_dev_s3_client, aws_dev_config, kms_key_id) -> BundleStore:
    return BundleStore(
        s3_client=aws_dev_s3_client,
        bucket_name=aws_dev_config.bucket,
        kms_key_id=kms_key_id,
    )


@pytest.fixture(scope="module")
def aws_bundle_loader(aws_dev_s3_client, aws_dev_config) -> BundleLoader:
    return BundleLoader(
        s3_client=aws_dev_s3_client,
        bucket_name=aws_dev_config.bucket,
        current_context_schema_version=CONTEXT_SCHEMA_VERSION,
        current_evaluator_version=EVALUATOR_VERSION,
    )


@pytest.fixture(scope="module")
def aws_snapshot_store(aws_dev_s3_client, aws_dev_config, kms_key_id) -> SnapshotStore:
    return SnapshotStore(
        s3_client=aws_dev_s3_client,
        bucket_name=aws_dev_config.bucket,
        kms_key_id=kms_key_id,
    )


@pytest.fixture(scope="module")
def aws_snapshot_loader(aws_dev_s3_client, aws_dev_config) -> SnapshotLoader:
    return SnapshotLoader(
        s3_client=aws_dev_s3_client,
        bucket_name=aws_dev_config.bucket,
        expected_snapshot_schema_version="1.0",
    )


@pytest.fixture(scope="module")
def aws_compiled_bundle(aws_dev_config):
    """Bundle compilado para testes AWS dev com run_id único."""
    compiler = DSLCompiler.create_default()
    metadata = CompilationMetadata(
        author="aws-dev-evaluator-slice-test",
        description=f"AWS dev evaluator slice test — run {aws_dev_config.run_id}",
        compiled_at="2024-01-01T00:00:00Z",
        source_hash="sha256:aws_dev_evaluator_slice",
    )
    compatibility = BundleCompatibility(
        dsl_version="1.0",
        context_schema_version=CONTEXT_SCHEMA_VERSION,
        snapshot_schema_version="1.0",
        evaluator_min_version=EVALUATOR_VERSION,
    )
    return compiler.compile(
        dsl_source=_EVALUATOR_SLICE_DSL,
        policy_set_id=f"aws-dev-evaluator-slice-{aws_dev_config.run_id}",
        metadata=metadata,
        compatibility=compatibility,
    )


@pytest.fixture(scope="module")
def aws_snapshot(aws_dev_config) -> ReferenceSnapshot:
    """Snapshot de referência com run_id único para isolamento."""
    return ReferenceSnapshot(
        snapshot_version=f"snap-aws-evaluator-slice-{aws_dev_config.run_id}",
        snapshot_schema_version="1.0",
        created_at="2024-01-01T00:00:00Z",
        data={
            "daily_limit_minor": 500_000,   # R$ 5.000,00
            "blocked_accounts": ("blocked_acc_aws_001", "blocked_acc_aws_002"),
        },
    )


@pytest.fixture(scope="module")
def aws_active_policy_set(
    aws_bundle_store,
    aws_bundle_loader,
    aws_snapshot_store,
    aws_snapshot_loader,
    aws_compiled_bundle,
    aws_snapshot,
    aws_dev_s3_client,
    aws_dev_config,
) -> Generator[ActivePolicySet, None, None]:
    """
    ActivePolicySet construído a partir do pipeline completo com S3 real:
    compile → store no S3 real → load do S3 real → build ActivePolicySet.

    Realiza cleanup dos objetos S3 ao final do módulo.
    """
    # Store bundle e snapshot no S3 real
    aws_bundle_store.store(aws_compiled_bundle)
    aws_snapshot_store.store(aws_snapshot)

    # Load bundle e snapshot do S3 real (com verificação de integridade)
    loaded_bundle = aws_bundle_loader.load(aws_compiled_bundle.artifact_hash)
    loaded_snapshot = aws_snapshot_loader.load(aws_snapshot.snapshot_version)

    aps = _build_active_policy_set(loaded_bundle, loaded_snapshot)

    yield aps

    # Cleanup: remove objetos de teste do S3 real
    bundle_key = f"bundles/{aws_compiled_bundle.artifact_hash}.json"
    snapshot_key = f"snapshots/{aws_snapshot.snapshot_version}.json"

    for key in [bundle_key, snapshot_key]:
        try:
            aws_dev_s3_client.delete_object(Bucket=aws_dev_config.bucket, Key=key)
            logger.info("Cleanup: objeto removido do S3", extra={"key": key})
        except Exception as exc:
            logger.warning("Cleanup: falha ao remover objeto", extra={"key": key, "error": str(exc)})


@pytest.fixture(scope="module")
def evaluator() -> RuleEvaluator:
    return RuleEvaluator()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
class TestAWSEvaluatorSlice:
    """
    Testa o slice de avaliação com bundle e snapshot carregados do S3 real.

    Pipeline: compile → store no S3 real → load do S3 real → evaluate localmente.
    """

    def test_active_policy_set_loaded_from_real_s3(
        self, aws_active_policy_set: ActivePolicySet
    ) -> None:
        """ActivePolicySet deve ser carregado com integridade verificada."""
        assert aws_active_policy_set.integrity_verified is True
        assert aws_active_policy_set.bundle is not None
        assert aws_active_policy_set.snapshot is not None

    def test_bundle_loaded_from_real_s3_has_correct_rules(
        self, aws_active_policy_set: ActivePolicySet
    ) -> None:
        """Bundle carregado do S3 real deve ter as 3 rules compiladas."""
        rule_names = {r.name for r in aws_active_policy_set.bundle.ast.rules}
        assert "deny_over_daily_limit" in rule_names
        assert "deny_blocked_account" in rule_names
        assert "allow_standard_brl" in rule_names

    def test_snapshot_loaded_from_real_s3_has_correct_data(
        self, aws_active_policy_set: ActivePolicySet
    ) -> None:
        """Snapshot carregado do S3 real deve ter os dados de referência corretos."""
        snapshot = aws_active_policy_set.snapshot
        assert snapshot.lookup(("daily_limit_minor",)) == 500_000
        blocked = snapshot.lookup(("blocked_accounts",))
        assert "blocked_acc_aws_001" in blocked

    def test_approve_transaction_within_limit_with_real_s3_bundle(
        self, evaluator: RuleEvaluator, aws_active_policy_set: ActivePolicySet
    ) -> None:
        """Transação dentro do limite com bundle do S3 real → APPROVED."""
        postings = [
            CanonicalPosting(account_id="acc_debit", amount=100_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=100_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        result = evaluator.evaluate(context, aws_active_policy_set)

        assert result.decision.final_verdict == FinalVerdict.APPROVED
        assert result.decision.matched_deny_rule is None

    def test_reject_transaction_over_limit_with_real_s3_bundle(
        self, evaluator: RuleEvaluator, aws_active_policy_set: ActivePolicySet
    ) -> None:
        """Transação acima do limite com bundle do S3 real → REJECTED."""
        postings = [
            CanonicalPosting(account_id="acc_debit", amount=600_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=600_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        result = evaluator.evaluate(context, aws_active_policy_set)

        assert result.decision.final_verdict == FinalVerdict.REJECTED
        assert result.decision.matched_deny_rule == "deny_over_daily_limit"

    def test_reject_blocked_account_with_real_s3_snapshot(
        self, evaluator: RuleEvaluator, aws_active_policy_set: ActivePolicySet
    ) -> None:
        """Conta bloqueada no snapshot real → REJECTED."""
        postings = [
            CanonicalPosting(account_id="blocked_acc_aws_001", amount=10_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=10_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        result = evaluator.evaluate(context, aws_active_policy_set)

        assert result.decision.final_verdict == FinalVerdict.REJECTED
        assert result.decision.matched_deny_rule == "deny_blocked_account"

    def test_evaluation_is_deterministic_with_real_s3_bundle(
        self, evaluator: RuleEvaluator, aws_active_policy_set: ActivePolicySet
    ) -> None:
        """Avaliações repetidas com bundle do S3 real produzem o mesmo veredito."""
        postings = [
            CanonicalPosting(account_id="acc_debit", amount=200_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=200_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        results = [evaluator.evaluate(context, aws_active_policy_set) for _ in range(3)]
        verdicts = [r.decision.final_verdict for r in results]

        # Determinismo: todos os vereditos devem ser iguais (Requisito 9.3)
        assert len(set(verdicts)) == 1

    def test_all_rules_evaluated_with_real_s3_bundle(
        self, evaluator: RuleEvaluator, aws_active_policy_set: ActivePolicySet
    ) -> None:
        """Todas as 3 rules do bundle real são avaliadas e registradas."""
        postings = [
            CanonicalPosting(account_id="acc_debit", amount=10_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=10_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        result = evaluator.evaluate(context, aws_active_policy_set)

        assert result.metrics.evaluated_rules == 3

    def test_evaluation_latency_is_within_budget_with_real_s3_bundle(
        self, evaluator: RuleEvaluator, aws_active_policy_set: ActivePolicySet
    ) -> None:
        """
        Latência de avaliação em memória deve ser inferior a 15ms no p95.

        Requisito 19.1: avaliação do RuleEvaluator < 15ms no p95.
        Este teste valida o budget de latência com bundle real do S3.
        """
        postings = [
            CanonicalPosting(account_id="acc_debit", amount=100_000, currency="BRL", direction="DEBIT"),
            CanonicalPosting(account_id="acc_credit", amount=100_000, currency="BRL", direction="CREDIT"),
        ]
        context = _make_context(postings)

        # Executa 20 avaliações para estimar latência
        latencies = [
            evaluator.evaluate(context, aws_active_policy_set).metrics.evaluation_latency_ms
            for _ in range(20)
        ]

        # Ordena e pega o p95 (índice 18 de 20 amostras)
        latencies.sort()
        p95_latency = latencies[int(len(latencies) * 0.95)]

        logger.info(
            "Latência de avaliação com bundle real do S3",
            extra={
                "p50_ms": latencies[len(latencies) // 2],
                "p95_ms": p95_latency,
                "max_ms": latencies[-1],
            },
        )

        # Budget: < 15ms no p95 (Requisito 19.1)
        assert p95_latency < 15.0, (
            f"Latência p95 ({p95_latency:.2f}ms) excede o budget de 15ms. "
            "Verifique se o bundle não está muito grande ou se há overhead inesperado."
        )
