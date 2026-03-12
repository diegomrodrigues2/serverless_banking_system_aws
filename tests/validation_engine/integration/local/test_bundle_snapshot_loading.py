"""
Testes de integração local — armazenamento e carregamento de bundles e snapshots.

Usa moto para simular o S3 localmente sem dependências de AWS reais.

Cobertura:
- Round-trip bundle: store → load → verificar campos
- Round-trip snapshot: store → load → verificar campos e tipos
- Integridade: hash correto passa, conteúdo adulterado falha
- Idempotência: store repetido não cria duplicatas
- Cache do BundleLoader: hit e invalidação
- Cache do SnapshotLoader: hit e invalidação

Nota sobre KMS com moto:
    moto aceita os parâmetros SSE-KMS no put_object sem realizar criptografia real.
    Isso é comportamento esperado para testes locais — a chave KMS é fictícia.

Requisitos cobertos: 3.1, 3.2, 3.3, 3.4
"""

from __future__ import annotations

import hashlib
import json

import pytest

from validation_engine.domain.errors import PolicyBundleIntegrityFailure
from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
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
from validation_engine.infrastructure.bundle_loader import BundleLoader
from validation_engine.infrastructure.bundle_store import BundleStore
from validation_engine.infrastructure.snapshot_loader import SnapshotLoader
from validation_engine.infrastructure.snapshot_store import SnapshotStore

# ---------------------------------------------------------------------------
# Constantes de teste
# ---------------------------------------------------------------------------

FAKE_KMS_KEY_ID = "arn:aws:kms:us-east-1:123456789012:key/fake-local-test-key"
CONTEXT_SCHEMA_VERSION = "1.0"
EVALUATOR_VERSION = "1.0.0"
SNAPSHOT_SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Helpers de construção de artefatos de teste
# ---------------------------------------------------------------------------


def _make_rule_ast() -> RuleAST:
    """Constrói um RuleAST mínimo para testes de integração."""
    rule = PolicyRuleNode(
        name="deny_high_value",
        priority=100,
        condition=ComparisonNode(
            left=FieldAccessNode(path=("facts", "max_posting_amount")),
            operator=">=",
            right=LiteralNode(value=100_000),
        ),
        effect=PolicyEffect.DENY,
        message="Valor acima do limite permitido",
    )
    return RuleAST(rules=(rule,))


def _compute_artifact_hash(bundle_without_hash: RuleBundle) -> str:
    """
    Calcula o artifact_hash correto para um bundle.

    Replica o algoritmo do DSLCompiler:
    1. Serializar o bundle para JSON
    2. Remover o campo artifact_hash
    3. Re-serializar com sort_keys=True
    4. SHA-256 do conteúdo UTF-8
    """
    raw = json.loads(bundle_without_hash.to_json())
    content = {k: v for k, v in raw.items() if k != "artifact_hash"}
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_bundle(
    policy_set_id: str = "integration-test-policy-set",
    context_schema_version: str = CONTEXT_SCHEMA_VERSION,
    evaluator_min_version: str = EVALUATOR_VERSION,
    snapshot_schema_version: str = SNAPSHOT_SCHEMA_VERSION,
    author: str = "integration-test-author",
    description: str = "Bundle de integração local",
) -> RuleBundle:
    """
    Constrói um RuleBundle com artifact_hash correto para testes de integração.

    O hash é calculado sobre o conteúdo real do bundle, garantindo que
    a verificação de integridade no BundleLoader passe corretamente.
    """
    # Passo 1: criar bundle com hash placeholder para calcular o hash real
    placeholder = RuleBundle(
        policy_set_id=policy_set_id,
        artifact_hash="placeholder",
        ast=_make_rule_ast(),
        execution_plan={"version": 1, "steps": []},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version=context_schema_version,
            snapshot_schema_version=snapshot_schema_version,
            evaluator_min_version=evaluator_min_version,
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author=author,
            description=description,
            compiled_at="2026-03-11T00:00:00Z",
            source_hash="sha256:integration-source",
        ),
    )

    # Passo 2: calcular o hash real sobre o conteúdo sem o campo artifact_hash
    correct_hash = _compute_artifact_hash(placeholder)

    # Passo 3: recriar o bundle com o hash correto
    return RuleBundle(
        policy_set_id=policy_set_id,
        artifact_hash=correct_hash,
        ast=_make_rule_ast(),
        execution_plan={"version": 1, "steps": []},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version=context_schema_version,
            snapshot_schema_version=snapshot_schema_version,
            evaluator_min_version=evaluator_min_version,
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author=author,
            description=description,
            compiled_at="2026-03-11T00:00:00Z",
            source_hash="sha256:integration-source",
        ),
    )


def _build_snapshot(
    snapshot_version: str = "snap-integration-v1",
    snapshot_schema_version: str = SNAPSHOT_SCHEMA_VERSION,
) -> ReferenceSnapshot:
    """
    Constrói um ReferenceSnapshot com dados variados para testes de integração.

    Inclui todos os tipos suportados: int, str, bool, tuple[str,...], tuple[int,...].
    """
    return ReferenceSnapshot(
        snapshot_version=snapshot_version,
        snapshot_schema_version=snapshot_schema_version,
        created_at="2026-03-11T00:00:00Z",
        data={
            "daily_limit_minor": 100_000,
            "tenant_name": "integration-tenant",
            "is_active": True,
            "blocked_accounts": ("acc_001", "acc_002", "acc_003"),
            "allowed_currencies": ("BRL", "USD"),
            "tier_limits": (10_000, 50_000, 100_000),
        },
    )


# ---------------------------------------------------------------------------
# 1. TestBundleRoundTrip
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestBundleRoundTrip:
    """
    Testes de round-trip para RuleBundle: store → load → verificar campos.

    Valida que o bundle armazenado no S3 (via moto) pode ser carregado
    de volta com todos os campos preservados.

    Requisitos: 3.1, 3.3
    """

    def test_store_and_load_bundle_round_trip(self, local_test_env):
        """
        Armazena um bundle e carrega de volta — todos os campos devem ser iguais.

        Verifica o round-trip completo: serialização → S3 → desserialização.
        """
        bundle = _build_bundle()
        store = BundleStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)
        loader = BundleLoader(
            local_test_env.s3_client,
            local_test_env.bucket_name,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )

        store.store(bundle)
        loaded = loader.load(bundle.artifact_hash)

        assert loaded == bundle

    def test_bundle_round_trip_preserves_ast(self, local_test_env):
        """
        O AST deve ser preservado integralmente através do ciclo store/load.

        Verifica que as rules, efeitos, prioridades e condições do AST
        são idênticos após round-trip.
        """
        bundle = _build_bundle()
        store = BundleStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)
        loader = BundleLoader(
            local_test_env.s3_client,
            local_test_env.bucket_name,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )

        store.store(bundle)
        loaded = loader.load(bundle.artifact_hash)

        # Verificar que o AST foi preservado
        assert loaded.ast == bundle.ast
        assert len(loaded.ast.rules) == len(bundle.ast.rules)

        original_rule = bundle.ast.rules[0]
        loaded_rule = loaded.ast.rules[0]
        assert loaded_rule.name == original_rule.name
        assert loaded_rule.effect == original_rule.effect
        assert loaded_rule.priority == original_rule.priority
        assert loaded_rule.message == original_rule.message

    def test_bundle_round_trip_preserves_compatibility(self, local_test_env):
        """
        Os campos de compatibilidade devem ser preservados através do round-trip.

        Verifica dsl_version, context_schema_version, snapshot_schema_version
        e evaluator_min_version.
        """
        bundle = _build_bundle()
        store = BundleStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)
        loader = BundleLoader(
            local_test_env.s3_client,
            local_test_env.bucket_name,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )

        store.store(bundle)
        loaded = loader.load(bundle.artifact_hash)

        assert loaded.compatibility.dsl_version == bundle.compatibility.dsl_version
        assert loaded.compatibility.context_schema_version == bundle.compatibility.context_schema_version
        assert loaded.compatibility.snapshot_schema_version == bundle.compatibility.snapshot_schema_version
        assert loaded.compatibility.evaluator_min_version == bundle.compatibility.evaluator_min_version

    def test_bundle_round_trip_preserves_metadata(self, local_test_env):
        """
        Os metadados de compilação devem ser preservados através do round-trip.

        Verifica author, description, compiled_at e source_hash.
        """
        bundle = _build_bundle(author="test-engineer", description="Policy de limite diário")
        store = BundleStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)
        loader = BundleLoader(
            local_test_env.s3_client,
            local_test_env.bucket_name,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )

        store.store(bundle)
        loaded = loader.load(bundle.artifact_hash)

        assert loaded.metadata.author == "test-engineer"
        assert loaded.metadata.description == "Policy de limite diário"
        assert loaded.metadata.compiled_at == bundle.metadata.compiled_at
        assert loaded.metadata.source_hash == bundle.metadata.source_hash


# ---------------------------------------------------------------------------
# 2. TestSnapshotRoundTrip
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestSnapshotRoundTrip:
    """
    Testes de round-trip para ReferenceSnapshot: store → load → verificar campos.

    Valida que o snapshot armazenado no S3 (via moto) pode ser carregado
    de volta com todos os campos e tipos preservados.

    Requisitos: 3.2, 3.3
    """

    def test_store_and_load_snapshot_round_trip(self, local_test_env):
        """
        Armazena um snapshot e carrega de volta — todos os campos devem ser iguais.

        Verifica o round-trip completo incluindo restauração de tipos.
        """
        snapshot = _build_snapshot()
        store = SnapshotStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)
        loader = SnapshotLoader(
            local_test_env.s3_client,
            local_test_env.bucket_name,
            expected_snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        )

        store.store(snapshot)
        loaded = loader.load(snapshot.snapshot_version)

        assert loaded.snapshot_version == snapshot.snapshot_version
        assert loaded.snapshot_schema_version == snapshot.snapshot_schema_version
        assert loaded.created_at == snapshot.created_at
        assert loaded.data == snapshot.data

    def test_snapshot_round_trip_preserves_tuple_types(self, local_test_env):
        """
        Tuples de strings e inteiros devem ser preservadas como tuples após round-trip.

        JSON serializa tuples como listas — o SnapshotLoader deve restaurar
        os tipos corretos na desserialização.
        """
        snapshot = _build_snapshot()
        store = SnapshotStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)
        loader = SnapshotLoader(
            local_test_env.s3_client,
            local_test_env.bucket_name,
            expected_snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        )

        store.store(snapshot)
        loaded = loader.load(snapshot.snapshot_version)

        # tuple[str, ...] deve ser preservada como tuple de strings
        blocked = loaded.data["blocked_accounts"]
        assert isinstance(blocked, tuple)
        assert all(isinstance(item, str) for item in blocked)
        assert blocked == ("acc_001", "acc_002", "acc_003")

        # tuple[str, ...] com moedas
        currencies = loaded.data["allowed_currencies"]
        assert isinstance(currencies, tuple)
        assert currencies == ("BRL", "USD")

        # tuple[int, ...] deve ser preservada como tuple de inteiros
        tiers = loaded.data["tier_limits"]
        assert isinstance(tiers, tuple)
        assert all(isinstance(item, int) for item in tiers)
        assert tiers == (10_000, 50_000, 100_000)

    def test_snapshot_round_trip_preserves_scalars(self, local_test_env):
        """
        Valores escalares (int, str, bool) devem ser preservados após round-trip.
        """
        snapshot = _build_snapshot()
        store = SnapshotStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)
        loader = SnapshotLoader(
            local_test_env.s3_client,
            local_test_env.bucket_name,
            expected_snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        )

        store.store(snapshot)
        loaded = loader.load(snapshot.snapshot_version)

        # int preservado
        assert loaded.data["daily_limit_minor"] == 100_000
        assert isinstance(loaded.data["daily_limit_minor"], int)

        # str preservada
        assert loaded.data["tenant_name"] == "integration-tenant"
        assert isinstance(loaded.data["tenant_name"], str)

        # bool preservado
        assert loaded.data["is_active"] is True
        assert isinstance(loaded.data["is_active"], bool)


# ---------------------------------------------------------------------------
# 3. TestBundleIntegrity
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestBundleIntegrity:
    """
    Testes de integridade criptográfica do bundle.

    Valida que o BundleLoader aceita bundles com hash correto e rejeita
    bundles com conteúdo adulterado.

    Requisitos: 3.3, 20.3, 20.4
    """

    def test_bundle_integrity_passes_for_valid_bundle(self, local_test_env):
        """
        Bundle armazenado com hash correto deve ser carregado sem erros.

        Verifica que o fluxo normal (store → load) passa na verificação
        de integridade sem levantar exceção.
        """
        bundle = _build_bundle()
        store = BundleStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)
        loader = BundleLoader(
            local_test_env.s3_client,
            local_test_env.bucket_name,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )

        store.store(bundle)

        # Não deve levantar exceção — hash correto
        loaded = loader.load(bundle.artifact_hash)
        assert loaded.artifact_hash == bundle.artifact_hash

    def test_bundle_integrity_fails_for_tampered_content(self, local_test_env):
        """
        Bundle com conteúdo adulterado deve levantar PolicyBundleIntegrityFailure.

        Simula adulteração colocando diretamente no S3 um JSON modificado
        mas mantendo a chave original (artifact_hash). O loader deve detectar
        a divergência entre o hash esperado e o hash calculado do conteúdo.
        """
        bundle = _build_bundle()
        s3_client = local_test_env.s3_client
        bucket = local_test_env.bucket_name

        # Armazenar o bundle original para obter a chave correta
        store = BundleStore(s3_client, bucket, FAKE_KMS_KEY_ID)
        store.store(bundle)

        # Adulterar o conteúdo: modificar o JSON diretamente no S3
        # mantendo a mesma chave (artifact_hash) — simula ataque de substituição
        tampered_key = f"bundles/{bundle.artifact_hash}.json"
        original_json = json.loads(bundle.to_json())
        original_json["policy_set_id"] = "TAMPERED-policy-set"  # adulteração
        tampered_json = json.dumps(original_json, ensure_ascii=False, sort_keys=True)

        s3_client.put_object(
            Bucket=bucket,
            Key=tampered_key,
            Body=tampered_json.encode("utf-8"),
            ContentType="application/json",
        )

        loader = BundleLoader(
            s3_client,
            bucket,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )

        # O loader deve detectar a adulteração e levantar PolicyBundleIntegrityFailure
        with pytest.raises(PolicyBundleIntegrityFailure) as exc_info:
            loader.load(bundle.artifact_hash)

        # A mensagem deve indicar os hashes divergentes
        error_message = str(exc_info.value.message)
        assert bundle.artifact_hash in error_message


# ---------------------------------------------------------------------------
# 4. TestIdempotency
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestIdempotency:
    """
    Testes de idempotência do armazenamento.

    Valida que armazenar o mesmo artefato múltiplas vezes não cria duplicatas
    e não altera o conteúdo existente.

    Requisitos: 3.4
    """

    def test_bundle_store_is_idempotent(self, local_test_env):
        """
        Armazenar o mesmo bundle duas vezes deve resultar em apenas um objeto no S3.

        Verifica que a segunda chamada a store() é ignorada silenciosamente
        quando o objeto já existe.
        """
        bundle = _build_bundle()
        store = BundleStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)

        store.store(bundle)
        store.store(bundle)  # segunda chamada — deve ser ignorada

        # Verificar que existe exatamente um objeto com a chave do bundle
        key = f"bundles/{bundle.artifact_hash}.json"
        response = local_test_env.s3_client.list_objects_v2(
            Bucket=local_test_env.bucket_name,
            Prefix=key,
        )
        objects = response.get("Contents", [])
        assert len(objects) == 1
        assert objects[0]["Key"] == key

    def test_snapshot_store_is_idempotent(self, local_test_env):
        """
        Armazenar o mesmo snapshot duas vezes deve resultar em apenas um objeto no S3.
        """
        snapshot = _build_snapshot()
        store = SnapshotStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)

        store.store(snapshot)
        store.store(snapshot)  # segunda chamada — deve ser ignorada

        key = f"snapshots/{snapshot.snapshot_version}.json"
        response = local_test_env.s3_client.list_objects_v2(
            Bucket=local_test_env.bucket_name,
            Prefix=key,
        )
        objects = response.get("Contents", [])
        assert len(objects) == 1
        assert objects[0]["Key"] == key

    def test_bundle_store_idempotent_does_not_change_content(self, local_test_env):
        """
        Armazenar o mesmo bundle duas vezes não deve alterar o conteúdo existente.

        Verifica que o conteúdo após a segunda chamada é idêntico ao original.
        """
        bundle = _build_bundle()
        s3_client = local_test_env.s3_client
        bucket = local_test_env.bucket_name
        store = BundleStore(s3_client, bucket, FAKE_KMS_KEY_ID)

        store.store(bundle)

        # Capturar o conteúdo após o primeiro store
        key = f"bundles/{bundle.artifact_hash}.json"
        first_response = s3_client.get_object(Bucket=bucket, Key=key)
        first_content = first_response["Body"].read()

        store.store(bundle)  # segunda chamada — idempotente

        # Conteúdo deve ser idêntico ao original
        second_response = s3_client.get_object(Bucket=bucket, Key=key)
        second_content = second_response["Body"].read()

        assert first_content == second_content


# ---------------------------------------------------------------------------
# 5. TestBundleLoaderCache
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestBundleLoaderCache:
    """
    Testes de cache do BundleLoader com S3 real (moto).

    Valida que o cache em memória funciona corretamente: hit evita I/O ao S3
    e invalidação força recarga.

    Requisitos: 3.3, 3.4
    """

    def test_loader_caches_bundle_after_first_load(self, local_test_env):
        """
        Após o primeiro load, o bundle deve ser servido do cache sem I/O ao S3.

        Verifica que o segundo load retorna o mesmo objeto (identidade Python)
        que foi armazenado no cache após o primeiro load.
        """
        bundle = _build_bundle()
        store = BundleStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)
        loader = BundleLoader(
            local_test_env.s3_client,
            local_test_env.bucket_name,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )

        store.store(bundle)

        first_load = loader.load(bundle.artifact_hash)
        second_load = loader.load(bundle.artifact_hash)

        # Ambos os loads devem retornar o mesmo objeto (identidade — cache hit)
        assert first_load is second_load

    def test_loader_invalidate_forces_reload(self, local_test_env):
        """
        Após invalidate(), o próximo load deve buscar o bundle do S3 novamente.

        Verifica que o objeto retornado após invalidação é igual ao original
        (mesmo conteúdo, mas nova instância desserializada do S3).
        """
        bundle = _build_bundle()
        store = BundleStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)
        loader = BundleLoader(
            local_test_env.s3_client,
            local_test_env.bucket_name,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )

        store.store(bundle)

        first_load = loader.load(bundle.artifact_hash)

        # Invalidar o cache — próximo load deve ir ao S3
        loader.invalidate(bundle.artifact_hash)
        assert bundle.artifact_hash not in loader._cache

        # Recarregar do S3 — deve retornar bundle com mesmo conteúdo
        reloaded = loader.load(bundle.artifact_hash)
        assert reloaded == first_load

        # Após reload, o bundle deve estar no cache novamente
        assert bundle.artifact_hash in loader._cache


# ---------------------------------------------------------------------------
# 6. TestSnapshotLoaderCache
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestSnapshotLoaderCache:
    """
    Testes de cache do SnapshotLoader com S3 real (moto).

    Valida que o cache em memória funciona corretamente: hit evita I/O ao S3
    e invalidação força recarga.

    Requisitos: 3.3, 3.4
    """

    def test_loader_caches_snapshot_after_first_load(self, local_test_env):
        """
        Após o primeiro load, o snapshot deve ser servido do cache sem I/O ao S3.

        Verifica que o segundo load retorna o mesmo objeto (identidade Python)
        que foi armazenado no cache após o primeiro load.
        """
        snapshot = _build_snapshot()
        store = SnapshotStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)
        loader = SnapshotLoader(
            local_test_env.s3_client,
            local_test_env.bucket_name,
            expected_snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        )

        store.store(snapshot)

        first_load = loader.load(snapshot.snapshot_version)
        second_load = loader.load(snapshot.snapshot_version)

        # Ambos os loads devem retornar o mesmo objeto (identidade — cache hit)
        assert first_load is second_load

    def test_loader_invalidate_forces_reload(self, local_test_env):
        """
        Após invalidate(), o próximo load deve buscar o snapshot do S3 novamente.

        Verifica que o objeto retornado após invalidação é igual ao original
        (mesmo conteúdo, mas nova instância desserializada do S3).
        """
        snapshot = _build_snapshot()
        store = SnapshotStore(local_test_env.s3_client, local_test_env.bucket_name, FAKE_KMS_KEY_ID)
        loader = SnapshotLoader(
            local_test_env.s3_client,
            local_test_env.bucket_name,
            expected_snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        )

        store.store(snapshot)

        first_load = loader.load(snapshot.snapshot_version)

        # Invalidar o cache — próximo load deve ir ao S3
        loader.invalidate(snapshot.snapshot_version)
        assert snapshot.snapshot_version not in loader._cache

        # Recarregar do S3 — deve retornar snapshot com mesmo conteúdo
        reloaded = loader.load(snapshot.snapshot_version)
        assert reloaded == first_load

        # Após reload, o snapshot deve estar no cache novamente
        assert snapshot.snapshot_version in loader._cache
