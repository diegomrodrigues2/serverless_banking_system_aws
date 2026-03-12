"""
Testes unitários para SnapshotLoader.

Verifica:
- Cache hit: snapshot servido do cache sem I/O ao S3
- Cache miss: snapshot carregado do S3, verificado e armazenado em cache
- Desserialização: listas JSON → tuples Python (str e int)
- Desserialização: escalares (int, str, bool) inalterados
- Desserialização: lista vazia → tuple vazia
- Compatibilidade: snapshot_schema_version incompatível levanta PolicySnapshotUnavailable
- Indisponibilidade: objeto não encontrado no S3 levanta PolicySnapshotUnavailable
- Indisponibilidade: erro de I/O no S3 levanta PolicySnapshotUnavailable
- Invalidação: invalidate() remove snapshot do cache
- Chave S3: formato correto snapshots/{snapshot_version}.json

Requisitos cobertos: 3.3, 3.4, 11.6, 17.3, 20.3, 20.4
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from validation_engine.domain.errors import PolicySnapshotUnavailable
from validation_engine.domain.models import ReferenceSnapshot
from validation_engine.infrastructure.snapshot_loader import SnapshotLoader, _restore_tuple_type


# ---------------------------------------------------------------------------
# Helpers de construção de snapshot e payload S3
# ---------------------------------------------------------------------------


def _make_snapshot(
    snapshot_version: str = "snap_001",
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


def _serialize_snapshot(snapshot: ReferenceSnapshot) -> str:
    """
    Serializa um snapshot para JSON, convertendo tuples para listas.

    Replica o comportamento do SnapshotStore._serialize().
    """
    serializable_data = {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in snapshot.data.items()
    }
    payload = {
        "snapshot_version": snapshot.snapshot_version,
        "snapshot_schema_version": snapshot.snapshot_schema_version,
        "created_at": snapshot.created_at,
        "data": serializable_data,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


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
def loader(s3_mock) -> SnapshotLoader:
    """SnapshotLoader configurado com schema version padrão."""
    return SnapshotLoader(
        s3_client=s3_mock,
        bucket_name="test-bucket",
        expected_snapshot_schema_version="1.0",
    )


@pytest.fixture
def valid_snapshot() -> ReferenceSnapshot:
    """Snapshot compatível com o loader padrão (schema version 1.0)."""
    return _make_snapshot(snapshot_schema_version="1.0")


# ---------------------------------------------------------------------------
# Testes de chave S3
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotLoaderObjectKey:
    """Verifica o formato da chave S3 gerada pelo loader."""

    def test_key_format(self, loader):
        key = loader._object_key("snap_001")
        assert key == "snapshots/snap_001.json"

    def test_key_prefix_is_snapshots(self, loader):
        key = loader._object_key("any-version")
        assert key.startswith("snapshots/")

    def test_key_suffix_is_json(self, loader):
        key = loader._object_key("any-version")
        assert key.endswith(".json")


# ---------------------------------------------------------------------------
# Testes de cache hit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotLoaderCacheHit:
    """Verifica que snapshots em cache são servidos sem I/O ao S3."""

    def test_cache_hit_returns_same_snapshot(self, loader, s3_mock, valid_snapshot):
        """Segundo load() deve retornar o mesmo snapshot sem chamar S3."""
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(valid_snapshot)
        )

        first = loader.load(valid_snapshot.snapshot_version)
        second = loader.load(valid_snapshot.snapshot_version)

        assert first == second
        assert s3_mock.get_object.call_count == 1

    def test_cache_hit_does_not_call_s3(self, loader, s3_mock, valid_snapshot):
        """Após cache miss, o segundo load não deve chamar get_object."""
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(valid_snapshot)
        )

        loader.load(valid_snapshot.snapshot_version)
        s3_mock.get_object.reset_mock()

        loader.load(valid_snapshot.snapshot_version)
        s3_mock.get_object.assert_not_called()


# ---------------------------------------------------------------------------
# Testes de cache miss (carga bem-sucedida)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotLoaderCacheMiss:
    """Verifica carga bem-sucedida do S3 com verificação de schema."""

    def test_load_returns_correct_snapshot(self, loader, s3_mock, valid_snapshot):
        """load() deve retornar o snapshot correto após carga do S3."""
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(valid_snapshot)
        )

        result = loader.load(valid_snapshot.snapshot_version)

        assert result.snapshot_version == valid_snapshot.snapshot_version
        assert result.snapshot_schema_version == valid_snapshot.snapshot_schema_version

    def test_load_calls_s3_with_correct_key(self, loader, s3_mock, valid_snapshot):
        """load() deve chamar get_object com a chave correta."""
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(valid_snapshot)
        )

        loader.load(valid_snapshot.snapshot_version)

        s3_mock.get_object.assert_called_once_with(
            Bucket="test-bucket",
            Key=f"snapshots/{valid_snapshot.snapshot_version}.json",
        )

    def test_load_stores_snapshot_in_cache(self, loader, s3_mock, valid_snapshot):
        """Após carga bem-sucedida, o snapshot deve estar no cache."""
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(valid_snapshot)
        )

        loader.load(valid_snapshot.snapshot_version)

        assert valid_snapshot.snapshot_version in loader._cache


# ---------------------------------------------------------------------------
# Testes de desserialização — restauração de tipos
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotLoaderDeserialization:
    """Verifica a restauração correta de tipos na desserialização."""

    def test_list_of_strings_becomes_tuple_of_strings(self, loader, s3_mock):
        """Listas de strings no JSON devem ser restauradas como tuple[str, ...]."""
        snapshot = _make_snapshot(
            data={"blocked_accounts": ("acc_1", "acc_2", "acc_3")}
        )
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(snapshot)
        )

        result = loader.load(snapshot.snapshot_version)

        blocked = result.data["blocked_accounts"]
        assert isinstance(blocked, tuple)
        assert all(isinstance(item, str) for item in blocked)
        assert blocked == ("acc_1", "acc_2", "acc_3")

    def test_list_of_ints_becomes_tuple_of_ints(self, loader, s3_mock):
        """Listas de inteiros no JSON devem ser restauradas como tuple[int, ...]."""
        snapshot = _make_snapshot(
            data={"allowed_amounts": (100, 200, 500, 1000)}
        )
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(snapshot)
        )

        result = loader.load(snapshot.snapshot_version)

        amounts = result.data["allowed_amounts"]
        assert isinstance(amounts, tuple)
        assert all(isinstance(item, int) for item in amounts)
        assert amounts == (100, 200, 500, 1000)

    def test_integer_scalar_unchanged(self, loader, s3_mock):
        """Escalares inteiros devem ser preservados sem transformação."""
        snapshot = _make_snapshot(data={"daily_limit": 100000})
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(snapshot)
        )

        result = loader.load(snapshot.snapshot_version)

        assert result.data["daily_limit"] == 100000
        assert isinstance(result.data["daily_limit"], int)

    def test_string_scalar_unchanged(self, loader, s3_mock):
        """Escalares string devem ser preservados sem transformação."""
        snapshot = _make_snapshot(data={"currency": "BRL"})
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(snapshot)
        )

        result = loader.load(snapshot.snapshot_version)

        assert result.data["currency"] == "BRL"
        assert isinstance(result.data["currency"], str)

    def test_bool_scalar_unchanged(self, loader, s3_mock):
        """Escalares booleanos devem ser preservados sem transformação."""
        snapshot = _make_snapshot(data={"is_enabled": True})
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(snapshot)
        )

        result = loader.load(snapshot.snapshot_version)

        assert result.data["is_enabled"] is True
        assert isinstance(result.data["is_enabled"], bool)

    def test_empty_list_becomes_empty_tuple(self, loader, s3_mock):
        """Lista vazia no JSON deve ser restaurada como tuple vazia."""
        snapshot = _make_snapshot(data={"empty_list": ()})
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(snapshot)
        )

        result = loader.load(snapshot.snapshot_version)

        assert result.data["empty_list"] == ()
        assert isinstance(result.data["empty_list"], tuple)

    def test_mixed_data_types_all_restored_correctly(self, loader, s3_mock):
        """Snapshot com múltiplos tipos deve ter todos restaurados corretamente."""
        snapshot = _make_snapshot(
            data={
                "limit": 50000,
                "currency": "BRL",
                "enabled": False,
                "blocked": ("acc_1", "acc_2"),
                "tiers": (100, 200, 300),
            }
        )
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(snapshot)
        )

        result = loader.load(snapshot.snapshot_version)

        assert result.data["limit"] == 50000
        assert result.data["currency"] == "BRL"
        assert result.data["enabled"] is False
        assert result.data["blocked"] == ("acc_1", "acc_2")
        assert result.data["tiers"] == (100, 200, 300)


# ---------------------------------------------------------------------------
# Testes de verificação de compatibilidade de schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotLoaderSchemaCompatibility:
    """Verifica que schema incompatível levanta PolicySnapshotUnavailable."""

    def test_incompatible_schema_version_raises(self, s3_mock):
        """
        Snapshot com snapshot_schema_version diferente do esperado deve levantar
        PolicySnapshotUnavailable com mensagem clara.
        """
        loader = SnapshotLoader(
            s3_client=s3_mock,
            bucket_name="test-bucket",
            expected_snapshot_schema_version="1.0",
        )

        # Snapshot com schema version 2.0 — incompatível com runtime que espera 1.0
        snapshot = _make_snapshot(snapshot_schema_version="2.0")
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(snapshot)
        )

        with pytest.raises(PolicySnapshotUnavailable) as exc_info:
            loader.load(snapshot.snapshot_version)

        error_message = str(exc_info.value.message)
        assert "2.0" in error_message
        assert "1.0" in error_message

    def test_incompatible_schema_does_not_cache(self, s3_mock):
        """Snapshot com schema incompatível não deve ser armazenado no cache."""
        loader = SnapshotLoader(
            s3_client=s3_mock,
            bucket_name="test-bucket",
            expected_snapshot_schema_version="1.0",
        )

        snapshot = _make_snapshot(snapshot_schema_version="2.0")
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(snapshot)
        )

        with pytest.raises(PolicySnapshotUnavailable):
            loader.load(snapshot.snapshot_version)

        assert snapshot.snapshot_version not in loader._cache

    def test_compatible_schema_passes(self, loader, s3_mock, valid_snapshot):
        """Snapshot com schema compatível deve ser carregado sem erro."""
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(valid_snapshot)
        )

        result = loader.load(valid_snapshot.snapshot_version)
        assert result.snapshot_schema_version == "1.0"

    def test_incompatible_schema_error_code(self, s3_mock):
        """PolicySnapshotUnavailable deve ter o código correto."""
        loader = SnapshotLoader(
            s3_client=s3_mock,
            bucket_name="test-bucket",
            expected_snapshot_schema_version="1.0",
        )

        snapshot = _make_snapshot(snapshot_schema_version="99.0")
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(snapshot)
        )

        with pytest.raises(PolicySnapshotUnavailable) as exc_info:
            loader.load(snapshot.snapshot_version)

        assert exc_info.value.code == "POLICY_SNAPSHOT_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Testes de indisponibilidade do S3
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotLoaderS3Unavailability:
    """Verifica que erros de I/O do S3 levantam PolicySnapshotUnavailable."""

    def test_not_found_raises_snapshot_unavailable(self, loader, s3_mock):
        """Objeto não encontrado (404) deve levantar PolicySnapshotUnavailable."""
        s3_mock.get_object.side_effect = _make_client_error("404")

        with pytest.raises(PolicySnapshotUnavailable) as exc_info:
            loader.load("snap_nonexistent")

        assert "snap_nonexistent" in str(exc_info.value.message)

    def test_no_such_key_raises_snapshot_unavailable(self, loader, s3_mock):
        """NoSuchKey deve levantar PolicySnapshotUnavailable."""
        s3_mock.get_object.side_effect = _make_client_error("NoSuchKey")

        with pytest.raises(PolicySnapshotUnavailable):
            loader.load("snap_nonexistent")

    def test_io_error_raises_snapshot_unavailable(self, loader, s3_mock):
        """Erro de I/O genérico deve levantar PolicySnapshotUnavailable."""
        s3_mock.get_object.side_effect = _make_client_error("500")

        with pytest.raises(PolicySnapshotUnavailable):
            loader.load("snap_any")

    def test_unavailable_snapshot_not_cached(self, loader, s3_mock):
        """Snapshot indisponível não deve ser armazenado no cache."""
        s3_mock.get_object.side_effect = _make_client_error("404")

        with pytest.raises(PolicySnapshotUnavailable):
            loader.load("snap_nonexistent")

        assert "snap_nonexistent" not in loader._cache

    def test_snapshot_unavailable_has_correct_code(self, loader, s3_mock):
        """PolicySnapshotUnavailable deve ter o código correto."""
        s3_mock.get_object.side_effect = _make_client_error("404")

        with pytest.raises(PolicySnapshotUnavailable) as exc_info:
            loader.load("snap_any")

        assert exc_info.value.code == "POLICY_SNAPSHOT_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Testes de invalidação de cache
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotLoaderInvalidation:
    """Verifica que invalidate() remove snapshots do cache corretamente."""

    def test_invalidate_removes_snapshot_from_cache(self, loader, s3_mock, valid_snapshot):
        """Após invalidate(), o snapshot deve ser removido do cache."""
        s3_mock.get_object.return_value = _make_s3_response(
            _serialize_snapshot(valid_snapshot)
        )
        loader.load(valid_snapshot.snapshot_version)

        assert valid_snapshot.snapshot_version in loader._cache

        loader.invalidate(valid_snapshot.snapshot_version)

        assert valid_snapshot.snapshot_version not in loader._cache

    def test_invalidate_forces_s3_reload(self, loader, s3_mock, valid_snapshot):
        """Após invalidate(), o próximo load() deve buscar do S3 novamente."""
        # Retorna um novo BytesIO a cada chamada para evitar EOF no segundo read
        json_content = _serialize_snapshot(valid_snapshot)
        s3_mock.get_object.side_effect = lambda **kwargs: _make_s3_response(json_content)

        loader.load(valid_snapshot.snapshot_version)
        loader.invalidate(valid_snapshot.snapshot_version)
        loader.load(valid_snapshot.snapshot_version)

        assert s3_mock.get_object.call_count == 2

    def test_invalidate_nonexistent_key_does_not_raise(self, loader):
        """invalidate() de chave não presente no cache não deve levantar exceção."""
        loader.invalidate("snap_not_in_cache")


# ---------------------------------------------------------------------------
# Testes unitários da função auxiliar _restore_tuple_type
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRestoreTupleType:
    """Testes unitários para a função auxiliar _restore_tuple_type."""

    def test_list_of_strings_returns_tuple_of_strings(self):
        result = _restore_tuple_type(["a", "b", "c"])
        assert result == ("a", "b", "c")
        assert isinstance(result, tuple)

    def test_list_of_ints_returns_tuple_of_ints(self):
        result = _restore_tuple_type([1, 2, 3])
        assert result == (1, 2, 3)
        assert isinstance(result, tuple)

    def test_empty_list_returns_empty_tuple(self):
        result = _restore_tuple_type([])
        assert result == ()
        assert isinstance(result, tuple)

    def test_int_scalar_unchanged(self):
        result = _restore_tuple_type(42)
        assert result == 42
        assert isinstance(result, int)

    def test_string_scalar_unchanged(self):
        result = _restore_tuple_type("hello")
        assert result == "hello"
        assert isinstance(result, str)

    def test_bool_scalar_unchanged(self):
        result = _restore_tuple_type(True)
        assert result is True

    def test_bool_false_scalar_unchanged(self):
        result = _restore_tuple_type(False)
        assert result is False

    def test_single_string_list_returns_single_element_tuple(self):
        result = _restore_tuple_type(["only_one"])
        assert result == ("only_one",)

    def test_single_int_list_returns_single_element_tuple(self):
        result = _restore_tuple_type([99])
        assert result == (99,)
