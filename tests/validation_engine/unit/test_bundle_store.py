"""
Testes unitários para BundleStore.

Verifica:
- Armazenamento com parâmetros corretos (bucket, key, ContentType, SSE, KMS)
- Idempotência: objeto já existente → put_object NÃO é chamado
- Carga bem-sucedida: objeto inexistente → put_object É chamado
- Erro no put_object → PolicyBundleUnavailable
- Erro não-404 no head_object → PolicyBundleUnavailable
- Formato da chave S3: bundles/{artifact_hash}.json
- SSE-KMS obrigatório em todos os puts
- Corpo do put_object é JSON UTF-8 válido

Requisitos cobertos: 3.1, 3.2, 17.3, 20.3
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, call

import botocore.exceptions
import pytest

from validation_engine.domain.errors import PolicyBundleUnavailable
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
from validation_engine.infrastructure.bundle_store import BundleStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_error(code: str, operation: str = "HeadObject") -> botocore.exceptions.ClientError:
    """Constrói um ClientError boto3 simulado com o código fornecido."""
    error_response = {"Error": {"Code": code, "Message": f"Simulated {code}"}}
    return botocore.exceptions.ClientError(error_response, operation)


def _make_bundle(artifact_hash: str = "sha256:abc123def456") -> RuleBundle:
    """Constrói um RuleBundle mínimo para testes."""
    rule = PolicyRuleNode(
        name="deny_high_value",
        priority=100,
        condition=ComparisonNode(
            left=FieldAccessNode(path=("facts", "amount_minor")),
            operator=">=",
            right=LiteralNode(value=100000),
        ),
        effect=PolicyEffect.DENY,
        message="Valor acima do limite permitido",
    )
    return RuleBundle(
        policy_set_id="test-policy-set",
        artifact_hash=artifact_hash,
        ast=RuleAST(rules=(rule,)),
        execution_plan={"version": 1, "steps": []},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version="1.0",
            snapshot_schema_version="1.0",
            evaluator_min_version="1.0.0",
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author="test-author",
            description="Bundle de teste",
            compiled_at="2026-03-11T00:00:00Z",
            source_hash="sha256:source",
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def s3_mock() -> MagicMock:
    """Cliente S3 mockado para testes unitários."""
    return MagicMock()


@pytest.fixture
def store(s3_mock) -> BundleStore:
    """BundleStore configurado com bucket e KMS key de teste."""
    return BundleStore(
        s3_client=s3_mock,
        bucket_name="test-bundles-bucket",
        kms_key_id="arn:aws:kms:us-east-1:123456789012:key/test-key-id",
    )


@pytest.fixture
def valid_bundle() -> RuleBundle:
    """Bundle válido com artifact_hash realista."""
    return _make_bundle(artifact_hash="sha256:abc123def456")


# ---------------------------------------------------------------------------
# Testes de formato da chave S3
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleStoreObjectKey:
    """Verifica o formato da chave S3 gerada pelo store."""

    def test_object_key_format(self, store):
        """A chave deve seguir o formato bundles/{artifact_hash}.json."""
        key = store._object_key("sha256:abc123")
        assert key == "bundles/sha256:abc123.json"

    def test_object_key_prefix_is_bundles(self, store):
        """A chave deve começar com o prefixo 'bundles/'."""
        key = store._object_key("any-hash")
        assert key.startswith("bundles/")

    def test_object_key_suffix_is_json(self, store):
        """A chave deve terminar com '.json'."""
        key = store._object_key("any-hash")
        assert key.endswith(".json")

    def test_object_key_contains_artifact_hash(self, store):
        """A chave deve conter o artifact_hash exato."""
        artifact_hash = "sha256:deadbeef1234567890"
        key = store._object_key(artifact_hash)
        assert artifact_hash in key


# ---------------------------------------------------------------------------
# Testes de armazenamento com parâmetros corretos
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleStoreCallsPutObjectWithCorrectParams:
    """Verifica que put_object é chamado com os parâmetros corretos."""

    def test_store_calls_put_object_with_correct_params(self, store, s3_mock, valid_bundle):
        """
        store() deve chamar put_object com bucket, key, ContentType,
        ServerSideEncryption e SSEKMSKeyId corretos.
        """
        # Simula objeto inexistente (head_object levanta 404)
        s3_mock.head_object.side_effect = _make_client_error("404")

        store.store(valid_bundle)

        s3_mock.put_object.assert_called_once()
        call_kwargs = s3_mock.put_object.call_args.kwargs

        assert call_kwargs["Bucket"] == "test-bundles-bucket"
        assert call_kwargs["Key"] == f"bundles/{valid_bundle.artifact_hash}.json"
        assert call_kwargs["ContentType"] == "application/json"
        assert call_kwargs["ServerSideEncryption"] == "aws:kms"
        assert call_kwargs["SSEKMSKeyId"] == "arn:aws:kms:us-east-1:123456789012:key/test-key-id"

    def test_store_uses_sse_kms(self, store, s3_mock, valid_bundle):
        """
        put_object deve sempre usar ServerSideEncryption='aws:kms'
        e incluir SSEKMSKeyId — requisito de segurança obrigatório.
        """
        s3_mock.head_object.side_effect = _make_client_error("404")

        store.store(valid_bundle)

        call_kwargs = s3_mock.put_object.call_args.kwargs
        assert call_kwargs["ServerSideEncryption"] == "aws:kms"
        assert "SSEKMSKeyId" in call_kwargs
        assert call_kwargs["SSEKMSKeyId"] != ""

    def test_store_content_is_utf8_json(self, store, s3_mock, valid_bundle):
        """
        O corpo passado ao put_object deve ser bytes UTF-8 de JSON válido.
        Verifica que o conteúdo pode ser decodificado e parseado como JSON.
        """
        s3_mock.head_object.side_effect = _make_client_error("404")

        store.store(valid_bundle)

        call_kwargs = s3_mock.put_object.call_args.kwargs
        body_bytes = call_kwargs["Body"]

        # Deve ser bytes
        assert isinstance(body_bytes, bytes)

        # Deve ser UTF-8 decodificável
        body_str = body_bytes.decode("utf-8")

        # Deve ser JSON válido
        parsed = json.loads(body_str)
        assert isinstance(parsed, dict)

        # Deve conter os campos do bundle
        assert parsed["policy_set_id"] == valid_bundle.policy_set_id
        assert parsed["artifact_hash"] == valid_bundle.artifact_hash


# ---------------------------------------------------------------------------
# Testes de idempotência
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleStoreIdempotency:
    """Verifica o comportamento idempotente do store."""

    def test_store_is_idempotent_when_object_exists(self, store, s3_mock, valid_bundle):
        """
        Quando head_object retorna com sucesso (objeto existe),
        put_object NÃO deve ser chamado — operação idempotente.
        """
        # head_object retorna sem erro → objeto já existe
        s3_mock.head_object.return_value = {"ContentLength": 1024}

        store.store(valid_bundle)

        s3_mock.put_object.assert_not_called()

    def test_store_calls_put_object_when_object_does_not_exist(self, store, s3_mock, valid_bundle):
        """
        Quando head_object levanta 404 (objeto não existe),
        put_object DEVE ser chamado.
        """
        s3_mock.head_object.side_effect = _make_client_error("404")

        store.store(valid_bundle)

        s3_mock.put_object.assert_called_once()

    def test_store_calls_put_object_when_no_such_key(self, store, s3_mock, valid_bundle):
        """
        Quando head_object levanta NoSuchKey (objeto não existe),
        put_object DEVE ser chamado.
        """
        s3_mock.head_object.side_effect = _make_client_error("NoSuchKey")

        store.store(valid_bundle)

        s3_mock.put_object.assert_called_once()

    def test_store_checks_existence_before_writing(self, store, s3_mock, valid_bundle):
        """
        head_object deve ser chamado antes de put_object para verificar
        idempotência — evita PutObject desnecessário em buckets com Object Lock.
        """
        s3_mock.head_object.side_effect = _make_client_error("404")

        store.store(valid_bundle)

        # Verifica que head_object foi chamado com os parâmetros corretos
        s3_mock.head_object.assert_called_once_with(
            Bucket="test-bundles-bucket",
            Key=f"bundles/{valid_bundle.artifact_hash}.json",
        )


# ---------------------------------------------------------------------------
# Testes de tratamento de erros
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBundleStoreErrorHandling:
    """Verifica que erros de I/O são propagados como PolicyBundleUnavailable."""

    def test_store_raises_bundle_unavailable_on_put_error(self, store, s3_mock, valid_bundle):
        """
        Quando put_object levanta ClientError, store() deve levantar
        PolicyBundleUnavailable com mensagem descritiva.
        """
        s3_mock.head_object.side_effect = _make_client_error("404")
        s3_mock.put_object.side_effect = _make_client_error("500", "PutObject")

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            store.store(valid_bundle)

        # A mensagem deve identificar o bundle problemático
        assert valid_bundle.artifact_hash in str(exc_info.value.message)

    def test_store_raises_bundle_unavailable_on_head_error(self, store, s3_mock, valid_bundle):
        """
        Quando head_object levanta ClientError não-404 (ex: 403, 500),
        store() deve levantar PolicyBundleUnavailable.
        """
        s3_mock.head_object.side_effect = _make_client_error("403")

        with pytest.raises(PolicyBundleUnavailable):
            store.store(valid_bundle)

    def test_store_raises_bundle_unavailable_on_head_500(self, store, s3_mock, valid_bundle):
        """Erro 500 no head_object deve levantar PolicyBundleUnavailable."""
        s3_mock.head_object.side_effect = _make_client_error("500")

        with pytest.raises(PolicyBundleUnavailable):
            store.store(valid_bundle)

    def test_bundle_unavailable_has_correct_error_code(self, store, s3_mock, valid_bundle):
        """PolicyBundleUnavailable deve ter o código de erro correto."""
        s3_mock.head_object.side_effect = _make_client_error("404")
        s3_mock.put_object.side_effect = _make_client_error("500", "PutObject")

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            store.store(valid_bundle)

        assert exc_info.value.code == "POLICY_BUNDLE_UNAVAILABLE"

    def test_bundle_unavailable_has_correct_http_status(self, store, s3_mock, valid_bundle):
        """PolicyBundleUnavailable deve ter http_status 503."""
        s3_mock.head_object.side_effect = _make_client_error("404")
        s3_mock.put_object.side_effect = _make_client_error("500", "PutObject")

        with pytest.raises(PolicyBundleUnavailable) as exc_info:
            store.store(valid_bundle)

        assert exc_info.value.http_status == 503

    def test_put_not_called_when_head_raises_non_404(self, store, s3_mock, valid_bundle):
        """
        Quando head_object levanta erro não-404, put_object NÃO deve ser chamado
        — a exceção deve ser propagada antes de tentar escrever.
        """
        s3_mock.head_object.side_effect = _make_client_error("403")

        with pytest.raises(PolicyBundleUnavailable):
            store.store(valid_bundle)

        s3_mock.put_object.assert_not_called()
