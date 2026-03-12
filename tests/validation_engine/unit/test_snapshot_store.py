"""
Testes unitários para SnapshotStore.

Verifica:
- Armazenamento com parâmetros corretos (bucket, key, ContentType, SSE, KMS)
- Idempotência: objeto já existente → put_object NÃO é chamado
- Carga bem-sucedida: objeto inexistente → put_object É chamado
- Erro no put_object → PolicySnapshotUnavailable
- Erro não-404 no head_object → PolicySnapshotUnavailable
- Formato da chave S3: snapshots/{snapshot_version}.json
- Serialização: tuples são convertidas para listas JSON
- Serialização: escalares (int, str, bool) inalterados
- Serialização: determinística (mesma entrada → mesma saída)
- Corpo do put_object é JSON válido

Requisitos cobertos: 3.1, 3.2, 17.3, 20.3
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import botocore.exceptions
import pytest

from validation_engine.domain.errors import PolicySnapshotUnavailable
from validation_engine.domain.models import ReferenceSnapshot
from validation_engine.infrastructure.snapshot_store import SnapshotStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client_error(code: str, operation: str = "HeadObject") -> botocore.exceptions.ClientError:
    """Constrói um ClientError boto3 simulado com o código fornecido."""
    error_response = {"Error": {"Code": code, "Message": f"Simulated {code}"}}
    return botocore.exceptions.ClientError(error_response, operation)


def _make_snapshot(
    snapshot_version: str = "snap_v1.0.0",
    snapshot_schema_version: str = "1.0",
    data: dict | None = None,
) -> ReferenceSnapshot:
    """Constrói um ReferenceSnapshot mínimo para testes."""
    if data is None:
        data = {
            "daily_limit_minor": 100000,
            "blocked_accounts": ("acc_bad_1", "acc_bad_2"),
            "allowed_currencies": ("BRL", "USD"),
            "max_postings": 10,
            "is_feature_enabled": True,
        }
    return ReferenceSnapshot(
        snapshot_version=snapshot_version,
        snapshot_schema_version=snapshot_schema_version,
        created_at="2026-03-11T00:00:00Z",
        data=data,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def s3_mock() -> MagicMock:
    """Cliente S3 mockado para testes unitários."""
    return MagicMock()


@pytest.fixture
def store(s3_mock) -> SnapshotStore:
    """SnapshotStore configurado com bucket e KMS key de teste."""
    return SnapshotStore(
        s3_client=s3_mock,
        bucket_name="test-bundles-bucket",
        kms_key_id="arn:aws:kms:us-east-1:123456789012:key/test-key-id",
    )


@pytest.fixture
def valid_snapshot() -> ReferenceSnapshot:
    """Snapshot válido com dados representativos."""
    return _make_snapshot()


# ---------------------------------------------------------------------------
# Testes de formato da chave S3
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotStoreObjectKey:
    """Verifica o formato da chave S3 gerada pelo store."""

    def test_object_key_format(self, store):
        """A chave deve seguir o formato snapshots/{snapshot_version}.json."""
        key = store._object_key("snap_v1.0.0")
        assert key == "snapshots/snap_v1.0.0.json"

    def test_object_key_prefix_is_snapshots(self, store):
        """A chave deve começar com o prefixo 'snapshots/'."""
        key = store._object_key("any-version")
        assert key.startswith("snapshots/")

    def test_object_key_suffix_is_json(self, store):
        """A chave deve terminar com '.json'."""
        key = store._object_key("any-version")
        assert key.endswith(".json")

    def test_object_key_contains_snapshot_version(self, store):
        """A chave deve conter o snapshot_version exato."""
        version = "snap_2026-03-11_001"
        key = store._object_key(version)
        assert version in key


# ---------------------------------------------------------------------------
# Testes de armazenamento com parâmetros corretos
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotStoreCallsPutObjectWithCorrectParams:
    """Verifica que put_object é chamado com os parâmetros corretos."""

    def test_store_calls_put_object_with_correct_params(self, store, s3_mock, valid_snapshot):
        """
        store() deve chamar put_object com bucket, key, ContentType,
        ServerSideEncryption e SSEKMSKeyId corretos.
        """
        s3_mock.head_object.side_effect = _make_client_error("404")

        store.store(valid_snapshot)

        s3_mock.put_object.assert_called_once()
        call_kwargs = s3_mock.put_object.call_args.kwargs

        assert call_kwargs["Bucket"] == "test-bundles-bucket"
        assert call_kwargs["Key"] == f"snapshots/{valid_snapshot.snapshot_version}.json"
        assert call_kwargs["ContentType"] == "application/json"
        assert call_kwargs["ServerSideEncryption"] == "aws:kms"
        assert call_kwargs["SSEKMSKeyId"] == "arn:aws:kms:us-east-1:123456789012:key/test-key-id"

    def test_store_content_is_valid_json(self, store, s3_mock, valid_snapshot):
        """
        O corpo passado ao put_object deve ser bytes de JSON válido.
        Verifica que o conteúdo pode ser decodificado e parseado como JSON.
        """
        s3_mock.head_object.side_effect = _make_client_error("404")

        store.store(valid_snapshot)

        call_kwargs = s3_mock.put_object.call_args.kwargs
        body_bytes = call_kwargs["Body"]

        # Deve ser bytes
        assert isinstance(body_bytes, bytes)

        # Deve ser JSON válido
        body_str = body_bytes.decode("utf-8")
        parsed = json.loads(body_str)
        assert isinstance(parsed, dict)

        # Deve conter os campos do snapshot
        assert parsed["snapshot_version"] == valid_snapshot.snapshot_version
        assert parsed["snapshot_schema_version"] == valid_snapshot.snapshot_schema_version


# ---------------------------------------------------------------------------
# Testes de idempotência
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotStoreIdempotency:
    """Verifica o comportamento idempotente do store."""

    def test_store_is_idempotent_when_object_exists(self, store, s3_mock, valid_snapshot):
        """
        Quando head_object retorna com sucesso (objeto existe),
        put_object NÃO deve ser chamado — operação idempotente.
        """
        s3_mock.head_object.return_value = {"ContentLength": 2048}

        store.store(valid_snapshot)

        s3_mock.put_object.assert_not_called()

    def test_store_calls_put_object_when_object_does_not_exist(self, store, s3_mock, valid_snapshot):
        """
        Quando head_object levanta 404 (objeto não existe),
        put_object DEVE ser chamado.
        """
        s3_mock.head_object.side_effect = _make_client_error("404")

        store.store(valid_snapshot)

        s3_mock.put_object.assert_called_once()

    def test_store_calls_put_object_when_no_such_key(self, store, s3_mock, valid_snapshot):
        """
        Quando head_object levanta NoSuchKey (objeto não existe),
        put_object DEVE ser chamado.
        """
        s3_mock.head_object.side_effect = _make_client_error("NoSuchKey")

        store.store(valid_snapshot)

        s3_mock.put_object.assert_called_once()

    def test_store_checks_existence_before_writing(self, store, s3_mock, valid_snapshot):
        """
        head_object deve ser chamado antes de put_object para verificar
        idempotência — evita PutObject desnecessário em buckets com Object Lock.
        """
        s3_mock.head_object.side_effect = _make_client_error("404")

        store.store(valid_snapshot)

        s3_mock.head_object.assert_called_once_with(
            Bucket="test-bundles-bucket",
            Key=f"snapshots/{valid_snapshot.snapshot_version}.json",
        )


# ---------------------------------------------------------------------------
# Testes de tratamento de erros
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotStoreErrorHandling:
    """Verifica que erros de I/O são propagados como PolicySnapshotUnavailable."""

    def test_store_raises_snapshot_unavailable_on_put_error(self, store, s3_mock, valid_snapshot):
        """
        Quando put_object levanta ClientError, store() deve levantar
        PolicySnapshotUnavailable com mensagem descritiva.
        """
        s3_mock.head_object.side_effect = _make_client_error("404")
        s3_mock.put_object.side_effect = _make_client_error("500", "PutObject")

        with pytest.raises(PolicySnapshotUnavailable) as exc_info:
            store.store(valid_snapshot)

        assert valid_snapshot.snapshot_version in str(exc_info.value.message)

    def test_store_raises_snapshot_unavailable_on_head_error(self, store, s3_mock, valid_snapshot):
        """
        Quando head_object levanta ClientError não-404 (ex: 403, 500),
        store() deve levantar PolicySnapshotUnavailable.
        """
        s3_mock.head_object.side_effect = _make_client_error("403")

        with pytest.raises(PolicySnapshotUnavailable):
            store.store(valid_snapshot)

    def test_store_raises_snapshot_unavailable_on_head_500(self, store, s3_mock, valid_snapshot):
        """Erro 500 no head_object deve levantar PolicySnapshotUnavailable."""
        s3_mock.head_object.side_effect = _make_client_error("500")

        with pytest.raises(PolicySnapshotUnavailable):
            store.store(valid_snapshot)

    def test_snapshot_unavailable_has_correct_error_code(self, store, s3_mock, valid_snapshot):
        """PolicySnapshotUnavailable deve ter o código de erro correto."""
        s3_mock.head_object.side_effect = _make_client_error("404")
        s3_mock.put_object.side_effect = _make_client_error("500", "PutObject")

        with pytest.raises(PolicySnapshotUnavailable) as exc_info:
            store.store(valid_snapshot)

        assert exc_info.value.code == "POLICY_SNAPSHOT_UNAVAILABLE"

    def test_snapshot_unavailable_has_correct_http_status(self, store, s3_mock, valid_snapshot):
        """PolicySnapshotUnavailable deve ter http_status 503."""
        s3_mock.head_object.side_effect = _make_client_error("404")
        s3_mock.put_object.side_effect = _make_client_error("500", "PutObject")

        with pytest.raises(PolicySnapshotUnavailable) as exc_info:
            store.store(valid_snapshot)

        assert exc_info.value.http_status == 503

    def test_put_not_called_when_head_raises_non_404(self, store, s3_mock, valid_snapshot):
        """
        Quando head_object levanta erro não-404, put_object NÃO deve ser chamado
        — a exceção deve ser propagada antes de tentar escrever.
        """
        s3_mock.head_object.side_effect = _make_client_error("403")

        with pytest.raises(PolicySnapshotUnavailable):
            store.store(valid_snapshot)

        s3_mock.put_object.assert_not_called()


# ---------------------------------------------------------------------------
# Testes de serialização (_serialize)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotStoreSerialization:
    """Verifica a serialização correta de tipos pelo SnapshotStore._serialize."""

    def test_serialize_converts_tuples_to_lists(self, store):
        """
        Tuples (str e int) devem ser convertidas para listas JSON.
        O JSON não distingue listas de tuples — a restauração é feita pelo SnapshotLoader.
        """
        snapshot = _make_snapshot(
            data={
                "blocked_accounts": ("acc_1", "acc_2", "acc_3"),
                "allowed_tiers": (100, 200, 500),
            }
        )

        serialized = store._serialize(snapshot)
        parsed = json.loads(serialized)

        # Tuples de strings → listas de strings no JSON
        assert parsed["data"]["blocked_accounts"] == ["acc_1", "acc_2", "acc_3"]
        assert isinstance(parsed["data"]["blocked_accounts"], list)

        # Tuples de inteiros → listas de inteiros no JSON
        assert parsed["data"]["allowed_tiers"] == [100, 200, 500]
        assert isinstance(parsed["data"]["allowed_tiers"], list)

    def test_serialize_preserves_scalars(self, store):
        """
        Escalares (int, str, bool) devem ser preservados sem transformação
        na serialização JSON.
        """
        snapshot = _make_snapshot(
            data={
                "limit": 50000,
                "currency": "BRL",
                "is_enabled": True,
                "is_blocked": False,
            }
        )

        serialized = store._serialize(snapshot)
        parsed = json.loads(serialized)

        assert parsed["data"]["limit"] == 50000
        assert isinstance(parsed["data"]["limit"], int)

        assert parsed["data"]["currency"] == "BRL"
        assert isinstance(parsed["data"]["currency"], str)

        assert parsed["data"]["is_enabled"] is True
        assert parsed["data"]["is_blocked"] is False

    def test_serialize_is_deterministic(self, store):
        """
        A mesma instância de snapshot serializada duas vezes deve produzir
        exatamente a mesma string — garantia de determinismo para idempotência.
        """
        snapshot = _make_snapshot(
            data={
                "limit": 100000,
                "blocked": ("acc_1", "acc_2"),
                "currency": "BRL",
            }
        )

        first_serialization = store._serialize(snapshot)
        second_serialization = store._serialize(snapshot)

        assert first_serialization == second_serialization

    def test_serialize_includes_all_snapshot_fields(self, store):
        """
        O JSON serializado deve incluir todos os campos do snapshot:
        snapshot_version, snapshot_schema_version, created_at e data.
        """
        snapshot = _make_snapshot(
            snapshot_version="snap_v2.0.0",
            snapshot_schema_version="2.0",
        )

        serialized = store._serialize(snapshot)
        parsed = json.loads(serialized)

        assert parsed["snapshot_version"] == "snap_v2.0.0"
        assert parsed["snapshot_schema_version"] == "2.0"
        assert parsed["created_at"] == "2026-03-11T00:00:00Z"
        assert "data" in parsed

    def test_serialize_empty_tuple_becomes_empty_list(self, store):
        """Tuple vazia deve ser serializada como lista vazia no JSON."""
        snapshot = _make_snapshot(data={"empty_set": ()})

        serialized = store._serialize(snapshot)
        parsed = json.loads(serialized)

        assert parsed["data"]["empty_set"] == []
        assert isinstance(parsed["data"]["empty_set"], list)
