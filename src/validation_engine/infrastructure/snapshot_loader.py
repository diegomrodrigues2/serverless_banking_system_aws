"""
SnapshotLoader — carregamento de ReferenceSnapshot do S3 com cache em memória.

Responsabilidade:
    Carregar ReferenceSnapshots do S3 de forma eficiente, verificando
    compatibilidade de schema antes de disponibilizá-los para o
    PolicyRuntimeRegistry.

Estratégia de cache:
    Cache em memória (dict) indexado por snapshot_version. Em steady state,
    snapshots já carregados são servidos diretamente do cache sem I/O.
    O cache é invalidado explicitamente via invalidate() para cenários
    de refresh forçado.

Verificação de schema:
    Valida que snapshot.snapshot_schema_version corresponde à versão
    esperada pelo runtime. Incompatibilidade levanta PolicySnapshotUnavailable
    com mensagem clara indicando as versões envolvidas.

Desserialização:
    JSON → ReferenceSnapshot com restauração de tipos:
    - Listas de strings → tuple[str, ...]
    - Listas de inteiros → tuple[int, ...]
    - Escalares (int, str, bool) → inalterados

    O SnapshotStore serializa tuples como listas JSON (JSON não distingue
    listas de tuples). O SnapshotLoader é responsável por restaurar a
    semântica de imutabilidade usando o tipo dos elementos.

Erros levantados:
    - PolicySnapshotUnavailable: objeto não encontrado no S3, erro de I/O
                                 ou schema incompatível

Requisitos cobertos: 3.3, 3.4, 11.6, 17.3, 20.3, 20.4
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import botocore.exceptions

from validation_engine.domain.errors import PolicySnapshotUnavailable
from validation_engine.domain.models import ReferenceSnapshot

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

logger = logging.getLogger(__name__)


class SnapshotLoader:
    """
    Adaptador de infraestrutura para carregamento de ReferenceSnapshots do S3.

    Mantém cache em memória para evitar leituras repetidas do S3 em steady state.
    Cada snapshot carregado passa por verificação de compatibilidade de schema
    antes de ser disponibilizado.

    Uso típico (Data Plane — PolicyRuntimeRegistry):
        loader = SnapshotLoader(
            s3_client=s3,
            bucket_name="my-bundles",
            expected_snapshot_schema_version="1.0",
        )
        snapshot = loader.load("snap_2026_03_11_001")  # cache miss → S3 → verifica → cacheia
        snapshot = loader.load("snap_2026_03_11_001")  # cache hit → retorna direto
    """

    def __init__(
        self,
        s3_client: "S3Client",
        bucket_name: str,
        expected_snapshot_schema_version: str,
    ) -> None:
        """
        Inicializa o SnapshotLoader.

        Args:
            s3_client:                        cliente boto3 S3 configurado.
            bucket_name:                      nome do bucket S3 com os snapshots.
            expected_snapshot_schema_version: versão do schema de snapshot
                                              suportada pelo runtime atual.
        """
        self._s3 = s3_client
        self._bucket = bucket_name
        self._expected_snapshot_schema_version = expected_snapshot_schema_version

        # Cache em memória: snapshot_version → ReferenceSnapshot já verificado.
        # Evita leituras repetidas do S3 em steady state (Requisito 6.3, 19.3).
        self._cache: dict[str, ReferenceSnapshot] = {}

    def load(self, snapshot_version: str) -> ReferenceSnapshot:
        """
        Carrega um ReferenceSnapshot pelo snapshot_version.

        Em cache hit: retorna o snapshot diretamente do cache sem I/O.
        Em cache miss: busca no S3, desserializa, verifica compatibilidade
        de schema, armazena no cache e retorna.

        Args:
            snapshot_version: identificador único e imutável do snapshot.

        Returns:
            ReferenceSnapshot verificado e compatível com o runtime atual.

        Raises:
            PolicySnapshotUnavailable: se o objeto não existir no S3,
                                       ocorrer erro de I/O, ou o schema
                                       for incompatível com o runtime.
        """
        # Verificar cache antes de qualquer I/O — hot path em steady state.
        if snapshot_version in self._cache:
            logger.debug(
                "snapshot servido do cache em memória",
                extra={"snapshot_version": snapshot_version},
            )
            return self._cache[snapshot_version]

        logger.info(
            "cache miss — carregando snapshot do S3",
            extra={"snapshot_version": snapshot_version, "bucket": self._bucket},
        )

        # Buscar JSON bruto do S3.
        raw_json = self._fetch_from_s3(snapshot_version)

        # Desserializar o snapshot com restauração de tipos (tuples).
        snapshot = self._deserialize(raw_json)

        # Verificar compatibilidade de schema após desserialização.
        # Incompatibilidade indica que o manifesto foi ativado com snapshot errado.
        self._verify_schema_compatibility(snapshot)

        # Armazenar no cache apenas após verificação de compatibilidade.
        # Snapshots incompatíveis nunca entram no cache.
        self._cache[snapshot_version] = snapshot

        logger.info(
            "snapshot carregado, verificado e armazenado em cache",
            extra={
                "snapshot_version": snapshot.snapshot_version,
                "snapshot_schema_version": snapshot.snapshot_schema_version,
                "data_keys_count": len(snapshot.data),
            },
        )

        return snapshot

    def invalidate(self, snapshot_version: str) -> None:
        """
        Remove um snapshot do cache em memória.

        Usado em cenários de refresh forçado, onde o PolicyRuntimeRegistry
        precisa garantir que o próximo load() busque o snapshot atualizado do S3.

        Args:
            snapshot_version: identificador do snapshot a remover do cache.
        """
        removed = self._cache.pop(snapshot_version, None)
        if removed is not None:
            logger.info(
                "snapshot removido do cache",
                extra={"snapshot_version": snapshot_version},
            )
        else:
            logger.debug(
                "invalidate chamado para snapshot não presente no cache",
                extra={"snapshot_version": snapshot_version},
            )

    def _object_key(self, snapshot_version: str) -> str:
        """
        Retorna a chave S3 para um snapshot identificado pelo snapshot_version.

        Formato: snapshots/{snapshot_version}.json

        Mantém consistência com o SnapshotStore, que usa o mesmo formato de chave.

        Args:
            snapshot_version: identificador único e imutável do snapshot.

        Returns:
            Chave S3 no formato 'snapshots/{snapshot_version}.json'.
        """
        return f"snapshots/{snapshot_version}.json"

    def _fetch_from_s3(self, snapshot_version: str) -> str:
        """
        Busca o JSON bruto de um snapshot no S3.

        Retorna o conteúdo como string UTF-8 sem nenhuma transformação.
        A desserialização e verificação de schema são responsabilidade
        do chamador (método load).

        Args:
            snapshot_version: identificador do snapshot a buscar.

        Returns:
            Conteúdo JSON bruto como string UTF-8.

        Raises:
            PolicySnapshotUnavailable: se o objeto não existir (404) ou
                                       ocorrer qualquer erro de I/O.
        """
        key = self._object_key(snapshot_version)
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
            raw_bytes = response["Body"].read()
            return raw_bytes.decode("utf-8")
        except botocore.exceptions.ClientError as error:
            error_code = error.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                raise PolicySnapshotUnavailable(
                    f"Snapshot '{snapshot_version}' não encontrado no S3 (chave: {key})"
                ) from error
            raise PolicySnapshotUnavailable(
                f"Erro de I/O ao carregar snapshot '{snapshot_version}' do S3: {error}"
            ) from error

    def _deserialize(self, raw_json: str) -> ReferenceSnapshot:
        """
        Desserializa um ReferenceSnapshot a partir de JSON bruto.

        Restaura os tipos corretos para o campo `data`:
        - Listas de strings → tuple[str, ...]
        - Listas de inteiros → tuple[int, ...]
        - Escalares (int, str, bool) → inalterados

        O SnapshotStore serializa tuples como listas JSON porque JSON não
        distingue listas de tuples. A restauração usa o tipo dos elementos
        da lista para inferir o tipo correto da tuple.

        Regra de inferência de tipo:
        - Se todos os elementos forem str → tuple[str, ...]
        - Se todos os elementos forem int → tuple[int, ...]
        - Lista vazia → tuple vazia (tuple[str, ...] por convenção)

        Args:
            raw_json: JSON bruto carregado do S3.

        Returns:
            ReferenceSnapshot com tipos corretos restaurados.

        Raises:
            PolicySnapshotUnavailable: se o JSON for inválido ou estiver
                                       com estrutura inesperada.
        """
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as error:
            raise PolicySnapshotUnavailable(
                f"Snapshot com JSON inválido: {error}"
            ) from error

        # Restaurar tipos do campo data: listas JSON → tuples Python.
        # O SnapshotStore converte tuples para listas na serialização;
        # aqui revertemos essa conversão para manter a semântica de imutabilidade.
        restored_data: dict[str, int | str | bool | tuple[str, ...] | tuple[int, ...]] = {}
        for key, value in payload.get("data", {}).items():
            restored_data[key] = _restore_tuple_type(value)

        return ReferenceSnapshot(
            snapshot_version=payload["snapshot_version"],
            snapshot_schema_version=payload["snapshot_schema_version"],
            created_at=payload["created_at"],
            data=restored_data,
        )

    def _verify_schema_compatibility(self, snapshot: ReferenceSnapshot) -> None:
        """
        Verifica compatibilidade do schema do snapshot com o runtime atual.

        Valida que snapshot.snapshot_schema_version corresponde à versão
        esperada pelo runtime. A comparação usa igualdade exata de string.

        Args:
            snapshot: ReferenceSnapshot a verificar.

        Raises:
            PolicySnapshotUnavailable: se o schema_version for incompatível.
                                       Usa PolicySnapshotUnavailable (não
                                       InvalidPolicyBundle) conforme especificado
                                       no design — snapshot incompatível é tratado
                                       como indisponibilidade para o runtime.
        """
        snapshot_schema = snapshot.snapshot_schema_version
        if snapshot_schema != self._expected_snapshot_schema_version:
            logger.error(
                "snapshot incompatível — snapshot_schema_version divergente",
                extra={
                    "snapshot_schema_version": snapshot_schema,
                    "expected_snapshot_schema_version": self._expected_snapshot_schema_version,
                    "snapshot_version": snapshot.snapshot_version,
                },
            )
            raise PolicySnapshotUnavailable(
                f"Snapshot incompatível: snapshot_schema_version é '{snapshot_schema}', "
                f"runtime espera '{self._expected_snapshot_schema_version}'"
            )


def _restore_tuple_type(
    value: object,
) -> int | str | bool | tuple[str, ...] | tuple[int, ...]:
    """
    Restaura o tipo correto de um valor desserializado do JSON.

    JSON não distingue listas de tuples. Esta função converte listas
    de volta para tuples usando o tipo dos elementos como critério:
    - Lista de strings → tuple[str, ...]
    - Lista de inteiros → tuple[int, ...]
    - Lista vazia → tuple vazia
    - Escalares (int, str, bool) → inalterados

    Args:
        value: valor desserializado do JSON.

    Returns:
        Valor com tipo correto restaurado.
    """
    if not isinstance(value, list):
        # Escalares (int, str, bool) são retornados sem transformação.
        return value  # type: ignore[return-value]

    if len(value) == 0:
        # Lista vazia → tuple vazia (sem elementos para inferir tipo).
        return ()

    # Inferir tipo da tuple pelo tipo do primeiro elemento.
    # O SnapshotStore garante homogeneidade de tipos nas listas serializadas.
    first_element = value[0]
    if isinstance(first_element, str):
        return tuple(str(item) for item in value)
    if isinstance(first_element, int):
        return tuple(int(item) for item in value)

    # Fallback: retornar como tuple genérica para tipos inesperados.
    # Isso não deveria ocorrer com snapshots bem formados.
    return tuple(value)  # type: ignore[return-value]
