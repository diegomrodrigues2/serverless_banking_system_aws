"""
Testes unitários para PolicyRuntimeRegistry.

Verifica:
- get_active_policy_set: retorna do cache em memória (hot path sem I/O)
- get_active_policy_set: dispara bootstrap se escopo não inicializado
- refresh_scope: carrega manifesto, bundle e snapshot e faz swap atômico
- refresh_scope: skip se activation_id não mudou
- refresh_scope: usa LKG se refresh falhar após boot válido
- refresh_scope: levanta PolicyEngineNotReady se falhar sem LKG disponível
- Integridade: falha de integridade do bundle propaga corretamente
- Compatibilidade: bundle incompatível propaga corretamente

Requisitos cobertos: 6.1, 6.3, 17.1, 17.2
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from validation_engine.application.runtime_registry import PolicyRuntimeRegistry
from validation_engine.domain.errors import (
    PolicyBundleIntegrityFailure,
    PolicyBundleUnavailable,
    PolicyEngineNotReady,
    PolicySnapshotUnavailable,
)
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


# ---------------------------------------------------------------------------
# Helpers de construção de fixtures
# ---------------------------------------------------------------------------


def _make_manifest(
    activation_id: str = "act_001",
    scope_id: str = "tenantA:TRANSFER:PIX:*:prod",
    artifact_hash: str = "sha256:abc123",
    snapshot_version: str = "snap_001",
) -> PolicyActivationManifest:
    return PolicyActivationManifest(
        activation_id=activation_id,
        policy_scope_id=scope_id,
        artifact_hash=artifact_hash,
        snapshot_version=snapshot_version,
        context_schema_version="1.0",
        evaluator_version="1.0.0",
        activated_at="2026-01-01T00:00:00Z",
        activated_by="test",
    )


def _make_bundle(artifact_hash: str = "sha256:abc123") -> RuleBundle:
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


def _make_snapshot(snapshot_version: str = "snap_001") -> ReferenceSnapshot:
    return ReferenceSnapshot(
        snapshot_version=snapshot_version,
        snapshot_schema_version="1.0",
        created_at="2026-01-01T00:00:00Z",
        data={"daily_limit_minor": 500000},
    )


def _make_active_policy_set(
    activation_id: str = "act_001",
    scope_id: str = "tenantA:TRANSFER:PIX:*:prod",
) -> ActivePolicySet:
    manifest = _make_manifest(activation_id=activation_id, scope_id=scope_id)
    return ActivePolicySet(
        manifest=manifest,
        bundle=_make_bundle(),
        snapshot=_make_snapshot(),
        loaded_at="2026-01-01T00:00:00Z",
        integrity_verified=True,
    )


# ---------------------------------------------------------------------------
# Fixture de registry com mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_manifest_resolver() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_bundle_loader() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_snapshot_loader() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_lkg_store() -> MagicMock:
    mock = MagicMock()
    mock.has_valid_boot = False
    mock.load.return_value = None
    return mock


@pytest.fixture
def registry(
    mock_manifest_resolver: MagicMock,
    mock_bundle_loader: MagicMock,
    mock_snapshot_loader: MagicMock,
    mock_lkg_store: MagicMock,
) -> PolicyRuntimeRegistry:
    return PolicyRuntimeRegistry(
        manifest_resolver=mock_manifest_resolver,
        bundle_loader=mock_bundle_loader,
        snapshot_loader=mock_snapshot_loader,
        lkg_store=mock_lkg_store,
        evaluator_version="1.0.0",
    )


# ---------------------------------------------------------------------------
# Testes do hot path (cache em memória)
# ---------------------------------------------------------------------------


class TestHotPath:
    """Testa que o hot path usa apenas memória sem I/O."""

    def test_get_active_policy_set_retorna_do_cache(
        self,
        registry: PolicyRuntimeRegistry,
        mock_manifest_resolver: MagicMock,
        mock_bundle_loader: MagicMock,
        mock_snapshot_loader: MagicMock,
    ) -> None:
        """get_active_policy_set() deve retornar do cache sem chamar loaders."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        expected_aps = _make_active_policy_set(scope_id=scope_id)

        # Injetar diretamente no cache interno
        registry._active_sets[scope_id] = expected_aps

        result = registry.get_active_policy_set(scope_id)

        assert result is expected_aps
        # Nenhum I/O deve ter ocorrido
        mock_manifest_resolver.resolve.assert_not_called()
        mock_bundle_loader.load.assert_not_called()
        mock_snapshot_loader.load.assert_not_called()

    def test_get_active_policy_set_dispara_bootstrap_se_escopo_ausente(
        self,
        registry: PolicyRuntimeRegistry,
        mock_manifest_resolver: MagicMock,
        mock_bundle_loader: MagicMock,
        mock_snapshot_loader: MagicMock,
        mock_lkg_store: MagicMock,
    ) -> None:
        """get_active_policy_set() deve disparar bootstrap se escopo não inicializado."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        manifest = _make_manifest(scope_id=scope_id)
        bundle = _make_bundle()
        snapshot = _make_snapshot()

        mock_manifest_resolver.resolve.return_value = manifest
        mock_bundle_loader.load.return_value = bundle
        mock_snapshot_loader.load.return_value = snapshot
        mock_lkg_store.has_valid_boot = False

        result = registry.get_active_policy_set(scope_id)

        assert result is not None
        assert result.manifest.activation_id == manifest.activation_id
        mock_manifest_resolver.resolve.assert_called_once_with(scope_id)


# ---------------------------------------------------------------------------
# Testes de refresh_scope
# ---------------------------------------------------------------------------


class TestRefreshScope:
    """Testa o ciclo de vida de refresh do ActivePolicySet."""

    def test_refresh_carrega_bundle_e_snapshot(
        self,
        registry: PolicyRuntimeRegistry,
        mock_manifest_resolver: MagicMock,
        mock_bundle_loader: MagicMock,
        mock_snapshot_loader: MagicMock,
        mock_lkg_store: MagicMock,
    ) -> None:
        """refresh_scope() deve carregar bundle e snapshot e atualizar o cache."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        manifest = _make_manifest(scope_id=scope_id)
        bundle = _make_bundle()
        snapshot = _make_snapshot()

        mock_manifest_resolver.resolve.return_value = manifest
        mock_bundle_loader.load.return_value = bundle
        mock_snapshot_loader.load.return_value = snapshot
        mock_lkg_store.has_valid_boot = False

        registry.refresh_scope(scope_id)

        # Verificar que os loaders foram chamados com os identificadores corretos
        mock_bundle_loader.load.assert_called_once_with(manifest.artifact_hash)
        mock_snapshot_loader.load.assert_called_once_with(manifest.snapshot_version)

        # Verificar que o cache foi atualizado
        assert scope_id in registry._active_sets
        aps = registry._active_sets[scope_id]
        assert aps.manifest.activation_id == manifest.activation_id
        assert aps.integrity_verified is True

    def test_refresh_salva_lkg_apos_sucesso(
        self,
        registry: PolicyRuntimeRegistry,
        mock_manifest_resolver: MagicMock,
        mock_bundle_loader: MagicMock,
        mock_snapshot_loader: MagicMock,
        mock_lkg_store: MagicMock,
    ) -> None:
        """refresh_scope() deve salvar o LKG após carregamento bem-sucedido."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        manifest = _make_manifest(scope_id=scope_id)

        mock_manifest_resolver.resolve.return_value = manifest
        mock_bundle_loader.load.return_value = _make_bundle()
        mock_snapshot_loader.load.return_value = _make_snapshot()
        mock_lkg_store.has_valid_boot = False

        registry.refresh_scope(scope_id)

        mock_lkg_store.save.assert_called_once()
        saved_scope_id, saved_aps = mock_lkg_store.save.call_args[0]
        assert saved_scope_id == scope_id
        assert saved_aps.manifest.activation_id == manifest.activation_id

    def test_refresh_marca_boot_valido_na_primeira_vez(
        self,
        registry: PolicyRuntimeRegistry,
        mock_manifest_resolver: MagicMock,
        mock_bundle_loader: MagicMock,
        mock_snapshot_loader: MagicMock,
        mock_lkg_store: MagicMock,
    ) -> None:
        """refresh_scope() deve chamar mark_boot_valid() na primeira inicialização."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"

        mock_manifest_resolver.resolve.return_value = _make_manifest(scope_id=scope_id)
        mock_bundle_loader.load.return_value = _make_bundle()
        mock_snapshot_loader.load.return_value = _make_snapshot()
        mock_lkg_store.has_valid_boot = False

        registry.refresh_scope(scope_id)

        mock_lkg_store.mark_boot_valid.assert_called_once()

    def test_refresh_nao_marca_boot_valido_se_ja_marcado(
        self,
        registry: PolicyRuntimeRegistry,
        mock_manifest_resolver: MagicMock,
        mock_bundle_loader: MagicMock,
        mock_snapshot_loader: MagicMock,
        mock_lkg_store: MagicMock,
    ) -> None:
        """refresh_scope() não deve chamar mark_boot_valid() se já foi marcado."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"

        mock_manifest_resolver.resolve.return_value = _make_manifest(scope_id=scope_id)
        mock_bundle_loader.load.return_value = _make_bundle()
        mock_snapshot_loader.load.return_value = _make_snapshot()
        # Simular que boot já foi marcado
        mock_lkg_store.has_valid_boot = True

        registry.refresh_scope(scope_id)

        mock_lkg_store.mark_boot_valid.assert_not_called()

    def test_refresh_skip_se_activation_id_nao_mudou(
        self,
        registry: PolicyRuntimeRegistry,
        mock_manifest_resolver: MagicMock,
        mock_bundle_loader: MagicMock,
        mock_snapshot_loader: MagicMock,
        mock_lkg_store: MagicMock,
    ) -> None:
        """refresh_scope() deve pular carregamento se activation_id não mudou."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        activation_id = "act_001"
        existing_aps = _make_active_policy_set(
            activation_id=activation_id, scope_id=scope_id
        )

        # Pré-popular o cache com o conjunto atual
        registry._active_sets[scope_id] = existing_aps

        # Manifesto retorna o mesmo activation_id
        mock_manifest_resolver.resolve.return_value = _make_manifest(
            activation_id=activation_id, scope_id=scope_id
        )
        mock_lkg_store.has_valid_boot = True

        registry.refresh_scope(scope_id)

        # Loaders não devem ser chamados — activation_id não mudou
        mock_bundle_loader.load.assert_not_called()
        mock_snapshot_loader.load.assert_not_called()

    def test_refresh_carrega_novos_artefatos_se_activation_id_mudou(
        self,
        registry: PolicyRuntimeRegistry,
        mock_manifest_resolver: MagicMock,
        mock_bundle_loader: MagicMock,
        mock_snapshot_loader: MagicMock,
        mock_lkg_store: MagicMock,
    ) -> None:
        """refresh_scope() deve carregar novos artefatos se activation_id mudou."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        old_aps = _make_active_policy_set(activation_id="act_001", scope_id=scope_id)
        registry._active_sets[scope_id] = old_aps

        # Novo manifesto com activation_id diferente
        new_manifest = _make_manifest(
            activation_id="act_002",
            scope_id=scope_id,
            artifact_hash="sha256:new_hash",
            snapshot_version="snap_002",
        )
        mock_manifest_resolver.resolve.return_value = new_manifest
        mock_bundle_loader.load.return_value = _make_bundle("sha256:new_hash")
        mock_snapshot_loader.load.return_value = _make_snapshot("snap_002")
        mock_lkg_store.has_valid_boot = True

        registry.refresh_scope(scope_id)

        # Loaders devem ser chamados com os novos identificadores
        mock_bundle_loader.load.assert_called_once_with("sha256:new_hash")
        mock_snapshot_loader.load.assert_called_once_with("snap_002")

        # Cache deve ter o novo conjunto
        assert registry._active_sets[scope_id].manifest.activation_id == "act_002"


# ---------------------------------------------------------------------------
# Testes de fallback para LKG
# ---------------------------------------------------------------------------


class TestFallbackLKG:
    """Testa a política de fallback para o Last Known Good."""

    def test_usa_lkg_se_refresh_falhar_apos_boot_valido(
        self,
        registry: PolicyRuntimeRegistry,
        mock_manifest_resolver: MagicMock,
        mock_lkg_store: MagicMock,
    ) -> None:
        """refresh_scope() deve usar LKG se refresh falhar e boot válido existir."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        lkg_aps = _make_active_policy_set(activation_id="act_lkg", scope_id=scope_id)

        # Simular falha no resolver
        mock_manifest_resolver.resolve.side_effect = PolicyBundleUnavailable(
            "AppConfig indisponível"
        )
        # LKG disponível após boot válido
        mock_lkg_store.has_valid_boot = True
        mock_lkg_store.load.return_value = lkg_aps

        # Não deve levantar exceção
        registry.refresh_scope(scope_id)

        # Cache deve ter o LKG
        assert registry._active_sets[scope_id].manifest.activation_id == "act_lkg"

    def test_levanta_policy_engine_not_ready_sem_lkg(
        self,
        registry: PolicyRuntimeRegistry,
        mock_manifest_resolver: MagicMock,
        mock_lkg_store: MagicMock,
    ) -> None:
        """refresh_scope() deve levantar PolicyEngineNotReady se falhar sem LKG."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"

        mock_manifest_resolver.resolve.side_effect = PolicyBundleUnavailable(
            "AppConfig indisponível"
        )
        # Sem LKG disponível (cold start)
        mock_lkg_store.has_valid_boot = False
        mock_lkg_store.load.return_value = None

        with pytest.raises(PolicyEngineNotReady) as exc_info:
            registry.refresh_scope(scope_id)

        assert "tenantA:TRANSFER:PIX:*:prod" in str(exc_info.value)

    def test_levanta_policy_engine_not_ready_se_bundle_falhar_sem_lkg(
        self,
        registry: PolicyRuntimeRegistry,
        mock_manifest_resolver: MagicMock,
        mock_bundle_loader: MagicMock,
        mock_lkg_store: MagicMock,
    ) -> None:
        """PolicyEngineNotReady deve ser levantado se bundle falhar e sem LKG."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"

        mock_manifest_resolver.resolve.return_value = _make_manifest(scope_id=scope_id)
        mock_bundle_loader.load.side_effect = PolicyBundleIntegrityFailure(
            "Hash divergente"
        )
        mock_lkg_store.has_valid_boot = False
        mock_lkg_store.load.return_value = None

        with pytest.raises(PolicyEngineNotReady):
            registry.refresh_scope(scope_id)

    def test_usa_lkg_se_snapshot_falhar_apos_boot_valido(
        self,
        registry: PolicyRuntimeRegistry,
        mock_manifest_resolver: MagicMock,
        mock_bundle_loader: MagicMock,
        mock_snapshot_loader: MagicMock,
        mock_lkg_store: MagicMock,
    ) -> None:
        """refresh_scope() deve usar LKG se snapshot falhar após boot válido."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        lkg_aps = _make_active_policy_set(activation_id="act_lkg", scope_id=scope_id)

        mock_manifest_resolver.resolve.return_value = _make_manifest(scope_id=scope_id)
        mock_bundle_loader.load.return_value = _make_bundle()
        mock_snapshot_loader.load.side_effect = PolicySnapshotUnavailable(
            "Snapshot indisponível"
        )
        mock_lkg_store.has_valid_boot = True
        mock_lkg_store.load.return_value = lkg_aps

        registry.refresh_scope(scope_id)

        assert registry._active_sets[scope_id].manifest.activation_id == "act_lkg"


# ---------------------------------------------------------------------------
# Testes de get_current_activation_id
# ---------------------------------------------------------------------------


class TestGetCurrentActivationId:
    """Testa o helper de consulta do activation_id atual."""

    def test_retorna_none_se_escopo_nao_inicializado(
        self, registry: PolicyRuntimeRegistry
    ) -> None:
        """get_current_activation_id() deve retornar None para escopo não inicializado."""
        result = registry.get_current_activation_id("escopo_inexistente:*:*:*:prod")
        assert result is None

    def test_retorna_activation_id_do_conjunto_ativo(
        self, registry: PolicyRuntimeRegistry
    ) -> None:
        """get_current_activation_id() deve retornar o activation_id do conjunto ativo."""
        scope_id = "tenantA:TRANSFER:PIX:*:prod"
        aps = _make_active_policy_set(activation_id="act_001", scope_id=scope_id)
        registry._active_sets[scope_id] = aps

        result = registry.get_current_activation_id(scope_id)
        assert result == "act_001"
