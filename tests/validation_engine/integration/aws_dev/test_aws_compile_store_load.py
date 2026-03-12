"""
Testes de integração AWS dev — pipeline compile → store → load.

Usa recursos AWS REAIS (S3 real) em ambiente dev.
NÃO usa moto ou qualquer mock de AWS.

Pré-requisitos:
    - VALIDATION_ENGINE_TEST_BUCKET: bucket S3 dedicado para testes
    - VALIDATION_ENGINE_TEST_KMS_KEY_ARN: ARN da chave KMS (opcional)
    - AWS_REGION: região AWS (padrão: us-east-1)
    - Credenciais AWS válidas com permissão de leitura/escrita no bucket

Estratégia de isolamento:
    O run_id (UUID único por sessão) é embutido no policy_set_id dos bundles.
    Isso garante que testes de sessões diferentes não colidem entre si.

Cleanup:
    Cada teste deleta os objetos que criou ao final via fixture de cleanup.
    O cleanup é best-effort: falhas são logadas mas não falham os testes.

Requisitos cobertos: 2.4, 3.1, 3.6
"""
from __future__ import annotations

import logging
import os
from collections.abc import Generator

import pytest

from validation_engine.domain.compiler import DSLCompiler
from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
)
from validation_engine.infrastructure.bundle_loader import BundleLoader
from validation_engine.infrastructure.bundle_store import BundleStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTEXT_SCHEMA_VERSION = "1.0"
EVALUATOR_VERSION = "1.0.0"
KMS_KEY_ARN_ENV_VAR = "VALIDATION_ENGINE_TEST_KMS_KEY_ARN"

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


@pytest.fixture(scope="module")
def kms_key_id() -> str:
    """
    ARN da chave KMS para testes AWS dev.

    Retorna um ARN fictício se a variável de ambiente não estiver definida.
    Testes que requerem KMS real devem verificar esta variável explicitamente.

    Returns:
        ARN da chave KMS ou valor fictício para testes sem KMS real.
    """
    return os.environ.get(
        KMS_KEY_ARN_ENV_VAR,
        "arn:aws:kms:us-east-1:123456789012:key/test-key-placeholder",
    )


@pytest.fixture(scope="module")
def aws_bundle_store(aws_dev_s3_client, aws_dev_config, kms_key_id) -> BundleStore:
    """
    BundleStore configurado para S3 real em ambiente dev.

    Args:
        aws_dev_s3_client: Cliente S3 real (fixture do conftest aws_dev).
        aws_dev_config:    Configuração da sessão AWS dev.
        kms_key_id:        ARN da chave KMS.

    Returns:
        BundleStore apontando para S3 real.
    """
    return BundleStore(
        s3_client=aws_dev_s3_client,
        bucket_name=aws_dev_config.bucket,
        kms_key_id=kms_key_id,
    )


@pytest.fixture(scope="module")
def aws_bundle_loader(aws_dev_s3_client, aws_dev_config) -> BundleLoader:
    """
    BundleLoader configurado para S3 real em ambiente dev.

    Args:
        aws_dev_s3_client: Cliente S3 real (fixture do conftest aws_dev).
        aws_dev_config:    Configuração da sessão AWS dev.

    Returns:
        BundleLoader apontando para S3 real.
    """
    return BundleLoader(
        s3_client=aws_dev_s3_client,
        bucket_name=aws_dev_config.bucket,
        current_context_schema_version=CONTEXT_SCHEMA_VERSION,
        current_evaluator_version=EVALUATOR_VERSION,
    )


@pytest.fixture(scope="module")
def aws_compiled_bundle(aws_dev_config) -> object:
    """
    Bundle compilado para testes AWS dev.

    Usa o run_id da sessão no policy_set_id para garantir isolamento.

    Args:
        aws_dev_config: Configuração da sessão AWS dev.

    Returns:
        RuleBundle compilado e pronto para armazenamento.
    """
    compiler = DSLCompiler.create_default()
    metadata = CompilationMetadata(
        author="aws-dev-integration-test",
        description=f"AWS dev integration test bundle — run {aws_dev_config.run_id}",
        compiled_at="2024-01-01T00:00:00Z",
        source_hash="sha256:aws_dev_test",
    )
    compatibility = BundleCompatibility(
        dsl_version="1.0",
        context_schema_version=CONTEXT_SCHEMA_VERSION,
        snapshot_schema_version="1.0",
        evaluator_min_version=EVALUATOR_VERSION,
    )
    return compiler.compile(
        dsl_source=_SAMPLE_DSL,
        policy_set_id=f"aws-dev-test-{aws_dev_config.run_id}",
        metadata=metadata,
        compatibility=compatibility,
    )


@pytest.fixture(scope="module")
def stored_bundle_hash(
    aws_bundle_store, aws_compiled_bundle, aws_dev_s3_client, aws_dev_config
) -> Generator[str, None, None]:
    """
    Armazena o bundle no S3 real e retorna o artifact_hash.

    Realiza cleanup do objeto S3 ao final do módulo de testes.

    Args:
        aws_bundle_store:  BundleStore para S3 real.
        aws_compiled_bundle: Bundle compilado.
        aws_dev_s3_client: Cliente S3 para cleanup.
        aws_dev_config:    Configuração da sessão.

    Yields:
        artifact_hash do bundle armazenado.
    """
    aws_bundle_store.store(aws_compiled_bundle)
    artifact_hash = aws_compiled_bundle.artifact_hash

    yield artifact_hash

    # Cleanup: remove o objeto de teste do S3
    key = f"bundles/{artifact_hash}.json"
    try:
        aws_dev_s3_client.delete_object(
            Bucket=aws_dev_config.bucket,
            Key=key,
        )
        logger.info(
            "Cleanup: objeto de teste removido do S3",
            extra={"bucket": aws_dev_config.bucket, "key": key},
        )
    except Exception as exc:
        logger.warning(
            "Cleanup: falha ao remover objeto de teste do S3",
            extra={"bucket": aws_dev_config.bucket, "key": key, "error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
class TestAWSCompileStoreLoad:
    """
    Testes de integração AWS dev para o pipeline compile → store → load.

    Todos os testes são ignorados se VALIDATION_ENGINE_TEST_BUCKET não estiver
    definido (via pytest.skip no conftest aws_dev).
    """

    def test_compile_produces_valid_bundle(self, aws_compiled_bundle):
        """Compilação deve produzir bundle válido com artifact_hash SHA-256."""
        assert aws_compiled_bundle is not None
        assert len(aws_compiled_bundle.artifact_hash) == 64
        assert all(c in "0123456789abcdef" for c in aws_compiled_bundle.artifact_hash)

    def test_store_bundle_in_real_s3_succeeds(self, aws_bundle_store, aws_compiled_bundle):
        """Armazenar bundle no S3 real deve ter sucesso."""
        # Should not raise — idempotent if already stored
        aws_bundle_store.store(aws_compiled_bundle)

    def test_load_bundle_from_real_s3_returns_bundle(
        self, aws_bundle_loader, stored_bundle_hash
    ):
        """Carregar bundle do S3 real deve retornar bundle válido."""
        loaded = aws_bundle_loader.load(stored_bundle_hash)
        assert loaded is not None

    def test_loaded_bundle_artifact_hash_matches(
        self, aws_bundle_loader, aws_compiled_bundle, stored_bundle_hash
    ):
        """artifact_hash do bundle carregado deve corresponder ao original."""
        loaded = aws_bundle_loader.load(stored_bundle_hash)
        assert loaded.artifact_hash == aws_compiled_bundle.artifact_hash

    def test_loaded_bundle_ast_matches_original(
        self, aws_bundle_loader, aws_compiled_bundle, stored_bundle_hash
    ):
        """AST do bundle carregado deve ser igual ao original."""
        loaded = aws_bundle_loader.load(stored_bundle_hash)
        assert loaded.ast == aws_compiled_bundle.ast

    def test_loaded_bundle_composition_mode_matches(
        self, aws_bundle_loader, aws_compiled_bundle, stored_bundle_hash
    ):
        """composition_mode do bundle carregado deve ser igual ao original."""
        loaded = aws_bundle_loader.load(stored_bundle_hash)
        assert loaded.composition_mode == aws_compiled_bundle.composition_mode

    def test_loaded_bundle_equals_original(
        self, aws_bundle_loader, aws_compiled_bundle, stored_bundle_hash
    ):
        """Bundle carregado deve ser estruturalmente igual ao original."""
        loaded = aws_bundle_loader.load(stored_bundle_hash)
        assert loaded == aws_compiled_bundle

    def test_idempotent_store_in_real_s3(
        self, aws_bundle_store, aws_compiled_bundle, stored_bundle_hash
    ):
        """
        Armazenar o mesmo bundle duas vezes no S3 real não deve levantar erro.

        O stored_bundle_hash fixture já armazenou o bundle uma vez.
        Esta chamada adicional verifica idempotência.
        """
        # stored_bundle_hash already stored it once; store again
        aws_bundle_store.store(aws_compiled_bundle)

    def test_loaded_bundle_has_correct_rule_count(
        self, aws_bundle_loader, stored_bundle_hash
    ):
        """Bundle carregado deve ter 3 rules (conforme o DSL de exemplo)."""
        loaded = aws_bundle_loader.load(stored_bundle_hash)
        assert len(loaded.ast.rules) == 3

    def test_loaded_bundle_has_correct_rule_names(
        self, aws_bundle_loader, stored_bundle_hash
    ):
        """Bundle carregado deve ter os nomes de rules corretos."""
        loaded = aws_bundle_loader.load(stored_bundle_hash)
        rule_names = {r.name for r in loaded.ast.rules}
        assert "deny_over_daily_limit" in rule_names
        assert "deny_blocked_account" in rule_names
        assert "allow_standard_brl" in rule_names
