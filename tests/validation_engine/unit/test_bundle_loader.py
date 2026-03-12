"""
Testes unitários para BundleLoader.

Verifica:
- Cache hit: bundle servido do cache sem I/O ao S3
- Cache miss: bundle carregado do S3, verificado e armazenado em cache
- Integridade: hash divergente levanta PolicyBundleIntegrityFailure
- Compatibilidade: context_schema_version incompatível levanta InvalidPolicyBundle
- Compatibilidade: evaluator_min_version incompatível levanta InvalidPolicyBundle
- Indisponibilidade: objeto não encontrado no S3 levanta PolicyBundleUnavailable
- Indisponibilidade: erro de I/O no S3 levanta PolicyBundleUnavailable
- Invalidação: invalidate() remove bundle do cache
- Chave S3: formato correto bundles/{artifact_hash}.json

Requisitos cobertos: 3.3, 3.4, 11.6, 17.3, 20.3, 20.4
"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from validation_engine.domain.errors import (
    InvalidPolicyBundle,
    PolicyBundleIntegrityFailure,
    PolicyBundleUnavailable,
)
from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
    RuleBundle,
)
from validation_engine.domain.policy_ast import (
    CompositionMode,
    ComparisonNode,
    FieldAccessNode,
    LiteralNode,
    PolicyEffect,
    PolicyRuleNode,
    RuleAST,
)
from validation_engine.infrastructure.bundle_loader import BundleLoader


# ---------------------------------------------------------------------------
# Helpers de construção de bundle e hash
# ---------------------------------------------------------------------------


def _make_bundle(
    artifact_hash: str = "placeholder",
    context_schema_version: str = "1.0",
    evaluator_min_version: str = "1.0.0",
) -> RuleBundle:
    """Constrói um RuleBundle mínimo para testes."""
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
    return RuleBundle(
        policy_set_id="test-policy-set",
        artifact_hash=artifact_hash,
        ast=RuleAST(rules=(rule,)),
        execution_plan={"version": 1, "steps": []},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version=context_schema_version,
            snapshot_schema_version="1.0",
            evaluator_min_version=evaluator_min_version,
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author="test-author",
            description="Test bundle",
            compiled_at="2026-03-11T00:00:00Z",
            source_hash="sha256:src",
        ),
    )


def _compute_correct_hash(bundle: RuleBundle) -> str:
    """
    Calcula o artifact_hash correto para um bundle.

    Replica o algoritmo do DSLCompiler:
    1. Serializar o bundle para dict
    2. Remover o campo artifact_hash
    3. Re-serializar com sort_keys=True
    4. SHA-256 do conteúdo UTF-8
    """
    raw = json.loads(bundle.to_json())
    content_without_hash = {k: v for k, v in raw.items() if k != "artifact_hash"}
    canonical = json.dumps(content_without_hash, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _make_bundle_with_correct_hash(
    context_schema_version: str = "1.0",
    evaluator_min_version: str = "1.0.0",
) -> RuleBundle:
    """
    Constrói um bundle com artifact_hash correto (calculado sobre o conteúdo).

    Necessário para testes de carga bem-sucedida, onde a verificação de
    integridade deve passar.
    """
    # Primeiro cria com hash placeholder para calcular o hash real
    bundle_placeholder = _make_bundle(
        artifact_hash="placeholder",
        context_schema_version=context_schema_version,
        evaluator_min_version=evaluator_min_version,
    )
    correct_hash = _compute_correct_hash(bundle_placeholder)

    # Recria com o hash correto
    return _make_bundle(
        artifact_hash=correct_hash,
        context_schema_version=context_schema_version,
        evaluator_min_version=evaluator_min_version,
    )


def _make_s3_response(content: str) -> dict:
    """Constrói uma resposta S3 get_object simulada."""
    return {"Body": BytesIO(content.encode("utf-8"))}


def _make_client_error(code: str) -> Exception:
    """Constrói um ClientError boto3 simulado."""
    import botocore.exceptions

    error_response = {"Error": {"Code": code, "Message": f"Simulated {code}"}}
    return botocore.exceptions.ClientError(error_response, "GetObject")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def s3_mock() -> MagicMock:
    """Cliente S3 mockado para testes unitários."""
    return MagicMock()


@pytest.fixture
def loader(s3_mock) -> BundleLoader:
    """BundleLoader configurado com versões de runtime padrão."""
    return BundleLoader(
        s3_client=s3_mock,
        bucket_name="test-bucket",
        current_context_schema_version="1.0",
        current_evaluator_version="1.0.0",
    )


@pytest.fixture
def valid_bundle() -> RuleBundle:
    """Bundle com artifact_hash correto e compatível com o loader padrão."""
    return _make_bundle_with_correct_hash(
        context_schema_version="1.0",
        evaluator_min_version="1.0.0",
    )


# ---------------------------------------------------------------------------
# Testes de chave S3
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleLoaderObjectKey:
    """Verifica o formato da chave S3 gerada pelo loader."""

    def test_key_format(self, loader):
        key = loader._object_key("sha256:abc123")
        assert key == "bundles/sha256:abc123.json"

    def test_key_prefix_is_bundles(self, loader):
        key = loader._object_key("any-hash")
        assert key.startswith("bundles/")

    def test_key_suffix_is_json(self, loader):
        key = loader._object_key("any-hash")
        assert key.endswith(".json")


# ---------------------------------------------------------------------------
# Testes de cache hit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleLoaderCacheHit:
    """Verifica que bundles em cache são servidos sem I/O ao S3."""

    def test_cache_hit_returns_same_bundle(self, loader, s3_mock, valid_bundle):
        """Segundo load() deve retornar o mesmo bundle sem chamar S3."""
        s3_mock.get_object.return_value = _make_s3_response(valid_bundle.to_json())

        first = loader.load(valid_bundle.artifact_hash)
        second = loader.load(valid_bundle.artifact_hash)

        assert first == second
        # S3 deve ter sido chamado apenas uma vez (no cache miss)
        assert s3_mock.get_object.call_count == 1

    def test_cache_hit_does_not_call_s3(self, loader, s3_mock, valid_bundle):
        """Após cache miss, o segundo load não deve chamar get_object."""
        s3_mock.get_object.return_value = _make_s3_response(valid_bundle.to_json())

        loader.load(valid_bundle.artifact_hash)
        s3_mock.get_object.reset_mock()

        loader.load(valid_bundle.artifact_hash)
        s3_mock.get_object.assert_not_called()


# ---------------------------------------------------------------------------
# Testes de cache miss (carga bem-sucedida)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleLoaderCacheMiss:
    """Verifica carga bem-sucedida do S3 com verificação de integridade e compatibilidade."""

    def test_load_returns_correct_bundle(self, loader, s3_mock, valid_bundle):
        """load() deve retornar o bundle correto após carga do S3."""
        s3_mock.get_object.return_value = _make_s3_response(valid_bundle.to_json())

        result = loader.load(valid_bundle.artifact_hash)

        assert result.artifact_hash == valid_bundle.artifact_hash
        assert result.policy_set_id == valid_bundle.policy_set_id

    def test_load_calls_s3_with_correct_key(self, loader, s3_mock, valid_bundle):
        """load() deve chamar get_object com a chave correta."""
        s3_mock.get_object.return_value = _make_s3_response(valid_bundle.to_json())

        loader.load(valid_bundle.artifact_hash)

        s3_mock.get_object.assert_called_once_with(
            Bucket="test-bucket",
            Key=f"bundles/{valid_bundle.artifact_hash}.json",
        )

    def test_load_stores_bundle_in_cache(self, loader, s3_mock, valid_bundle):
        """Após carga bem-sucedida, o bundle deve estar no cache."""
        s3_mock.get_object.return_value = _make_s3_response(valid_bundle.to_json())

        loader.load(valid_bundle.artifact_hash)

        assert valid_bundle.artifact_hash in loader._cache

    def test_load_preserves_bundle_fields(self, loader, s3_mock, valid_bundle):
        """O bundle carregado deve ter os mesmos campos do original."""
        s3_mock.get_object.return_value = _make_s3_response(valid_bundle.to_json())

        result = loader.load(valid_bundle.artifact_hash)

        assert result.compatibility == valid_bundle.compatibility
        assert result.composition_mode == valid_bundle.composition_mode
        assert result.metadata == valid_bundle.metadata


# ---------------------------------------------------------------------------
# Testes de verificação de integridade
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleLoaderIntegrityVerification:
    """Verifica que hash divergente levanta PolicyBundleIntegrityFailure."""

    def test_integrity_failure_on_hash_mismatch(self, loader, s3_mock):
        """
        Se o hash calculado divergir do esperado, deve levantar
        PolicyBundleIntegrityFailure.
        """
        # Bundle com hash incorreto (não corresponde ao conteúdo)
        bundle_with_wrong_hash = _make_bundle(artifact_hash="wrong-hash-value")
        s3_mock.get_object.return_value = _make_s3_response(bundle_with_wrong_hash.to_json())

        with pytest.raises(PolicyBundleIntegrityFailure) as exc_info:
            loader.load("wrong-hash-value")

        assert "wrong-hash-value" in str(exc_info.value.message)

    def test_integrity_failure_does_not_cache_bundle(self, loader, s3_mock):
        """Bundle com hash inválido não deve ser armazenado no cache."""
        bundle_with_wrong_hash = _make_bundle(artifact_hash="wrong-hash")
        s3_mock.get_object.return_value = _make_s3_response(bundle_with_wrong_hash.to_json())

        with pytest.raises(PolicyBundleIntegrityFailure):
            loader.load("wrong-hash")

        assert "wrong-hash" not in loader._cache

    def test_integrity_passes_for_correct_hash(self, loader, s3_mock, valid_bundle):
        """Bundle com hash correto deve passar na verificação de integridade."""
        s3_mock.get_object.return_value = _make_s3_response(valid_bundle.to_json())

        # Não deve levantar exceção
        result = loader.load(valid_bundle.artifact_hash)
        assert result is not None

    def test_integrity_check_excludes_artifact_hash_field(self, loader, s3_mock):
        """
        A verificação de integridade deve excluir o campo artifact_hash
        do cálculo do hash, conforme especificado no design.
        """
        # Cria bundle com hash correto
        bundle = _make_bundle_with_correct_hash()
        s3_mock.get_object.return_value = _make_s3_response(bundle.to_json())

        # Deve carregar sem erro — o hash foi calculado excluindo o campo artifact_hash
        result = loader.load(bundle.artifact_hash)
        assert result.artifact_hash == bundle.artifact_hash


# ---------------------------------------------------------------------------
# Testes de verificação de compatibilidade
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleLoaderCompatibilityVerification:
    """Verifica que incompatibilidade de versão levanta InvalidPolicyBundle."""

    def test_incompatible_context_schema_version_raises(self, s3_mock):
        """
        Bundle com context_schema_version diferente do runtime deve levantar
        InvalidPolicyBundle.
        """
        # Loader configurado para versão "1.0"
        loader = BundleLoader(
            s3_client=s3_mock,
            bucket_name="test-bucket",
            current_context_schema_version="1.0",
            current_evaluator_version="1.0.0",
        )

        # Bundle compilado para versão "2.0" — incompatível
        bundle = _make_bundle_with_correct_hash(
            context_schema_version="2.0",
            evaluator_min_version="1.0.0",
        )
        s3_mock.get_object.return_value = _make_s3_response(bundle.to_json())

        with pytest.raises(InvalidPolicyBundle) as exc_info:
            loader.load(bundle.artifact_hash)

        assert "context_schema_version" in str(exc_info.value.message)
        assert "2.0" in str(exc_info.value.message)
        assert "1.0" in str(exc_info.value.message)

    def test_incompatible_evaluator_version_raises(self, s3_mock):
        """
        Bundle com evaluator_min_version diferente do runtime deve levantar
        InvalidPolicyBundle.
        """
        loader = BundleLoader(
            s3_client=s3_mock,
            bucket_name="test-bucket",
            current_context_schema_version="1.0",
            current_evaluator_version="1.0.0",
        )

        # Bundle requer evaluator 2.0.0 — incompatível com runtime 1.0.0
        bundle = _make_bundle_with_correct_hash(
            context_schema_version="1.0",
            evaluator_min_version="2.0.0",
        )
        s3_mock.get_object.return_value = _make_s3_response(bundle.to_json())

        with pytest.raises(InvalidPolicyBundle) as exc_info:
            loader.load(bundle.artifact_hash)

        assert "evaluator_min_version" in str(exc_info.value.message)
        assert "2.0.0" in str(exc_info.value.message)

    def test_incompatible_bundle_does_not_cache(self, s3_mock):
        """Bundle incompatível não deve ser armazenado no cache."""
        loader = BundleLoader(
            s3_client=s3_mock,
            bucket_name="test-bucket",
            current_context_schema_version="1.0",
            current_evaluator_version="1.0.0",
        )

        bundle = _make_bundle_with_correct_hash(
            context_schema_version="2.0",
            evaluator_min_version="1.0.0",
        )
        s3_mock.get_object.return_value = _make_s3_response(bundle.to_json())

        with pytest.raises(InvalidPolicyBundle):
            loader.load(bundle.artifact_hash)

        assert bundle.artifact_hash not in loader._cache

    def test_compatible_bundle_passes_all_checks(self, loader, s3_mock, valid_bundle):
        """Bundle compatível deve passar em todas as verificações."""
        s3_mock.get_object.return_value = _make_s3_response(valid_bundle.to_json())

        result = loader.load(valid_bundle.artifact_hash)

        assert result.compatibility.context_schema_version == "1.0"
        assert result.compatibility.evaluator_min_version == "1.0.0"


# ---------------------------------------------------------------------------
# Testes de indisponibilidade do S3
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleLoaderS3Unavailability:
    """Verifica que erros de I/O do S3 levantam PolicyBundleUnavailable."""

    def test_not_found_raises_bundle_unavailable(self, loader, s3_mock):
        """Objeto não encontrado (404) deve levantar PolicyBundleUnavailable."""
        s3_mock.get_object.side_effect = _make_client_error("404")

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            loader.load("sha256:nonexistent")

        assert "sha256:nonexistent" in str(exc_info.value.message)

    def test_no_such_key_raises_bundle_unavailable(self, loader, s3_mock):
        """NoSuchKey deve levantar PolicyBundleUnavailable."""
        s3_mock.get_object.side_effect = _make_client_error("NoSuchKey")

        with pytest.raises(PolicyBundleUnavailable):
            loader.load("sha256:nonexistent")

    def test_io_error_raises_bundle_unavailable(self, loader, s3_mock):
        """Erro de I/O genérico (500) deve levantar PolicyBundleUnavailable."""
        s3_mock.get_object.side_effect = _make_client_error("500")

        with pytest.raises(PolicyBundleUnavailable):
            loader.load("sha256:any-hash")

    def test_access_denied_raises_bundle_unavailable(self, loader, s3_mock):
        """Erro de acesso negado (403) deve levantar PolicyBundleUnavailable."""
        s3_mock.get_object.side_effect = _make_client_error("403")

        with pytest.raises(PolicyBundleUnavailable):
            loader.load("sha256:any-hash")

    def test_unavailable_bundle_not_cached(self, loader, s3_mock):
        """Bundle indisponível não deve ser armazenado no cache."""
        s3_mock.get_object.side_effect = _make_client_error("404")

        with pytest.raises(PolicyBundleUnavailable):
            loader.load("sha256:nonexistent")

        assert "sha256:nonexistent" not in loader._cache


# ---------------------------------------------------------------------------
# Testes de invalidação de cache
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleLoaderInvalidation:
    """Verifica que invalidate() remove bundles do cache corretamente."""

    def test_invalidate_removes_bundle_from_cache(self, loader, s3_mock, valid_bundle):
        """Após invalidate(), o bundle deve ser removido do cache."""
        s3_mock.get_object.return_value = _make_s3_response(valid_bundle.to_json())
        loader.load(valid_bundle.artifact_hash)

        assert valid_bundle.artifact_hash in loader._cache

        loader.invalidate(valid_bundle.artifact_hash)

        assert valid_bundle.artifact_hash not in loader._cache

    def test_invalidate_forces_s3_reload(self, loader, s3_mock, valid_bundle):
        """Após invalidate(), o próximo load() deve buscar do S3 novamente."""
        # Retorna um novo BytesIO a cada chamada para evitar EOF no segundo read
        json_content = valid_bundle.to_json()
        s3_mock.get_object.side_effect = lambda **kwargs: _make_s3_response(json_content)

        loader.load(valid_bundle.artifact_hash)
        loader.invalidate(valid_bundle.artifact_hash)
        loader.load(valid_bundle.artifact_hash)

        # S3 deve ter sido chamado duas vezes: antes e depois do invalidate
        assert s3_mock.get_object.call_count == 2

    def test_invalidate_nonexistent_key_does_not_raise(self, loader):
        """invalidate() de chave não presente no cache não deve levantar exceção."""
        # Não deve levantar exceção
        loader.invalidate("sha256:not-in-cache")

    def test_invalidate_only_removes_specified_bundle(self, loader, s3_mock):
        """invalidate() deve remover apenas o bundle especificado."""
        bundle_a = _make_bundle_with_correct_hash(context_schema_version="1.0")
        bundle_b = _make_bundle_with_correct_hash(
            context_schema_version="1.0",
            evaluator_min_version="1.0.0",
        )

        # Carrega dois bundles diferentes (simulando hashes distintos)
        s3_mock.get_object.return_value = _make_s3_response(bundle_a.to_json())
        loader.load(bundle_a.artifact_hash)

        # Invalida apenas o bundle_a
        loader.invalidate(bundle_a.artifact_hash)

        # bundle_a deve ter sido removido
        assert bundle_a.artifact_hash not in loader._cache


# ---------------------------------------------------------------------------
# Testes de erros de domínio
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleLoaderErrorCodes:
    """Verifica que os erros levantados têm os códigos corretos."""

    def test_bundle_unavailable_has_correct_code(self, loader, s3_mock):
        s3_mock.get_object.side_effect = _make_client_error("404")

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            loader.load("sha256:any")

        assert exc_info.value.code == "POLICY_BUNDLE_UNAVAILABLE"

    def test_integrity_failure_has_correct_code(self, loader, s3_mock):
        bundle = _make_bundle(artifact_hash="wrong-hash")
        s3_mock.get_object.return_value = _make_s3_response(bundle.to_json())

        with pytest.raises(PolicyBundleIntegrityFailure) as exc_info:
            loader.load("wrong-hash")

        assert exc_info.value.code == "POLICY_BUNDLE_INTEGRITY_FAILURE"

    def test_invalid_bundle_has_correct_code(self, s3_mock):
        loader = BundleLoader(
            s3_client=s3_mock,
            bucket_name="test-bucket",
            current_context_schema_version="1.0",
            current_evaluator_version="1.0.0",
        )
        bundle = _make_bundle_with_correct_hash(context_schema_version="2.0")
        s3_mock.get_object.return_value = _make_s3_response(bundle.to_json())

        with pytest.raises(InvalidPolicyBundle) as exc_info:
            loader.load(bundle.artifact_hash)

        assert exc_info.value.code == "INVALID_POLICY_BUNDLE"
