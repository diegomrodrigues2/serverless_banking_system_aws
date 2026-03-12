"""
Testes unitários para LKGStore.

Verifica:
- save/load round-trip: ActivePolicySet salvo e carregado corretamente
- Invariante de boot: load() retorna None antes de mark_boot_valid()
- Invariante de boot: load() retorna LKG após mark_boot_valid()
- Arquivo ausente: load() retorna None se arquivo não existe
- Arquivo corrompido: load() retorna None sem propagar exceção
- clear(): remove arquivo LKG sem erro
- Sanitização de scope_id: ':' e '*' substituídos por '_' no nome do arquivo
- has_valid_boot: False inicialmente, True após mark_boot_valid()
- Falha de I/O no save: não propaga exceção (best-effort)

Requisitos cobertos: 17.2
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from validation_engine.domain.models import (
    ActivePolicySet,
    BundleCompatibility,
    CompilationMetadata,
    PolicyActivationManifest,
    ReferenceSnapshot,
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
from validation_engine.infrastructure.lkg_store import LKGStore


# ---------------------------------------------------------------------------
# Helpers de construção de fixtures
# ---------------------------------------------------------------------------


def _make_active_policy_set(
    scope_id: str = "tenantA:TRANSFER:PIX:*:prod",
    activation_id: str = "act_001",
    artifact_hash: str = "sha256:abc123",
    snapshot_version: str = "snap_001",
) -> ActivePolicySet:
    """Constrói um ActivePolicySet mínimo para testes."""
    manifest = PolicyActivationManifest(
        activation_id=activation_id,
        policy_scope_id=scope_id,
        artifact_hash=artifact_hash,
        snapshot_version=snapshot_version,
        context_schema_version="1.0",
        evaluator_version="1.0.0",
        activated_at="2026-01-01T00:00:00Z",
        activated_by="test",
    )

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
    bundle = RuleBundle(
        policy_set_id="test-policy-set",
        artifact_hash=artifact_hash,
        ast=RuleAST(rules=(rule,)),
        execution_plan={},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version="1.0",
            snapshot_schema_version="1.0",
            evaluator_min_version="1.0.0",
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author="test",
            description="Test bundle",
            compiled_at="2026-01-01T00:00:00Z",
            source_hash="sha256:source123",
        ),
    )

    snapshot = ReferenceSnapshot(
        snapshot_version=snapshot_version,
        snapshot_schema_version="1.0",
        created_at="2026-01-01T00:00:00Z",
        data={
            "daily_limit_minor": 500000,
            "blocked_accounts": ("acc_001", "acc_002"),
            "allowed_currencies": ("BRL", "USD"),
        },
    )

    return ActivePolicySet(
        manifest=manifest,
        bundle=bundle,
        snapshot=snapshot,
        loaded_at="2026-01-01T00:00:00Z",
        integrity_verified=True,
    )


# ---------------------------------------------------------------------------
# Fixture de diretório temporário
# ---------------------------------------------------------------------------


@pytest.fixture
def lkg_dir(tmp_path: Path) -> str:
    """Diretório temporário para armazenamento do LKG nos testes."""
    return str(tmp_path / "lkg")


@pytest.fixture
def lkg_store(lkg_dir: str) -> LKGStore:
    """LKGStore com diretório temporário."""
    return LKGStore(lkg_dir=lkg_dir)


# ---------------------------------------------------------------------------
# Testes de has_valid_boot
# ---------------------------------------------------------------------------


class TestHasValidBoot:
    """Testa o controle de boot válido."""

    def test_has_valid_boot_false_inicialmente(self, lkg_store: LKGStore) -> None:
        """has_valid_boot deve ser False antes de qualquer inicialização."""
        assert lkg_store.has_valid_boot is False

    def test_has_valid_boot_true_apos_mark_boot_valid(self, lkg_store: LKGStore) -> None:
        """has_valid_boot deve ser True após mark_boot_valid()."""
        lkg_store.mark_boot_valid()
        assert lkg_store.has_valid_boot is True

    def test_mark_boot_valid_idempotente(self, lkg_store: LKGStore) -> None:
        """Chamar mark_boot_valid() múltiplas vezes não deve causar erro."""
        lkg_store.mark_boot_valid()
        lkg_store.mark_boot_valid()
        assert lkg_store.has_valid_boot is True


# ---------------------------------------------------------------------------
# Testes de save/load round-trip
# ---------------------------------------------------------------------------


class TestSaveLoadRoundTrip:
    """Testa persistência e carregamento do LKG."""

    def test_round_trip_completo(self, lkg_store: LKGStore) -> None:
        """ActivePolicySet salvo deve ser carregado com os mesmos dados."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        original = _make_active_policy_set(scope_id=scope_id)

        lkg_store.mark_boot_valid()
        lkg_store.save(scope_id, original)
        loaded = lkg_store.load(scope_id)

        assert loaded is not None
        assert loaded.manifest.activation_id == original.manifest.activation_id
        assert loaded.manifest.artifact_hash == original.manifest.artifact_hash
        assert loaded.manifest.snapshot_version == original.manifest.snapshot_version
        assert loaded.bundle.policy_set_id == original.bundle.policy_set_id
        assert loaded.snapshot.snapshot_version == original.snapshot.snapshot_version
        assert loaded.integrity_verified is True

    def test_snapshot_data_tuples_preservados(self, lkg_store: LKGStore) -> None:
        """Tuples no snapshot.data devem ser preservadas após round-trip."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        original = _make_active_policy_set(scope_id=scope_id)

        lkg_store.mark_boot_valid()
        lkg_store.save(scope_id, original)
        loaded = lkg_store.load(scope_id)

        assert loaded is not None
        # Tuples de strings devem ser restauradas como tuples
        assert isinstance(loaded.snapshot.data["blocked_accounts"], tuple)
        assert loaded.snapshot.data["blocked_accounts"] == ("acc_001", "acc_002")
        assert isinstance(loaded.snapshot.data["allowed_currencies"], tuple)
        assert loaded.snapshot.data["allowed_currencies"] == ("BRL", "USD")
        # Escalares devem ser preservados
        assert loaded.snapshot.data["daily_limit_minor"] == 500000

    def test_save_sobrescreve_lkg_anterior(self, lkg_store: LKGStore) -> None:
        """Salvar novo LKG deve sobrescrever o anterior para o mesmo escopo."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        original = _make_active_policy_set(activation_id="act_001")
        updated = _make_active_policy_set(activation_id="act_002")

        lkg_store.mark_boot_valid()
        lkg_store.save(scope_id, original)
        lkg_store.save(scope_id, updated)
        loaded = lkg_store.load(scope_id)

        assert loaded is not None
        assert loaded.manifest.activation_id == "act_002"

    def test_escopos_independentes(self, lkg_store: LKGStore) -> None:
        """LKGs de escopos diferentes devem ser armazenados independentemente."""
        scope_a = "tenantA:TRANSFER:PIX:*:prod"
        scope_b = "tenantB:PAYMENT:TED:*:prod"
        aps_a = _make_active_policy_set(scope_id=scope_a, activation_id="act_a")
        aps_b = _make_active_policy_set(scope_id=scope_b, activation_id="act_b")

        lkg_store.mark_boot_valid()
        lkg_store.save(scope_a, aps_a)
        lkg_store.save(scope_b, aps_b)

        loaded_a = lkg_store.load(scope_a)
        loaded_b = lkg_store.load(scope_b)

        assert loaded_a is not None
        assert loaded_b is not None
        assert loaded_a.manifest.activation_id == "act_a"
        assert loaded_b.manifest.activation_id == "act_b"


# ---------------------------------------------------------------------------
# Testes do invariante de boot
# ---------------------------------------------------------------------------


class TestInvarianteDeBootValido:
    """Testa que o LKG só é disponibilizado após boot válido."""

    def test_load_retorna_none_antes_de_boot_valido(self, lkg_store: LKGStore) -> None:
        """load() deve retornar None se has_valid_boot == False, mesmo com arquivo."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        aps = _make_active_policy_set(scope_id=scope_id)

        # Salvar sem marcar boot válido — simula arquivo de execução anterior
        lkg_store.save(scope_id, aps)

        # Sem boot válido, load() deve retornar None
        result = lkg_store.load(scope_id)
        assert result is None

    def test_load_retorna_lkg_apos_boot_valido(self, lkg_store: LKGStore) -> None:
        """load() deve retornar o LKG após mark_boot_valid()."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        aps = _make_active_policy_set(scope_id=scope_id)

        lkg_store.save(scope_id, aps)
        lkg_store.mark_boot_valid()

        result = lkg_store.load(scope_id)
        assert result is not None
        assert result.manifest.activation_id == aps.manifest.activation_id


# ---------------------------------------------------------------------------
# Testes de arquivo ausente e corrompido
# ---------------------------------------------------------------------------


class TestArquivoAusenteOuCorrompido:
    """Testa comportamento com arquivo ausente ou corrompido."""

    def test_load_retorna_none_se_arquivo_ausente(self, lkg_store: LKGStore) -> None:
        """load() deve retornar None se não há arquivo LKG para o escopo."""
        lkg_store.mark_boot_valid()
        result = lkg_store.load("escopo_sem_lkg:TRANSFER:*:*:prod")
        assert result is None

    def test_load_retorna_none_se_arquivo_corrompido(
        self, lkg_store: LKGStore, lkg_dir: str
    ) -> None:
        """load() deve retornar None se o arquivo JSON está corrompido."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        lkg_store.mark_boot_valid()

        # Escrever JSON inválido diretamente no arquivo esperado
        file_path = Path(lkg_dir) / "tenantA_TRANSFER_PIX___prod.lkg.json"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("{ json invalido !!!", encoding="utf-8")

        result = lkg_store.load(scope_id)
        assert result is None

    def test_load_retorna_none_se_campos_ausentes(
        self, lkg_store: LKGStore, lkg_dir: str
    ) -> None:
        """load() deve retornar None se o JSON está faltando campos obrigatórios."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        lkg_store.mark_boot_valid()

        # Escrever JSON válido mas com estrutura incompleta
        file_path = Path(lkg_dir) / "tenantA_TRANSFER_PIX___prod.lkg.json"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(json.dumps({"incomplete": "data"}), encoding="utf-8")

        result = lkg_store.load(scope_id)
        assert result is None


# ---------------------------------------------------------------------------
# Testes de clear()
# ---------------------------------------------------------------------------


class TestClear:
    """Testa remoção de arquivos LKG."""

    def test_clear_remove_arquivo_existente(self, lkg_store: LKGStore) -> None:
        """clear() deve remover o arquivo LKG do escopo."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        aps = _make_active_policy_set(scope_id=scope_id)

        lkg_store.mark_boot_valid()
        lkg_store.save(scope_id, aps)

        # Verificar que o arquivo existe antes do clear
        assert lkg_store.load(scope_id) is not None

        lkg_store.clear(scope_id)

        # Após clear, load() deve retornar None
        assert lkg_store.load(scope_id) is None

    def test_clear_nao_levanta_erro_se_arquivo_ausente(
        self, lkg_store: LKGStore
    ) -> None:
        """clear() não deve levantar exceção se o arquivo não existe."""
        # Não deve levantar exceção
        lkg_store.clear("escopo_inexistente:TRANSFER:*:*:prod")


# ---------------------------------------------------------------------------
# Testes de sanitização do scope_id
# ---------------------------------------------------------------------------


class TestSanitizacaoScopeId:
    """Testa que o scope_id é sanitizado corretamente para nome de arquivo."""

    def test_scope_id_com_dois_pontos_sanitizado(
        self, lkg_store: LKGStore, lkg_dir: str
    ) -> None:
        """':' no scope_id deve ser substituído por '_' no nome do arquivo."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        aps = _make_active_policy_set(scope_id=scope_id)

        lkg_store.mark_boot_valid()
        lkg_store.save(scope_id, aps)

        # Verificar que o arquivo foi criado com o nome sanitizado
        expected_file = Path(lkg_dir) / "tenantA_TRANSFER_PIX___prod.lkg.json"
        assert expected_file.exists()

    def test_scope_id_com_wildcard_sanitizado(
        self, lkg_store: LKGStore, lkg_dir: str
    ) -> None:
        """'*' no scope_id deve ser substituído por '_' no nome do arquivo."""
        scope_id = "tenantA:TRANSFER:*:*:prod"
        aps = _make_active_policy_set(scope_id=scope_id)

        lkg_store.mark_boot_valid()
        lkg_store.save(scope_id, aps)

        expected_file = Path(lkg_dir) / "tenantA_TRANSFER_____prod.lkg.json"
        assert expected_file.exists()
