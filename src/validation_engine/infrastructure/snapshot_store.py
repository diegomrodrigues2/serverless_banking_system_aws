"""
SnapshotStore — armazenamento imutável de ReferenceSnapshot no S3 com Object Lock (WORM).

Responsabilidade:
    Persistir ReferenceSnapshots de forma idempotente e determinística.
    Um snapshot armazenado nunca é sobrescrito — o S3 com Object Lock garante
    imutabilidade física; a idempotência por chave garante que a mesma
    operação seja segura de repetir.

Chave de armazenamento:
    snapshots/{snapshot_version}.json

Serialização:
    JSON UTF-8 determinístico (sort_keys=True).
    Tuples são serializadas como listas JSON — o SnapshotLoader é responsável
    por restaurar os tipos corretos na desserialização.

    Tipos suportados no campo `data`:
    - int, str, bool:         serializados diretamente
    - tuple[str, ...]:        serializado como lista de strings
    - tuple[int, ...]:        serializado como lista de inteiros

Idempotência:
    Se o objeto já existir no S3, a operação é ignorada silenciosamente.
    Isso é seguro porque snapshot_version é o identificador único e imutável.

Segurança:
    Todos os objetos são armazenados com SSE-KMS usando o kms_key_id fornecido.

Requisitos cobertos: 3.2, 3.4, 3.5, 3.6, 11.1, 11.4
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import botocore.exceptions

from validation_engine.domain.errors import PolicySnapshotUnavailable

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

    from validation_engine.domain.models import ReferenceSnapshot

logger = logging.getLogger(__name__)


class SnapshotStore:
    """
    Adaptador de infraestrutura para armazenamento de ReferenceSnapshots no S3.

    O bucket deve ter Object Lock habilitado (modo GOVERNANCE ou COMPLIANCE)
    para garantir imutabilidade física dos artefatos. O versionamento é
    obrigatório como pré-requisito do Object Lock.

    Uso típico (Control Plane):
        store = SnapshotStore(s3_client, bucket_name="my-bundles", kms_key_id="arn:aws:kms:...")
        store.store(snapshot)  # idempotente — seguro de chamar múltiplas vezes
    """

    def __init__(
        self,
        s3_client: "S3Client",
        bucket_name: str,
        kms_key_id: str,
    ) -> None:
        """
        Inicializa o SnapshotStore.

        Args:
            s3_client:   cliente boto3 S3 já configurado com credenciais e região.
            bucket_name: nome do bucket S3 com Object Lock habilitado.
            kms_key_id:  ARN ou ID da chave KMS para SSE-KMS no PutObject.
        """
        self._s3 = s3_client
        self._bucket = bucket_name
        self._kms_key_id = kms_key_id

    def store(self, snapshot: "ReferenceSnapshot") -> None:
        """
        Armazena um ReferenceSnapshot no S3 de forma idempotente.

        Se um objeto com a mesma chave (snapshot_version) já existir, a operação
        é ignorada silenciosamente. Isso é seguro porque snapshot_version é o
        identificador único e imutável do snapshot.

        Args:
            snapshot: ReferenceSnapshot a ser armazenado.

        Raises:
            PolicySnapshotUnavailable: se ocorrer erro de I/O ao verificar
                                       existência ou ao escrever no S3.
        """
        key = self._object_key(snapshot.snapshot_version)

        # Verificar idempotência antes de escrever — evita PutObject desnecessário
        # em buckets com Object Lock, onde sobrescrever pode ser proibido.
        if self._exists(key):
            logger.info(
                "snapshot já existe no storage — operação idempotente ignorada",
                extra={
                    "snapshot_version": snapshot.snapshot_version,
                    "key": key,
                    "bucket": self._bucket,
                },
            )
            return

        # Serializar o snapshot para JSON UTF-8 determinístico.
        # Tuples são convertidas para listas — o SnapshotLoader restaura os tipos.
        serialized_content = self._serialize(snapshot)
        content_bytes = serialized_content.encode("utf-8")

        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content_bytes,
                ContentType="application/json",
                # SSE-KMS obrigatório — requisito de segurança 3.6 e 20.1
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=self._kms_key_id,
            )
        except botocore.exceptions.ClientError as error:
            # Propagar como erro de domínio para desacoplar a camada de aplicação
            # de detalhes de infraestrutura boto3.
            raise PolicySnapshotUnavailable(
                f"Falha ao armazenar snapshot '{snapshot.snapshot_version}' no S3: {error}"
            ) from error

        logger.info(
            "snapshot armazenado com sucesso no S3",
            extra={
                "snapshot_version": snapshot.snapshot_version,
                "key": key,
                "bucket": self._bucket,
                "size_bytes": len(content_bytes),
            },
        )

    def _object_key(self, snapshot_version: str) -> str:
        """
        Retorna a chave S3 para um snapshot identificado pelo snapshot_version.

        Formato: snapshots/{snapshot_version}.json

        O prefixo 'snapshots/' separa snapshots de bundles no mesmo bucket,
        permitindo políticas de IAM e lifecycle distintas por prefixo.

        Args:
            snapshot_version: identificador único e imutável do snapshot.

        Returns:
            Chave S3 no formato 'snapshots/{snapshot_version}.json'.
        """
        return f"snapshots/{snapshot_version}.json"

    def _exists(self, key: str) -> bool:
        """
        Verifica se um objeto já existe no bucket S3.

        Usa HeadObject para verificar existência sem baixar o conteúdo.
        Retorna False para qualquer erro 404 (objeto não encontrado).
        Propaga PolicySnapshotUnavailable para outros erros de I/O.

        Args:
            key: chave S3 a verificar.

        Returns:
            True se o objeto existir, False caso contrário.

        Raises:
            PolicySnapshotUnavailable: se ocorrer erro de I/O diferente de 404.
        """
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except botocore.exceptions.ClientError as error:
            # O código de erro 404 indica que o objeto não existe — comportamento esperado.
            error_code = error.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                return False
            # Qualquer outro erro (403, 500, timeout) é uma falha de infraestrutura.
            raise PolicySnapshotUnavailable(
                f"Falha ao verificar existência do objeto '{key}' no S3: {error}"
            ) from error

    def _serialize(self, snapshot: "ReferenceSnapshot") -> str:
        """
        Serializa um ReferenceSnapshot para JSON UTF-8 determinístico.

        Converte o snapshot para um dicionário serializável e aplica
        json.dumps com sort_keys=True para garantir determinismo.

        Tratamento de tipos no campo `data`:
        - int, str, bool:         serializados diretamente pelo json.dumps
        - tuple[str, ...]:        convertido para lista de strings
        - tuple[int, ...]:        convertido para lista de inteiros

        O SnapshotLoader é responsável por restaurar tuples na desserialização,
        usando o tipo dos elementos para inferir o tipo correto.

        Args:
            snapshot: ReferenceSnapshot a serializar.

        Returns:
            String JSON UTF-8 determinística.
        """
        # Converter tuples para listas para compatibilidade com JSON.
        # JSON não distingue listas de tuples — a semântica de imutabilidade
        # é restaurada pelo SnapshotLoader ao desserializar.
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
