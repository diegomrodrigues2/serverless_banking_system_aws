"""Testes de integracao local para PolicyRuntimeRegistry.

Usa moto para simular S3 localmente e mock para ManifestResolver.
Requisitos cobertos: 6.1, 6.4, 17.1, 17.2
"""
from __future__ import annotations
import hashlib
import json
from unittest.mock import MagicMock
import boto3
import pytest
from validation_engine.application.runtime_registry import PolicyRuntimeRegistry
from validation_engine.domain.errors import PolicyBundleUnavailable, PolicyEngineNotReady
from validation_engine.domain.models import (
    BundleCompatibility, CompilationMetadata, PolicyActivationManifest,
    ReferenceSnapshot, RuleBundle,
)
from validation_engine.domain.policy_ast import (
    CompositionMode, ComparisonNode, FieldAccessNode, LiteralNode,
    PolicyEffect, PolicyRuleNode, RuleAST,
)
from validation_engine.infrastructure.bundle_loader import BundleLoader
from validation_engine.infrastructure.bundle_store import BundleStore
from validation_engine.infrastructure.lkg_store import LKGStore
from validation_engine.infrastructure.snapshot_loader import SnapshotLoader
from validation_engine.infrastructure.snapshot_store import SnapshotStore

AWS_REGION = "us-east-1"
LOCAL_S3_BUCKET = "validation-engine-registry-integration-test"
CONTEXT_SCHEMA_VERSION = "1.0"
SNAPSHOT_SCHEMA_VERSION = "1.0"
EVALUATOR_VERSION = "1.0.0"
FAKE_KMS_KEY_ID = "arn:aws:kms:us-east-1:123456789012:key/fake-registry-test-key"


def _make_bundle(policy_set_id="registry-integration-policy-set", description="Registry integration test bundle"):
    rule = PolicyRuleNode(
        name="deny_over_limit", priority=100,
        condition=ComparisonNode(
            left=FieldAccessNode(path=("facts", "posting_count")),
            operator=">=", right=LiteralNode(value=2),
        ),
        effect=PolicyEffect.DENY, message="Integration test deny",
    )
    return RuleBundle(
        policy_set_id=policy_set_id, artifact_hash="placeholder",
        ast=RuleAST(rules=(rule,)), execution_plan={},
        compatibility=BundleCompatibility(
            dsl_version="1.0", context_schema_version=CONTEXT_SCHEMA_VERSION,
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION, evaluator_min_version=EVALUATOR_VERSION,
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author="integration-test", description=description,
            compiled_at="2026-01-01T00:00:00Z", source_hash="sha256:registry_source_001",
        ),
    )


def _make_snapshot(snapshot_version="snap_registry_001"):
    return ReferenceSnapshot(
        snapshot_version=snapshot_version, snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        created_at="2026-01-01T00:00:00Z",
        data={"daily_limit_minor": 500000, "blocked_accounts": ("acc_blocked_001",)},
    )


def _bundle_with_correct_hash(bundle):
    raw = json.loads(bundle.to_json())
    content = {k: v for k, v in raw.items() if k != "artifact_hash"}
    correct_hash = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return RuleBundle(
        policy_set_id=bundle.policy_set_id, artifact_hash=correct_hash,
        ast=bundle.ast, execution_plan=bundle.execution_plan,
        compatibility=bundle.compatibility, composition_mode=bundle.composition_mode,
        metadata=bundle.metadata,
    )


def _make_manifest(scope_id, activation_id, artifact_hash, snapshot_version):
    return PolicyActivationManifest(
        activation_id=activation_id, policy_scope_id=scope_id,
        artifact_hash=artifact_hash, snapshot_version=snapshot_version,
        context_schema_version=CONTEXT_SCHEMA_VERSION, evaluator_version=EVALUATOR_VERSION,
        activated_at="2026-01-01T00:00:00Z", activated_by="integration-test",
    )


@pytest.fixture(scope="function")
def registry_env(tmp_path):
    """PolicyRuntimeRegistry com S3 real via moto e ManifestResolver mockado."""
    try:
        from moto import mock_aws
    except ImportError as exc:
        pytest.skip(f"moto nao instalado: {exc}")

    with mock_aws():
        s3 = boto3.client("s3", region_name=AWS_REGION, aws_access_key_id="test", aws_secret_access_key="test")
        s3.create_bucket(Bucket=LOCAL_S3_BUCKET)
        s3.put_bucket_versioning(Bucket=LOCAL_S3_BUCKET, VersioningConfiguration={"Status": "Enabled"})

        bundle_store = BundleStore(s3_client=s3, bucket_name=LOCAL_S3_BUCKET, kms_key_id=FAKE_KMS_KEY_ID)
        snapshot_store = SnapshotStore(s3_client=s3, bucket_name=LOCAL_S3_BUCKET, kms_key_id=FAKE_KMS_KEY_ID)
        bundle_loader = BundleLoader(
            s3_client=s3, bucket_name=LOCAL_S3_BUCKET,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )
        snapshot_loader = SnapshotLoader(
            s3_client=s3, bucket_name=LOCAL_S3_BUCKET,
            expected_snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        )
        mock_resolver = MagicMock()
        lkg_store = LKGStore(lkg_dir=str(tmp_path / "lkg"))
        registry = PolicyRuntimeRegistry(
            manifest_resolver=mock_resolver, bundle_loader=bundle_loader,
            snapshot_loader=snapshot_loader, lkg_store=lkg_store,
            evaluator_version=EVALUATOR_VERSION,
        )
        yield {
            "registry": registry, "bundle_store": bundle_store,
            "snapshot_store": snapshot_store, "lkg_store": lkg_store,
            "mock_resolver": mock_resolver,
        }


@pytest.mark.integration_local
class TestBootstrapValido:
    """Testa o bootstrap bem-sucedido do runtime registry com S3 real."""

    def test_bootstrap_carrega_active_policy_set(self, registry_env):
        """Bootstrap valido deve carregar bundle e snapshot do S3 e disponibilizar ActivePolicySet."""
        registry = registry_env["registry"]
        bundle_store = registry_env["bundle_store"]
        snapshot_store = registry_env["snapshot_store"]
        mock_resolver = registry_env["mock_resolver"]

        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        bundle = _bundle_with_correct_hash(_make_bundle())
        snapshot = _make_snapshot(snapshot_version="snap_001")

        bundle_store.store(bundle)
        snapshot_store.store(snapshot)
        mock_resolver.resolve.return_value = _make_manifest(scope_id, "act_001", bundle.artifact_hash, "snap_001")

        registry.refresh_scope(scope_id)

        aps = registry.get_active_policy_set(scope_id)
        assert aps is not None
        assert aps.manifest.activation_id == "act_001"
        assert aps.manifest.artifact_hash == bundle.artifact_hash
        assert aps.integrity_verified is True
        assert aps.bundle.policy_set_id == bundle.policy_set_id
        assert aps.snapshot.data["daily_limit_minor"] == 500000
        assert aps.snapshot.data["blocked_accounts"] == ("acc_blocked_001",)

    def test_bootstrap_marca_boot_valido(self, registry_env):
        """Bootstrap bem-sucedido deve marcar boot valido no LKGStore."""
        registry = registry_env["registry"]
        bundle_store = registry_env["bundle_store"]
        snapshot_store = registry_env["snapshot_store"]
        mock_resolver = registry_env["mock_resolver"]
        lkg_store = registry_env["lkg_store"]

        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        bundle = _bundle_with_correct_hash(_make_bundle())
        snapshot = _make_snapshot(snapshot_version="snap_002")

        bundle_store.store(bundle)
        snapshot_store.store(snapshot)
        mock_resolver.resolve.return_value = _make_manifest(scope_id, "act_002", bundle.artifact_hash, "snap_002")

        assert lkg_store.has_valid_boot is False
        registry.refresh_scope(scope_id)
        assert lkg_store.has_valid_boot is True

    def test_bootstrap_salva_lkg_em_disco(self, registry_env):
        """Bootstrap bem-sucedido deve salvar o LKG em disco."""
        registry = registry_env["registry"]
        bundle_store = registry_env["bundle_store"]
        snapshot_store = registry_env["snapshot_store"]
        mock_resolver = registry_env["mock_resolver"]
        lkg_store = registry_env["lkg_store"]

        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        bundle = _bundle_with_correct_hash(_make_bundle())
        snapshot = _make_snapshot(snapshot_version="snap_003")

        bundle_store.store(bundle)
        snapshot_store.store(snapshot)
        mock_resolver.resolve.return_value = _make_manifest(scope_id, "act_003", bundle.artifact_hash, "snap_003")

        registry.refresh_scope(scope_id)

        lkg = lkg_store.load(scope_id)
        assert lkg is not None
        assert lkg.manifest.activation_id == "act_003"
        assert lkg.bundle.policy_set_id == bundle.policy_set_id


@pytest.mark.integration_local
class TestRefreshESwapAtomico:
    """Testa o refresh e swap atomico do ActivePolicySet com S3 real."""

    def test_refresh_com_novo_activation_id_troca_conjunto(self, registry_env):
        """Refresh com novo activation_id deve carregar novos artefatos e fazer swap atomico."""
        registry = registry_env["registry"]
        bundle_store = registry_env["bundle_store"]
        snapshot_store = registry_env["snapshot_store"]
        mock_resolver = registry_env["mock_resolver"]

        scope_id = "tenantA:TRANSFER:PIX:*:prod"

        bundle_v1 = _bundle_with_correct_hash(_make_bundle(policy_set_id="policy-set-v1", description="v1"))
        snapshot_v1 = _make_snapshot(snapshot_version="snap_v1")
        bundle_store.store(bundle_v1)
        snapshot_store.store(snapshot_v1)
        mock_resolver.resolve.return_value = _make_manifest(scope_id, "act_v1", bundle_v1.artifact_hash, "snap_v1")
        registry.refresh_scope(scope_id)
        assert registry.get_active_policy_set(scope_id).manifest.activation_id == "act_v1"

        bundle_v2 = _bundle_with_correct_hash(_make_bundle(policy_set_id="policy-set-v2", description="v2"))
        snapshot_v2 = _make_snapshot(snapshot_version="snap_v2")
        bundle_store.store(bundle_v2)
        snapshot_store.store(snapshot_v2)
        registry._bundle_loader.invalidate(bundle_v1.artifact_hash)
        mock_resolver.resolve.return_value = _make_manifest(scope_id, "act_v2", bundle_v2.artifact_hash, "snap_v2")
        registry.refresh_scope(scope_id)

        aps_v2 = registry.get_active_policy_set(scope_id)
        assert aps_v2.manifest.activation_id == "act_v2"
        assert aps_v2.bundle.policy_set_id == "policy-set-v2"
        assert aps_v2.manifest.snapshot_version == "snap_v2"

    def test_hot_path_retorna_mesmo_objeto_em_memoria(self, registry_env):
        """Apos bootstrap, get_active_policy_set() deve retornar o mesmo objeto sem I/O."""
        registry = registry_env["registry"]
        bundle_store = registry_env["bundle_store"]
        snapshot_store = registry_env["snapshot_store"]
        mock_resolver = registry_env["mock_resolver"]

        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        bundle = _bundle_with_correct_hash(_make_bundle())
        snapshot = _make_snapshot(snapshot_version="snap_hot")

        bundle_store.store(bundle)
        snapshot_store.store(snapshot)
        mock_resolver.resolve.return_value = _make_manifest(scope_id, "act_hot", bundle.artifact_hash, "snap_hot")
        registry.refresh_scope(scope_id)

        aps_1 = registry.get_active_policy_set(scope_id)
        aps_2 = registry.get_active_policy_set(scope_id)
        aps_3 = registry.get_active_policy_set(scope_id)

        assert aps_1 is aps_2
        assert aps_2 is aps_3
        assert mock_resolver.resolve.call_count == 1


@pytest.mark.integration_local
class TestFallbackLKGIntegracao:
    """Testa o fallback para Last Known Good com S3 real."""

    def test_usa_lkg_se_resolver_falhar_apos_boot(self, registry_env):
        """Apos boot valido, falha do resolver deve usar LKG em vez de PolicyEngineNotReady."""
        registry = registry_env["registry"]
        bundle_store = registry_env["bundle_store"]
        snapshot_store = registry_env["snapshot_store"]
        mock_resolver = registry_env["mock_resolver"]
        lkg_store = registry_env["lkg_store"]

        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        bundle = _bundle_with_correct_hash(_make_bundle())
        snapshot = _make_snapshot(snapshot_version="snap_lkg")

        bundle_store.store(bundle)
        snapshot_store.store(snapshot)
        mock_resolver.resolve.return_value = _make_manifest(scope_id, "act_lkg", bundle.artifact_hash, "snap_lkg")
        registry.refresh_scope(scope_id)
        assert lkg_store.has_valid_boot is True

        mock_resolver.resolve.side_effect = PolicyBundleUnavailable("AppConfig indisponivel")
        del registry._active_sets[scope_id]

        registry.refresh_scope(scope_id)

        aps = registry.get_active_policy_set(scope_id)
        assert aps is not None
        assert aps.manifest.activation_id == "act_lkg"

    def test_policy_engine_not_ready_sem_lkg_no_cold_start(self, registry_env):
        """Cold start com resolver falhando deve levantar PolicyEngineNotReady."""
        registry = registry_env["registry"]
        mock_resolver = registry_env["mock_resolver"]

        scope_id = "escopo_sem_manifesto:TRANSFER:*:*:prod"
        mock_resolver.resolve.side_effect = PolicyBundleUnavailable("Escopo nao encontrado")

        with pytest.raises(PolicyEngineNotReady) as exc_info:
            registry.refresh_scope(scope_id)

        assert "escopo_sem_manifesto" in str(exc_info.value)
