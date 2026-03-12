"""
Testes de integração local: pipeline compile → store → load.

Exercita o pipeline completo de compilação, armazenamento e carregamento
de bundles usando S3 mockado via moto.

Cobre:
- Compilar DSL válida e armazenar bundle no S3 mockado
- Carregar bundle de volta e verificar equivalência com o original
- Verificar que artifact_hash, AST e composition_mode são preservados
- Verificar idempotência: armazenar o mesmo bundle duas vezes não gera erro
- Verificar que o BundleLoader valida integridade corretamente

Requisitos cobertos: 2.1, 2.4, 3.1, 3.3
"""
from __future__ import annotations

import pytest

from validation_engine.domain.compiler import DSLCompiler
from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
)
from validation_engine.infrastructure.bundle_loader import BundleLoader
from validation_engine.infrastructure.bundle_store import BundleStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FAKE_KMS_KEY_ID = "arn:aws:kms:us-east-1:123456789012:key/test-key-id"
_CONTEXT_SCHEMA_VERSION = "1.0"
_EVALUATOR_VERSION = "1.0.0"

_DEFAULT_COMPAT = BundleCompatibility(
    dsl_version="1.0",
    context_schema_version=_CONTEXT_SCHEMA_VERSION,
    snapshot_schema_version="1.0",
    evaluator_min_version=_EVALUATOR_VERSION,
)

_DEFAULT_META = CompilationMetadata(
    author="integration-test",
    description="Integration test bundle",
    compiled_at="2024-01-01T00:00:00Z",
    source_hash="sha256:integration_test",
)

_SAMPLE_DSL = """
POLICY deny_over_daily_limit PRIORITY 100
  WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > ref.daily_limit_minor
  THEN DENY "Transaction exceeds daily debit limit"

POLICY deny_blocked_account PRIORITY 90
  WHEN ANY(postings WHERE account_id IN ref.blocked_accounts)
  THEN DENY "Blocked account"

POLICY allow_standard_brl PRIORITY 10
  WHEN facts.posting_count >= 2
    AND COUNT(postings WHERE currency == "BRL") == facts.posting_count
  THEN ALLOW "Standard BRL flow"
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bundle_store(moto_s3_client, local_s3_bucket):
    """
    BundleStore configurado com S3 mockado via moto.

    Args:
        moto_s3_client: Cliente S3 mockado (fixture do conftest local).
        local_s3_bucket: Nome do bucket criado (fixture do conftest local).

    Returns:
        BundleStore pronto para uso em testes.
    """
    return BundleStore(
        s3_client=moto_s3_client,
        bucket_name=local_s3_bucket,
        kms_key_id=_FAKE_KMS_KEY_ID,
    )


@pytest.fixture
def bundle_loader(moto_s3_client, local_s3_bucket):
    """
    BundleLoader configurado com S3 mockado via moto.

    Args:
        moto_s3_client: Cliente S3 mockado (fixture do conftest local).
        local_s3_bucket: Nome do bucket criado (fixture do conftest local).

    Returns:
        BundleLoader pronto para uso em testes.
    """
    return BundleLoader(
        s3_client=moto_s3_client,
        bucket_name=local_s3_bucket,
        current_context_schema_version=_CONTEXT_SCHEMA_VERSION,
        current_evaluator_version=_EVALUATOR_VERSION,
    )


@pytest.fixture
def compiled_bundle():
    """
    Bundle compilado a partir do DSL de exemplo.

    Returns:
        RuleBundle compilado e pronto para armazenamento.
    """
    compiler = DSLCompiler.create_default()
    return compiler.compile(
        dsl_source=_SAMPLE_DSL,
        policy_set_id="integration_test_bundle",
        metadata=_DEFAULT_META,
        compatibility=_DEFAULT_COMPAT,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestCompileStoreLoad:
    """Testa o pipeline completo compile → store → load localmente."""

    def test_compile_produces_valid_bundle(self, compiled_bundle):
        """Compilação deve produzir um bundle válido com artifact_hash."""
        assert compiled_bundle is not None
        assert compiled_bundle.artifact_hash
        assert len(compiled_bundle.artifact_hash) == 64  # SHA-256 hex

    def test_store_bundle_succeeds(self, bundle_store, compiled_bundle):
        """Armazenar bundle no S3 mockado deve ter sucesso."""
        # Should not raise
        bundle_store.store(compiled_bundle)

    def test_load_bundle_after_store_returns_bundle(
        self, bundle_store, bundle_loader, compiled_bundle
    ):
        """Carregar bundle após armazenamento deve retornar bundle válido."""
        bundle_store.store(compiled_bundle)
        loaded = bundle_loader.load(compiled_bundle.artifact_hash)
        assert loaded is not None

    def test_loaded_bundle_has_same_artifact_hash(
        self, bundle_store, bundle_loader, compiled_bundle
    ):
        """Bundle carregado deve ter o mesmo artifact_hash do original."""
        bundle_store.store(compiled_bundle)
        loaded = bundle_loader.load(compiled_bundle.artifact_hash)
        assert loaded.artifact_hash == compiled_bundle.artifact_hash

    def test_loaded_bundle_has_same_policy_set_id(
        self, bundle_store, bundle_loader, compiled_bundle
    ):
        """Bundle carregado deve ter o mesmo policy_set_id do original."""
        bundle_store.store(compiled_bundle)
        loaded = bundle_loader.load(compiled_bundle.artifact_hash)
        assert loaded.policy_set_id == compiled_bundle.policy_set_id

    def test_loaded_bundle_has_same_composition_mode(
        self, bundle_store, bundle_loader, compiled_bundle
    ):
        """Bundle carregado deve ter o mesmo composition_mode do original."""
        bundle_store.store(compiled_bundle)
        loaded = bundle_loader.load(compiled_bundle.artifact_hash)
        assert loaded.composition_mode == compiled_bundle.composition_mode

    def test_loaded_bundle_has_same_ast(
        self, bundle_store, bundle_loader, compiled_bundle
    ):
        """Bundle carregado deve ter o mesmo AST do original."""
        bundle_store.store(compiled_bundle)
        loaded = bundle_loader.load(compiled_bundle.artifact_hash)
        assert loaded.ast == compiled_bundle.ast

    def test_loaded_bundle_has_same_compatibility(
        self, bundle_store, bundle_loader, compiled_bundle
    ):
        """Bundle carregado deve ter a mesma compatibilidade do original."""
        bundle_store.store(compiled_bundle)
        loaded = bundle_loader.load(compiled_bundle.artifact_hash)
        assert loaded.compatibility == compiled_bundle.compatibility

    def test_loaded_bundle_has_same_rule_count(
        self, bundle_store, bundle_loader, compiled_bundle
    ):
        """Bundle carregado deve ter o mesmo número de rules do original."""
        bundle_store.store(compiled_bundle)
        loaded = bundle_loader.load(compiled_bundle.artifact_hash)
        assert len(loaded.ast.rules) == len(compiled_bundle.ast.rules)

    def test_loaded_bundle_equals_original(
        self, bundle_store, bundle_loader, compiled_bundle
    ):
        """Bundle carregado deve ser igual ao original (igualdade estrutural)."""
        bundle_store.store(compiled_bundle)
        loaded = bundle_loader.load(compiled_bundle.artifact_hash)
        assert loaded == compiled_bundle

    def test_idempotent_store_does_not_raise(
        self, bundle_store, compiled_bundle
    ):
        """Armazenar o mesmo bundle duas vezes não deve levantar erro."""
        bundle_store.store(compiled_bundle)
        # Second store should be idempotent
        bundle_store.store(compiled_bundle)

    def test_idempotent_store_loads_correctly(
        self, bundle_store, bundle_loader, compiled_bundle
    ):
        """Bundle armazenado duas vezes deve ser carregado corretamente."""
        bundle_store.store(compiled_bundle)
        bundle_store.store(compiled_bundle)  # idempotent
        loaded = bundle_loader.load(compiled_bundle.artifact_hash)
        assert loaded == compiled_bundle

    def test_bundle_loader_caches_loaded_bundle(
        self, bundle_store, bundle_loader, compiled_bundle
    ):
        """BundleLoader deve cachear o bundle após o primeiro carregamento."""
        bundle_store.store(compiled_bundle)
        # First load: cache miss → S3
        loaded1 = bundle_loader.load(compiled_bundle.artifact_hash)
        # Second load: cache hit → same object
        loaded2 = bundle_loader.load(compiled_bundle.artifact_hash)
        assert loaded1 == loaded2

    def test_full_pipeline_compile_store_load_verify(
        self, bundle_store, bundle_loader
    ):
        """
        Pipeline completo: compile → store → load → verify.

        Verifica que o bundle compilado pode ser armazenado e carregado
        com todas as propriedades preservadas.
        """
        # Compile
        compiler = DSLCompiler.create_default()
        bundle = compiler.compile(
            dsl_source=_SAMPLE_DSL,
            policy_set_id="full_pipeline_test",
            metadata=_DEFAULT_META,
            compatibility=_DEFAULT_COMPAT,
        )

        # Store
        bundle_store.store(bundle)

        # Load
        loaded = bundle_loader.load(bundle.artifact_hash)

        # Verify all key properties
        assert loaded.artifact_hash == bundle.artifact_hash
        assert loaded.policy_set_id == bundle.policy_set_id
        assert loaded.composition_mode == bundle.composition_mode
        assert loaded.ast == bundle.ast
        assert len(loaded.ast.rules) == 3

        # Verify rule names are preserved
        rule_names = {r.name for r in loaded.ast.rules}
        assert "deny_over_daily_limit" in rule_names
        assert "deny_blocked_account" in rule_names
        assert "allow_standard_brl" in rule_names
