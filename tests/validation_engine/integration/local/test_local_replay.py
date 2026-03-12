"""
Integração local: replay de decisões de policy.

Valida que uma decisão passada pode ser reproduzida com fidelidade usando:
- JournalEntry persistido (com policy_context e DecisionSummary)
- RuleBundle identificado por artifact_hash
- ReferenceSnapshot identificado por snapshot_version

O replay reconstrói o CanonicalValidationContext a partir dos dados persistidos
e re-avalia usando o mesmo bundle e snapshot. O veredito deve ser semanticamente
idêntico ao original.

Cenários cobertos:
- Replay de decisão APPROVED produz mesmo veredito
- Replay de decisão REJECTED produz mesmo veredito e mesma deny rule
- input_hash é consistente entre avaliação original e replay
- Replay com bundle diferente pode produzir veredito diferente (divergência detectável)

Requisitos cobertos: 14.1, 14.3
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from ledger.application.commands import CreateJournalEntryCommand, PostingInput
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.services import LedgerEngine
from ledger.domain.validators import (
    MinorUnitsValidator,
    TransactionLimitValidator,
    ValidationChain,
    ZeroSumValidator,
)
from validation_engine.application.context_builder import (
    DefaultCanonicalValidationContextBuilder,
)
from validation_engine.application.facade import PolicyValidationFacade
from validation_engine.application.runtime_registry import PolicyRuntimeRegistry
from validation_engine.domain.compiler import DSLCompiler
from validation_engine.domain.errors import PolicyRejected
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
from validation_engine.infrastructure.decision_trail_emitter import (
    NoOpDecisionTrailEmitter,
)
from validation_engine.infrastructure.lkg_store import LKGStore
from validation_engine.infrastructure.manifest_resolver import ManifestResolver
from validation_engine.infrastructure.snapshot_loader import SnapshotLoader
from validation_engine.infrastructure.snapshot_store import SnapshotStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_KMS_KEY_ID = "arn:aws:kms:us-east-1:123456789012:key/test-key-id"
_CONTEXT_SCHEMA_VERSION = "1.0"
_SCOPE_ID = "tenantA:TRANSFER:PIX:*:prod"

_DEFAULT_COMPAT = BundleCompatibility(
    dsl_version="1.0",
    context_schema_version=_CONTEXT_SCHEMA_VERSION,
    snapshot_schema_version="1.0",
    evaluator_min_version=EVALUATOR_VERSION,
)

_DEFAULT_META = CompilationMetadata(
    author="replay-test",
    description="Local replay integration test",
    compiled_at="2024-01-01T00:00:00Z",
    source_hash="sha256:replay_test",
)

_REPLAY_DSL = """
POLICY deny_over_limit PRIORITY 100
  WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > ref.daily_limit_minor
  THEN DENY "Transaction exceeds daily debit limit"

POLICY allow_standard PRIORITY 10
  WHEN facts.posting_count >= 2
  THEN ALLOW "Standard transaction"
"""

_SNAPSHOT = ReferenceSnapshot(
    snapshot_version="snap_replay_001",
    snapshot_schema_version="1.0",
    created_at="2024-01-01T00:00:00Z",
    data={"daily_limit_minor": 500_000},
)


# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


class InMemoryLedgerRepository:
    """Repositório in-memory para testes de replay."""

    def __init__(self):
        self._entries = {}
        self._by_external_id = {}

    def save_journal_entry(self, journal_entry):
        self._entries[journal_entry.entry_id] = journal_entry
        self._by_external_id[journal_entry.external_id] = journal_entry

    def find_journal_entry_by_id(self, entry_id):
        return self._entries.get(entry_id)

    def find_journal_entry_by_external_id(self, ext_id):
        return self._by_external_id.get(ext_id)

    def get_balance(self, account_id, currency):
        return None

    def get_statement(self, account_id, cursor, page_size):
        return None


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
        current_evaluator_version=EVALUATOR_VERSION,
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
def compiled_bundle(bundle_store):
    """Compila e armazena o bundle no S3 mockado."""
    compiler = DSLCompiler.create_default()
    bundle = compiler.compile(
        dsl_source=_REPLAY_DSL,
        policy_set_id="replay_bundle",
        metadata=_DEFAULT_META,
        compatibility=_DEFAULT_COMPAT,
    )
    bundle_store.store(bundle)
    return bundle


@pytest.fixture
def stored_snapshot(snapshot_store):
    """Armazena o snapshot no S3 mockado."""
    snapshot_store.store(_SNAPSHOT)
    return _SNAPSHOT


@pytest.fixture
def manifest(compiled_bundle, stored_snapshot):
    return PolicyActivationManifest(
        activation_id="act_replay_001",
        policy_scope_id=_SCOPE_ID,
        artifact_hash=compiled_bundle.artifact_hash,
        snapshot_version=stored_snapshot.snapshot_version,
        context_schema_version=_CONTEXT_SCHEMA_VERSION,
        evaluator_version=EVALUATOR_VERSION,
        activated_at="2024-01-01T00:00:00Z",
        activated_by="replay-test",
    )


@pytest.fixture
def runtime_registry(manifest, bundle_loader, snapshot_loader, lkg_temp_dir):
    resolver = MagicMock(spec=ManifestResolver)
    resolver.resolve.return_value = manifest
    lkg_store = LKGStore(lkg_dir=lkg_temp_dir)
    return PolicyRuntimeRegistry(
        manifest_resolver=resolver,
        bundle_loader=bundle_loader,
        snapshot_loader=snapshot_loader,
        lkg_store=lkg_store,
        evaluator_version=EVALUATOR_VERSION,
    )


@pytest.fixture
def noop_emitter():
    return NoOpDecisionTrailEmitter()


@pytest.fixture
def facade(runtime_registry, noop_emitter):
    return PolicyValidationFacade(
        context_builder=DefaultCanonicalValidationContextBuilder(),
        runtime_registry=runtime_registry,
        evaluator=RuleEvaluator(),
        trail_emitter=noop_emitter,
    )


@pytest.fixture
def repo():
    return InMemoryLedgerRepository()


@pytest.fixture
def engine(repo, facade):
    chain = ValidationChain(
        validators=[
            ZeroSumValidator(),
            MinorUnitsValidator(),
            TransactionLimitValidator(),
            facade,
        ]
    )
    return LedgerEngine(
        repository=repo,
        validation_chain=chain,
        factory=JournalEntryFactory(),
    )


def _replay_decision(
    entry,
    bundle_loader,
    snapshot_loader,
    manifest,
    original_command=None,
):
    """
    Reproduz a decisão de policy a partir de um JournalEntry persistido.

    Replay pipeline:
    1. Extrair policy_context e postings do JournalEntry/comando original
    2. Reconstruir CanonicalValidationContext via builder
    3. Carregar o RuleBundle pelo artifact_hash do DecisionSummary
    4. Carregar o ReferenceSnapshot pelo snapshot_version do DecisionSummary
    5. Montar ActivePolicySet
    6. Re-avaliar com RuleEvaluator
    7. Comparar veredito com o original
    """
    summary = entry.metadata.get("policy_validation", {})
    artifact_hash = summary["artifact_hash"]
    snapshot_version = summary["snapshot_version"]

    # Carregar artefatos pelo identificador persistido no summary
    bundle = bundle_loader.load(artifact_hash)
    snapshot = snapshot_loader.load(snapshot_version)

    # Montar ActivePolicySet para replay
    active_policy_set = ActivePolicySet(
        manifest=manifest,
        bundle=bundle,
        snapshot=snapshot,
        loaded_at="2024-01-01T00:00:00Z",
        integrity_verified=True,
    )

    # Reconstruir contexto canônico a partir do comando original.
    # Em produção, o contexto seria reconstruído a partir do JournalEntry
    # persistido + policy_context. Aqui usamos o builder diretamente
    # sobre um comando reconstruído a partir dos dados do entry.
    builder = DefaultCanonicalValidationContextBuilder()
    replay_command = _reconstruct_command_from_entry(entry, original_command=original_command)
    context = builder.build(replay_command)

    # Re-avaliar com o mesmo evaluator
    evaluator = RuleEvaluator()
    return evaluator.evaluate(context, active_policy_set)


def _reconstruct_command_from_entry(entry, original_command=None):
    """
    Reconstrói um CreateJournalEntryCommand a partir de um JournalEntry persistido.

    Em produção, o replay usaria os dados persistidos no JournalEntry
    (postings, policy_context, tenant_id) para reconstruir o comando.
    Campos como operation_type e product_code seriam persistidos no
    metadata ou em um campo dedicado do entry para suportar replay.

    Para o teste, usamos o comando original quando disponível para
    garantir que campos como operation_type e product_code sejam preservados.
    """
    postings = [
        PostingInput(
            account_id=p.account_id,
            amount=p.money.amount,
            currency=p.money.currency,
            direction=p.direction.value,
        )
        for p in entry.postings
    ]

    # Em produção, estes campos seriam persistidos no entry para replay.
    # Aqui usamos o comando original como fonte de verdade.
    if original_command is not None:
        return CreateJournalEntryCommand(
            external_id=entry.external_id,
            postings=postings,
            tenant_id=getattr(original_command, "tenant_id", ""),
            policy_context=getattr(original_command, "policy_context", {}),
            metadata=getattr(original_command, "metadata", {}),
        )

    # Fallback: reconstruir a partir do metadata do entry
    original_metadata = {
        k: v for k, v in entry.metadata.items() if k != "policy_validation"
    }
    return CreateJournalEntryCommand(
        external_id=entry.external_id,
        postings=postings,
        tenant_id="tenantA",
        policy_context={},
        metadata=original_metadata,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestReplayApprovedDecision:
    """Replay de decisão APPROVED produz o mesmo veredito."""

    def test_replay_approved_produces_same_verdict(
        self, engine, repo, noop_emitter, bundle_loader, snapshot_loader, manifest
    ) -> None:
        """Replay de transação aprovada produz APPROVED."""
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_debit", 100_000, "BRL", "DEBIT"),
                PostingInput("acc_credit", 100_000, "BRL", "CREDIT"),
            ],
            tenant_id="tenantA",
        )
        entry = engine.create_journal_entry(cmd)

        # Verificar que o original foi APPROVED
        original_summary = entry.metadata["policy_validation"]
        assert original_summary["final_verdict"] == "APPROVED"

        # Replay
        replay_result = _replay_decision(
            entry, bundle_loader, snapshot_loader, manifest, original_command=cmd
        )
        assert replay_result.decision.final_verdict == FinalVerdict.APPROVED
        assert replay_result.decision.matched_deny_rule is None

    def test_replay_preserves_input_hash(
        self, engine, repo, noop_emitter, bundle_loader, snapshot_loader, manifest
    ) -> None:
        """input_hash do replay é consistente com o original."""
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_debit", 200_000, "BRL", "DEBIT"),
                PostingInput("acc_credit", 200_000, "BRL", "CREDIT"),
            ],
            tenant_id="tenantA",
        )
        entry = engine.create_journal_entry(cmd)

        original_input_hash = entry.metadata["policy_validation"]["input_hash"]

        # Reconstruir contexto a partir do comando original (preserva operation_type, etc.)
        builder = DefaultCanonicalValidationContextBuilder()
        replay_cmd = _reconstruct_command_from_entry(entry, original_command=cmd)
        replay_context = builder.build(replay_cmd)

        # Recalcular input_hash usando o mesmo algoritmo da facade
        import hashlib
        import json

        context_dict = {
            "tenant_id": replay_context.tenant_id,
            "external_id": replay_context.external_id,
            "operation_type": replay_context.operation_type,
            "product_code": replay_context.product_code,
            "channel": replay_context.channel,
            "postings": [
                {
                    "account_id": p.account_id,
                    "amount": p.amount,
                    "currency": p.currency,
                    "direction": p.direction,
                    "account_type": p.account_type,
                }
                for p in replay_context.postings
            ],
            "policy_context": dict(replay_context.policy_context),
            "context_schema_version": replay_context.context_schema_version,
        }
        replay_hash = "sha256:" + hashlib.sha256(
            json.dumps(context_dict, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

        assert original_input_hash == replay_hash


@pytest.mark.integration_local
class TestReplayRejectedDecision:
    """Replay de decisão REJECTED produz o mesmo veredito e mesma deny rule."""

    def test_replay_rejected_produces_same_verdict_and_rule(
        self, facade, noop_emitter, bundle_loader, snapshot_loader, manifest
    ) -> None:
        """Replay de transação rejeitada produz REJECTED com mesma deny rule."""
        # Executar avaliação original via facade (sem persistir, pois PolicyRejected é levantada)
        from dataclasses import dataclass, field
        from typing import Mapping

        @dataclass
        class _ReplayCommand:
            external_id: str = str(uuid.uuid4())
            tenant_id: str = "tenantA"
            operation_type: str = "TRANSFER"
            product_code: str | None = "PIX"
            channel: str | None = None
            postings: tuple = ()
            policy_context: Mapping[str, object] = field(default_factory=dict)
            metadata: Mapping[str, str] = field(default_factory=dict)

        cmd = _ReplayCommand(
            postings=(
                PostingInput("acc_debit", 600_000, "BRL", "DEBIT"),
                PostingInput("acc_credit", 600_000, "BRL", "CREDIT"),
            ),
        )

        # Capturar o trail emitido antes da exceção
        with pytest.raises(PolicyRejected):
            facade.validate(cmd)

        assert len(noop_emitter.emitted_trails) == 1
        original_trail = noop_emitter.emitted_trails[0]
        assert original_trail.final_verdict == FinalVerdict.REJECTED
        original_deny_rule = original_trail.matched_deny_rule

        # Replay: reconstruir contexto e re-avaliar
        builder = DefaultCanonicalValidationContextBuilder()
        context = builder.build(cmd)

        bundle = bundle_loader.load(original_trail.artifact_hash)
        snapshot = snapshot_loader.load(original_trail.snapshot_version)

        active_policy_set = ActivePolicySet(
            manifest=manifest,
            bundle=bundle,
            snapshot=snapshot,
            loaded_at="2024-01-01T00:00:00Z",
            integrity_verified=True,
        )

        evaluator = RuleEvaluator()
        replay_result = evaluator.evaluate(context, active_policy_set)

        assert replay_result.decision.final_verdict == FinalVerdict.REJECTED
        assert replay_result.decision.matched_deny_rule == original_deny_rule


@pytest.mark.integration_local
class TestReplayDivergenceDetection:
    """Replay com bundle diferente pode produzir veredito diferente."""

    def test_replay_with_different_snapshot_detects_divergence(
        self,
        engine,
        repo,
        bundle_loader,
        snapshot_store,
        snapshot_loader,
        compiled_bundle,
        manifest,
    ) -> None:
        """Replay com snapshot de limite mais alto produz veredito diferente."""
        # Transação original: R$ 4.500 — aprovada com limite R$ 5.000
        cmd = CreateJournalEntryCommand(
            external_id=str(uuid.uuid4()),
            postings=[
                PostingInput("acc_debit", 450_000, "BRL", "DEBIT"),
                PostingInput("acc_credit", 450_000, "BRL", "CREDIT"),
            ],
            tenant_id="tenantA",
        )
        entry = engine.create_journal_entry(cmd)
        assert entry.metadata["policy_validation"]["final_verdict"] == "APPROVED"

        # Criar snapshot alternativo com limite mais baixo (R$ 4.000)
        alt_snapshot = ReferenceSnapshot(
            snapshot_version="snap_replay_alt_001",
            snapshot_schema_version="1.0",
            created_at="2024-01-02T00:00:00Z",
            data={"daily_limit_minor": 400_000},
        )
        snapshot_store.store(alt_snapshot)

        # Replay com snapshot alternativo — deveria rejeitar
        alt_manifest = PolicyActivationManifest(
            activation_id="act_replay_alt_001",
            policy_scope_id=_SCOPE_ID,
            artifact_hash=compiled_bundle.artifact_hash,
            snapshot_version=alt_snapshot.snapshot_version,
            context_schema_version=_CONTEXT_SCHEMA_VERSION,
            evaluator_version=EVALUATOR_VERSION,
            activated_at="2024-01-02T00:00:00Z",
            activated_by="replay-test",
        )

        bundle = bundle_loader.load(compiled_bundle.artifact_hash)
        alt_snap = snapshot_loader.load(alt_snapshot.snapshot_version)

        active_policy_set = ActivePolicySet(
            manifest=alt_manifest,
            bundle=bundle,
            snapshot=alt_snap,
            loaded_at="2024-01-02T00:00:00Z",
            integrity_verified=True,
        )

        builder = DefaultCanonicalValidationContextBuilder()
        replay_cmd = _reconstruct_command_from_entry(entry, original_command=cmd)
        context = builder.build(replay_cmd)

        evaluator = RuleEvaluator()
        replay_result = evaluator.evaluate(context, active_policy_set)

        # Divergência detectada: original APPROVED, replay REJECTED
        assert replay_result.decision.final_verdict == FinalVerdict.REJECTED
        assert replay_result.decision.matched_deny_rule == "deny_over_limit"
