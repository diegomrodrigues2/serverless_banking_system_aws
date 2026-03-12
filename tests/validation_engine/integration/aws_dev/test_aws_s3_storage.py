"""
Testes de integração AWS dev — armazenamento e carregamento de bundles e snapshots.

Usa recursos AWS REAIS (S3 real, KMS real) em ambiente dev.
NÃO usa moto ou qualquer mock de AWS.

Pré-requisitos:
    - VALIDATION_ENGINE_TEST_BUCKET: bucket S3 dedicado para testes
    - VALIDATION_ENGINE_TEST_KMS_KEY_ARN: ARN da chave KMS (opcional — testes KMS são pulados se ausente)
    - Credenciais AWS válidas com permissão de leitura/escrita no bucket

Estratégia de isolamento:
    O run_id (UUID único por sessão) é embutido no policy_set_id dos bundles
    e no snapshot_version dos snapshots. Isso garante que:
    1. Testes de sessões diferentes não colidem entre si.
    2. Os objetos criados podem ser identificados e limpos pelo run_id.

    Chaves geradas:
    - bundles/{sha256_do_conteudo}.json
    - snapshots/snap-aws-dev-{run_id}-v1.json

Cleanup:
    Cada teste deleta os objetos que criou via fixture de cleanup por teste.
    O cleanup é best-effort: falhas são logadas mas não falham os testes.

Requisitos cobertos: 3.1, 3.2, 3.6, 20.1
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Generator

import pytest

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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes de teste
# ---------------------------------------------------------------------------

CONTEXT_SCHEMA_VERSION = "1.0"
EVALUATOR_VERSION = "1.0.0"
SNAPSHOT_SCHEMA_VERSION = "1.0"

# Variável de ambiente para a chave KMS de testes
KMS_KEY_ARN_ENV_VAR = "VALIDATION_ENGINE_TEST_KMS_KEY_ARN"


# ---------------------------------------------------------------------------
# Helpers de construção de artefatos de teste
# ---------------------------------------------------------------------------


def _make_rule_ast() -> RuleAST:
    """Constrói um RuleAST mínimo para testes de integração AWS dev."""
    rule = PolicyRuleNode(
        name="deny_high_value_aws_dev",
        priority=100,
        condition=ComparisonNode(
            left=FieldAccessNode(path=("facts", "max_posting_amount")),
            operator=">=",
            right=LiteralNode(value=500_000),
        ),
        effect=PolicyEffect.DENY,
        message="Valor acima do limite permitido (teste AWS dev)",
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
    policy_set_id: str,
    kms_key_id: str,
    author: str = "aws-dev-integration-test",
    description: str = "Bundle de integração AWS dev",
) -> RuleBundle:
    """
    Constrói um RuleBundle com artifact_hash correto para testes AWS dev.

    O policy_set_id deve incluir o run_id para garantir unicidade por sessão
    e evitar colisão entre execuções paralelas ou sequenciais.

    Args:
        policy_set_id: identificador do conjunto de policies (deve incluir run_id).
        kms_key_id:    ARN ou ID da chave KMS (usado apenas para o store, não no bundle).
        author:        autor do bundle para metadados de compilação.
        description:   descrição do bundle para metadados de compilação.

    Returns:
        RuleBundle com artifact_hash calculado corretamente.
    """
    # Passo 1: criar bundle com hash placeholder para calcular o hash real
    placeholder = RuleBundle(
        policy_set_id=policy_set_id,
        artifact_hash="placeholder",
        ast=_make_rule_ast(),
        execution_plan={"version": 1, "steps": []},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version=CONTEXT_SCHEMA_VERSION,
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
            evaluator_min_version=EVALUATOR_VERSION,
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author=author,
            description=description,
            compiled_at="2026-03-11T00:00:00Z",
            source_hash="sha256:aws-dev-integration-source",
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
            context_schema_version=CONTEXT_SCHEMA_VERSION,
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
            evaluator_min_version=EVALUATOR_VERSION,
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author=author,
            description=description,
            compiled_at="2026-03-11T00:00:00Z",
            source_hash="sha256:aws-dev-integration-source",
        ),
    )


def _build_snapshot(snapshot_version: str) -> ReferenceSnapshot:
    """
    Constrói um ReferenceSnapshot para testes AWS dev.

    O snapshot_version deve incluir o run_id para garantir unicidade por sessão.

    Args:
        snapshot_version: versão do snapshot (deve incluir run_id).

    Returns:
        ReferenceSnapshot com dados variados para validar round-trip.
    """
    return ReferenceSnapshot(
        snapshot_version=snapshot_version,
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        created_at="2026-03-11T00:00:00Z",
        data={
            "daily_limit_minor": 500_000,
            "tenant_name": "aws-dev-integration-tenant",
            "is_active": True,
            "blocked_accounts": ("acc_aws_001", "acc_aws_002"),
            "allowed_currencies": ("BRL", "USD", "EUR"),
            "tier_limits": (10_000, 100_000, 500_000),
        },
    )


# ---------------------------------------------------------------------------
# Fixtures de cleanup por teste
# ---------------------------------------------------------------------------


@pytest.fixture()
def bundle_cleanup(aws_dev_s3_client, aws_dev_config) -> Generator[list[str], None, None]:
    """
    Fixture de cleanup para objetos de bundle criados durante um teste.

    Coleta as chaves S3 dos bundles criados e os deleta ao final do teste.
    O cleanup é best-effort: falhas são logadas mas não falham o teste.

    Yields:
        Lista mutável de chaves S3 a deletar após o teste.
        O teste deve adicionar as chaves dos objetos criados a esta lista.
    """
    keys_to_delete: list[str] = []
    yield keys_to_delete

    # Cleanup ao final do teste
    for key in keys_to_delete:
        try:
            aws_dev_s3_client.delete_object(Bucket=aws_dev_config.bucket, Key=key)
            logger.info("objeto de bundle deletado no cleanup", extra={"key": key})
        except Exception as exc:
            logger.warning(
                "falha no cleanup do objeto de bundle",
                extra={"key": key, "error": str(exc)},
            )


@pytest.fixture()
def snapshot_cleanup(aws_dev_s3_client, aws_dev_config) -> Generator[list[str], None, None]:
    """
    Fixture de cleanup para objetos de snapshot criados durante um teste.

    Coleta as chaves S3 dos snapshots criados e os deleta ao final do teste.
    O cleanup é best-effort: falhas são logadas mas não falham o teste.

    Yields:
        Lista mutável de chaves S3 a deletar após o teste.
        O teste deve adicionar as chaves dos objetos criados a esta lista.
    """
    keys_to_delete: list[str] = []
    yield keys_to_delete

    # Cleanup ao final do teste
    for key in keys_to_delete:
        try:
            aws_dev_s3_client.delete_object(Bucket=aws_dev_config.bucket, Key=key)
            logger.info("objeto de snapshot deletado no cleanup", extra={"key": key})
        except Exception as exc:
            logger.warning(
                "falha no cleanup do objeto de snapshot",
                extra={"key": key, "error": str(exc)},
            )


@pytest.fixture()
def kms_key_arn() -> str:
    """
    Retorna o ARN da chave KMS para testes AWS dev.

    Lê a variável de ambiente VALIDATION_ENGINE_TEST_KMS_KEY_ARN.
    Se não estiver definida, pula o teste com mensagem explicativa.

    Returns:
        ARN da chave KMS configurada para testes.
    """
    arn = os.environ.get(KMS_KEY_ARN_ENV_VAR, "")
    if not arn:
        pytest.skip(
            f"{KMS_KEY_ARN_ENV_VAR} não definido — testes de KMS ignorados. "
            "Configure a variável com o ARN da chave KMS do bucket de testes."
        )
    return arn


@pytest.fixture()
def placeholder_kms_key() -> str:
    """
    Retorna um ARN de chave KMS placeholder para testes que não verificam KMS.

    Usado em testes de round-trip e idempotência onde a criptografia KMS
    não é o foco da verificação. O bucket de testes deve ter uma chave KMS
    padrão configurada para aceitar este placeholder.

    Returns:
        ARN placeholder de chave KMS para testes não-KMS.
    """
    # Tenta usar a chave real se disponível, senão usa um placeholder.
    # O bucket de testes deve ter SSE-KMS configurado como padrão para
    # aceitar objetos sem SSEKMSKeyId explícito, ou ter uma chave padrão.
    arn = os.environ.get(KMS_KEY_ARN_ENV_VAR, "")
    if arn:
        return arn
    # Placeholder: o bucket deve ter uma chave KMS padrão configurada.
    # Se não tiver, os testes de round-trip falharão com erro de KMS.
    return "aws/s3"


# ---------------------------------------------------------------------------
# 1. TestAWSBundleStoreLoad
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
class TestAWSBundleStoreLoad:
    """
    Testes de round-trip para RuleBundle contra S3 real em ambiente AWS dev.

    Valida que o bundle armazenado no S3 real pode ser carregado de volta
    com todos os campos preservados, e que as operações são idempotentes.

    O run_id é embutido no policy_set_id para garantir unicidade por sessão
    e evitar colisão entre execuções paralelas.

    Requisitos: 3.1, 3.3
    """

    def test_bundle_store_and_load_round_trip(
        self,
        aws_dev_config,
        aws_dev_s3_client,
        placeholder_kms_key,
        bundle_cleanup,
    ) -> None:
        """
        Armazena um bundle no S3 real e carrega de volta — todos os campos devem ser iguais.

        Verifica o round-trip completo contra AWS real:
        serialização → S3 → desserialização → verificação de integridade.
        """
        # Incluir run_id no policy_set_id para unicidade por sessão de testes
        policy_set_id = f"aws-dev-test-bundle-{aws_dev_config.run_id}"
        bundle = _build_bundle(policy_set_id=policy_set_id, kms_key_id=placeholder_kms_key)

        store = BundleStore(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            kms_key_id=placeholder_kms_key,
        )
        loader = BundleLoader(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )

        # Registrar chave para cleanup ao final do teste
        bundle_key = f"bundles/{bundle.artifact_hash}.json"
        bundle_cleanup.append(bundle_key)

        store.store(bundle)
        loaded = loader.load(bundle.artifact_hash)

        # Verificar que todos os campos foram preservados no round-trip
        assert loaded == bundle
        assert loaded.policy_set_id == policy_set_id
        assert loaded.artifact_hash == bundle.artifact_hash
        assert loaded.composition_mode == bundle.composition_mode
        assert loaded.compatibility == bundle.compatibility
        assert loaded.metadata == bundle.metadata

    def test_bundle_store_is_idempotent_on_real_s3(
        self,
        aws_dev_config,
        aws_dev_s3_client,
        placeholder_kms_key,
        bundle_cleanup,
    ) -> None:
        """
        Armazenar o mesmo bundle duas vezes no S3 real não deve causar erro.

        Verifica que a segunda chamada a store() é ignorada silenciosamente
        e que o conteúdo permanece inalterado após a segunda operação.
        """
        policy_set_id = f"aws-dev-test-idempotent-bundle-{aws_dev_config.run_id}"
        bundle = _build_bundle(policy_set_id=policy_set_id, kms_key_id=placeholder_kms_key)

        store = BundleStore(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            kms_key_id=placeholder_kms_key,
        )

        bundle_key = f"bundles/{bundle.artifact_hash}.json"
        bundle_cleanup.append(bundle_key)

        # Primeira chamada — deve armazenar o bundle
        store.store(bundle)

        # Capturar ETag após o primeiro store para verificar que o conteúdo não mudou
        head_after_first = aws_dev_s3_client.head_object(
            Bucket=aws_dev_config.bucket, Key=bundle_key
        )
        etag_after_first = head_after_first["ETag"]

        # Segunda chamada — deve ser ignorada silenciosamente (idempotência)
        store.store(bundle)

        # Verificar que o ETag não mudou — conteúdo inalterado
        head_after_second = aws_dev_s3_client.head_object(
            Bucket=aws_dev_config.bucket, Key=bundle_key
        )
        etag_after_second = head_after_second["ETag"]

        assert etag_after_first == etag_after_second

    def test_bundle_integrity_verified_on_real_s3(
        self,
        aws_dev_config,
        aws_dev_s3_client,
        placeholder_kms_key,
        bundle_cleanup,
    ) -> None:
        """
        Bundle carregado do S3 real deve ter artifact_hash que corresponde ao conteúdo.

        Verifica que o BundleLoader recalcula o hash após carregar do S3 real
        e que o hash calculado corresponde ao artifact_hash declarado no bundle.
        """
        policy_set_id = f"aws-dev-test-integrity-bundle-{aws_dev_config.run_id}"
        bundle = _build_bundle(policy_set_id=policy_set_id, kms_key_id=placeholder_kms_key)

        store = BundleStore(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            kms_key_id=placeholder_kms_key,
        )
        loader = BundleLoader(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )

        bundle_key = f"bundles/{bundle.artifact_hash}.json"
        bundle_cleanup.append(bundle_key)

        store.store(bundle)
        loaded = loader.load(bundle.artifact_hash)

        # Verificar que o artifact_hash do bundle carregado corresponde ao conteúdo real
        # (o BundleLoader já faz essa verificação internamente — se chegou aqui, passou)
        assert loaded.artifact_hash == bundle.artifact_hash

        # Verificar manualmente que o hash calculado sobre o conteúdo carregado é correto
        recalculated_hash = _compute_artifact_hash(loaded)
        assert recalculated_hash == loaded.artifact_hash


# ---------------------------------------------------------------------------
# 2. TestAWSSnapshotStoreLoad
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
class TestAWSSnapshotStoreLoad:
    """
    Testes de round-trip para ReferenceSnapshot contra S3 real em ambiente AWS dev.

    Valida que o snapshot armazenado no S3 real pode ser carregado de volta
    com todos os campos e tipos preservados.

    O run_id é embutido no snapshot_version para garantir unicidade por sessão.

    Requisitos: 3.2, 3.3
    """

    def test_snapshot_store_and_load_round_trip(
        self,
        aws_dev_config,
        aws_dev_s3_client,
        placeholder_kms_key,
        snapshot_cleanup,
    ) -> None:
        """
        Armazena um snapshot no S3 real e carrega de volta — todos os campos devem ser iguais.

        Verifica o round-trip completo contra AWS real incluindo restauração de tipos.
        """
        snapshot_version = f"snap-aws-dev-{aws_dev_config.run_id}-v1"
        snapshot = _build_snapshot(snapshot_version=snapshot_version)

        store = SnapshotStore(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            kms_key_id=placeholder_kms_key,
        )
        loader = SnapshotLoader(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            expected_snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        )

        snapshot_key = f"snapshots/{snapshot_version}.json"
        snapshot_cleanup.append(snapshot_key)

        store.store(snapshot)
        loaded = loader.load(snapshot_version)

        # Verificar que todos os campos foram preservados no round-trip
        assert loaded.snapshot_version == snapshot.snapshot_version
        assert loaded.snapshot_schema_version == snapshot.snapshot_schema_version
        assert loaded.created_at == snapshot.created_at
        assert loaded.data == snapshot.data

    def test_snapshot_store_is_idempotent_on_real_s3(
        self,
        aws_dev_config,
        aws_dev_s3_client,
        placeholder_kms_key,
        snapshot_cleanup,
    ) -> None:
        """
        Armazenar o mesmo snapshot duas vezes no S3 real não deve causar erro.

        Verifica que a segunda chamada a store() é ignorada silenciosamente
        e que o conteúdo permanece inalterado após a segunda operação.
        """
        snapshot_version = f"snap-aws-dev-{aws_dev_config.run_id}-idempotent"
        snapshot = _build_snapshot(snapshot_version=snapshot_version)

        store = SnapshotStore(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            kms_key_id=placeholder_kms_key,
        )

        snapshot_key = f"snapshots/{snapshot_version}.json"
        snapshot_cleanup.append(snapshot_key)

        # Primeira chamada — deve armazenar o snapshot
        store.store(snapshot)

        # Capturar ETag após o primeiro store
        head_after_first = aws_dev_s3_client.head_object(
            Bucket=aws_dev_config.bucket, Key=snapshot_key
        )
        etag_after_first = head_after_first["ETag"]

        # Segunda chamada — deve ser ignorada silenciosamente (idempotência)
        store.store(snapshot)

        # Verificar que o ETag não mudou — conteúdo inalterado
        head_after_second = aws_dev_s3_client.head_object(
            Bucket=aws_dev_config.bucket, Key=snapshot_key
        )
        etag_after_second = head_after_second["ETag"]

        assert etag_after_first == etag_after_second

    def test_snapshot_tuple_types_preserved_on_real_s3(
        self,
        aws_dev_config,
        aws_dev_s3_client,
        placeholder_kms_key,
        snapshot_cleanup,
    ) -> None:
        """
        Tuples de strings e inteiros devem ser preservadas como tuples após round-trip no S3 real.

        JSON serializa tuples como listas — o SnapshotLoader deve restaurar
        os tipos corretos na desserialização, mesmo passando pelo S3 real.
        """
        snapshot_version = f"snap-aws-dev-{aws_dev_config.run_id}-tuples"
        snapshot = _build_snapshot(snapshot_version=snapshot_version)

        store = SnapshotStore(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            kms_key_id=placeholder_kms_key,
        )
        loader = SnapshotLoader(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            expected_snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        )

        snapshot_key = f"snapshots/{snapshot_version}.json"
        snapshot_cleanup.append(snapshot_key)

        store.store(snapshot)
        loaded = loader.load(snapshot_version)

        # tuple[str, ...] deve ser preservada como tuple de strings
        blocked = loaded.data["blocked_accounts"]
        assert isinstance(blocked, tuple), f"esperado tuple, obtido {type(blocked)}"
        assert all(isinstance(item, str) for item in blocked)
        assert blocked == ("acc_aws_001", "acc_aws_002")

        # tuple[str, ...] com moedas
        currencies = loaded.data["allowed_currencies"]
        assert isinstance(currencies, tuple), f"esperado tuple, obtido {type(currencies)}"
        assert currencies == ("BRL", "USD", "EUR")

        # tuple[int, ...] deve ser preservada como tuple de inteiros
        tiers = loaded.data["tier_limits"]
        assert isinstance(tiers, tuple), f"esperado tuple, obtido {type(tiers)}"
        assert all(isinstance(item, int) for item in tiers)
        assert tiers == (10_000, 100_000, 500_000)


# ---------------------------------------------------------------------------
# 3. TestAWSKMSEncryption
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
class TestAWSKMSEncryption:
    """
    Testes de criptografia SSE-KMS para bundles e snapshots no S3 real.

    Verifica que os objetos armazenados usam criptografia KMS (SSE-KMS)
    inspecionando a resposta do HeadObject após o armazenamento.

    Estes testes requerem VALIDATION_ENGINE_TEST_KMS_KEY_ARN definido.
    Se a variável não estiver definida, os testes são pulados automaticamente.

    Requisitos: 3.6, 20.1
    """

    def test_bundle_stored_with_sse_kms(
        self,
        aws_dev_config,
        aws_dev_s3_client,
        kms_key_arn,
        bundle_cleanup,
    ) -> None:
        """
        Bundle armazenado no S3 real deve usar criptografia SSE-KMS.

        Verifica que o HeadObject retorna ServerSideEncryption='aws:kms'
        e que o SSEKMSKeyId corresponde à chave KMS configurada para testes.
        """
        policy_set_id = f"aws-dev-test-kms-bundle-{aws_dev_config.run_id}"
        bundle = _build_bundle(policy_set_id=policy_set_id, kms_key_id=kms_key_arn)

        store = BundleStore(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            kms_key_id=kms_key_arn,
        )

        bundle_key = f"bundles/{bundle.artifact_hash}.json"
        bundle_cleanup.append(bundle_key)

        store.store(bundle)

        # Inspecionar metadados do objeto para verificar criptografia KMS
        head_response = aws_dev_s3_client.head_object(
            Bucket=aws_dev_config.bucket, Key=bundle_key
        )

        # Verificar que SSE-KMS está ativo
        assert head_response.get("ServerSideEncryption") == "aws:kms", (
            f"Esperado ServerSideEncryption='aws:kms', "
            f"obtido: {head_response.get('ServerSideEncryption')}"
        )

        # Verificar que a chave KMS usada corresponde à chave configurada para testes.
        # O ARN retornado pelo S3 pode ser o ARN completo mesmo que o ID curto tenha sido usado.
        sse_kms_key_id = head_response.get("SSEKMSKeyId", "")
        assert sse_kms_key_id, "SSEKMSKeyId não presente na resposta do HeadObject"

        # Verificar que a chave KMS usada contém o identificador da chave de testes.
        # O S3 pode retornar o ARN completo mesmo que o alias tenha sido fornecido.
        assert kms_key_arn in sse_kms_key_id or sse_kms_key_id in kms_key_arn, (
            f"SSEKMSKeyId '{sse_kms_key_id}' não corresponde à chave de testes '{kms_key_arn}'"
        )

    def test_snapshot_stored_with_sse_kms(
        self,
        aws_dev_config,
        aws_dev_s3_client,
        kms_key_arn,
        snapshot_cleanup,
    ) -> None:
        """
        Snapshot armazenado no S3 real deve usar criptografia SSE-KMS.

        Verifica que o HeadObject retorna ServerSideEncryption='aws:kms'
        e que o SSEKMSKeyId corresponde à chave KMS configurada para testes.
        """
        snapshot_version = f"snap-aws-dev-{aws_dev_config.run_id}-kms"
        snapshot = _build_snapshot(snapshot_version=snapshot_version)

        store = SnapshotStore(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            kms_key_id=kms_key_arn,
        )

        snapshot_key = f"snapshots/{snapshot_version}.json"
        snapshot_cleanup.append(snapshot_key)

        store.store(snapshot)

        # Inspecionar metadados do objeto para verificar criptografia KMS
        head_response = aws_dev_s3_client.head_object(
            Bucket=aws_dev_config.bucket, Key=snapshot_key
        )

        # Verificar que SSE-KMS está ativo
        assert head_response.get("ServerSideEncryption") == "aws:kms", (
            f"Esperado ServerSideEncryption='aws:kms', "
            f"obtido: {head_response.get('ServerSideEncryption')}"
        )

        # Verificar que a chave KMS usada corresponde à chave configurada para testes
        sse_kms_key_id = head_response.get("SSEKMSKeyId", "")
        assert sse_kms_key_id, "SSEKMSKeyId não presente na resposta do HeadObject"

        assert kms_key_arn in sse_kms_key_id or sse_kms_key_id in kms_key_arn, (
            f"SSEKMSKeyId '{sse_kms_key_id}' não corresponde à chave de testes '{kms_key_arn}'"
        )


# ---------------------------------------------------------------------------
# 4. TestAWSTestPrefixIsolation
# ---------------------------------------------------------------------------


@pytest.mark.integration_aws_dev
class TestAWSTestPrefixIsolation:
    """
    Testes de isolamento por run_id para evitar colisão entre sessões de teste.

    Verifica que o run_id embutido no policy_set_id e snapshot_version garante
    unicidade dos artefatos por sessão de testes, evitando colisão entre
    execuções paralelas ou sequenciais.

    Requisitos: 3.1, 3.2 (isolamento de artefatos de teste)
    """

    def test_bundle_key_contains_run_id_in_policy_set_id(
        self,
        aws_dev_config,
        aws_dev_s3_client,
        placeholder_kms_key,
        bundle_cleanup,
    ) -> None:
        """
        O bundle armazenado deve usar run_id no policy_set_id para isolamento.

        Verifica que o policy_set_id do bundle carregado contém o run_id
        desta sessão de testes, garantindo que o artefato é único por sessão.
        """
        # O policy_set_id inclui o run_id para garantir unicidade por sessão
        policy_set_id = f"aws-dev-isolation-test-{aws_dev_config.run_id}"
        bundle = _build_bundle(policy_set_id=policy_set_id, kms_key_id=placeholder_kms_key)

        store = BundleStore(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            kms_key_id=placeholder_kms_key,
        )
        loader = BundleLoader(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            current_context_schema_version=CONTEXT_SCHEMA_VERSION,
            current_evaluator_version=EVALUATOR_VERSION,
        )

        bundle_key = f"bundles/{bundle.artifact_hash}.json"
        bundle_cleanup.append(bundle_key)

        store.store(bundle)
        loaded = loader.load(bundle.artifact_hash)

        # Verificar que o run_id está presente no policy_set_id do bundle carregado
        assert aws_dev_config.run_id in loaded.policy_set_id, (
            f"run_id '{aws_dev_config.run_id}' não encontrado no policy_set_id "
            f"'{loaded.policy_set_id}' — isolamento por sessão comprometido"
        )

    def test_different_run_ids_do_not_collide(
        self,
        aws_dev_config,
        aws_dev_s3_client,
        placeholder_kms_key,
        bundle_cleanup,
    ) -> None:
        """
        Dois bundles com run_ids diferentes no policy_set_id devem ter artifact_hashes distintos.

        Verifica que a estratégia de isolamento por run_id funciona corretamente:
        bundles de sessões diferentes produzem chaves S3 diferentes e não colidem.

        Este teste simula dois run_ids distintos para verificar que os artifact_hashes
        resultantes são diferentes, garantindo que não há colisão de chaves S3.
        """
        import uuid

        # Simular dois run_ids distintos (o atual e um fictício)
        run_id_a = aws_dev_config.run_id
        run_id_b = str(uuid.uuid4())

        policy_set_id_a = f"aws-dev-collision-test-{run_id_a}"
        policy_set_id_b = f"aws-dev-collision-test-{run_id_b}"

        bundle_a = _build_bundle(policy_set_id=policy_set_id_a, kms_key_id=placeholder_kms_key)
        bundle_b = _build_bundle(policy_set_id=policy_set_id_b, kms_key_id=placeholder_kms_key)

        # Os artifact_hashes devem ser diferentes porque os policy_set_ids são diferentes
        assert bundle_a.artifact_hash != bundle_b.artifact_hash, (
            "Bundles com policy_set_ids diferentes devem ter artifact_hashes distintos. "
            "Colisão de hashes indica problema na estratégia de isolamento por run_id."
        )

        # Armazenar apenas o bundle da sessão atual (bundle_a)
        store = BundleStore(
            s3_client=aws_dev_s3_client,
            bucket_name=aws_dev_config.bucket,
            kms_key_id=placeholder_kms_key,
        )

        bundle_key_a = f"bundles/{bundle_a.artifact_hash}.json"
        bundle_cleanup.append(bundle_key_a)

        store.store(bundle_a)

        # Verificar que a chave do bundle_b não existe no S3
        # (nunca foi armazenado — run_id diferente)
        bundle_key_b = f"bundles/{bundle_b.artifact_hash}.json"
        try:
            aws_dev_s3_client.head_object(Bucket=aws_dev_config.bucket, Key=bundle_key_b)
            # Se chegou aqui, o objeto existe — isso pode ser de uma sessão anterior
            # que usou o mesmo UUID (extremamente improvável com UUID v4)
            # Não falhar o teste por isso — apenas logar
            logger.warning(
                "bundle_b encontrado no S3 — possível colisão de UUID (extremamente improvável)",
                extra={"key": bundle_key_b, "run_id_b": run_id_b},
            )
        except Exception:
            # Comportamento esperado: bundle_b não existe no S3
            pass

        # A verificação principal é que os hashes são diferentes
        assert bundle_a.artifact_hash != bundle_b.artifact_hash
