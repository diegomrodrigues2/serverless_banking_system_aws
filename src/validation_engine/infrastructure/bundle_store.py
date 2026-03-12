"""
BundleStore — armazenamento imutável de RuleBundle no S3 com Object Lock (WORM).

Responsabilidade:
    Persistir RuleBundles compilados de forma idempotente e determinística.
    Um bundle armazenado nunca é sobrescrito — o S3 com Object Lock garante
    imutabilidade física; a idempotência por chave garante que a mesma
    operação seja segura de repetir.

Chave de armazenamento:
    bundles/{artifact_hash}.json

Serialização:
    Usa RuleBundle.to_json() — JSON UTF-8 determinístico com sort_keys=True.

Idempotência:
    Se o objeto já existir no S3, a operação é ignorada silenciosamente.
    Isso é seguro porque o artifact_hash é SHA-256 do conteúdo: mesma chave
    implica mesmo conteúdo.

Segurança:
    Todos os objetos são armazenados com SSE-KMS usando o kms_key_id fornecido.

Requisitos cobertos: 3.1, 3.3, 3.4, 3.5, 3.6
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import botocore.exceptions

from validation_engine.domain.errors import PolicyBundleUnavailable

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

    from validation_engine.domain.models import RuleBundle

logger = logging.getLogger(__name__)


class BundleStore:
    """
    Adaptador de infraestrutura para armazenamento de RuleBundles no S3.

    O bucket deve ter Object Lock habilitado (modo GOVERNANCE ou COMPLIANCE)
    para garantir imutabilidade física dos artefatos. O versionamento é
    obrigatório como pré-requisito do Object Lock.

    Uso típico (Control Plane):
        store = BundleStore(s3_client, bucket_name="my-bundles", kms_key_id="arn:aws:kms:...")
        store.store(bundle)  # idempotente — seguro de chamar múltiplas vezes
    """

    def __init__(
        self,
        s3_client: "S3Client",
        bucket_name: str,
        kms_key_id: str,
    ) -> None:
        """
        Inicializa o BundleStore.

        Args:
            s3_client:   cliente boto3 S3 já configurado com credenciais e região.
            bucket_name: nome do bucket S3 com Object Lock habilitado.
            kms_key_id:  ARN ou ID da chave KMS para SSE-KMS no PutObject.
        """
        self._s3 = s3_client
        self._bucket = bucket_name
        self._kms_key_id = kms_key_id

    def store(self, bundle: "RuleBundle") -> None:
        """
        Armazena um RuleBundle no S3 de forma idempotente.

        Se um objeto com a mesma chave (artifact_hash) já existir, a operação
        é ignorada silenciosamente. Isso é seguro porque o artifact_hash é
        SHA-256 do conteúdo: mesma chave implica mesmo conteúdo.

        Args:
            bundle: RuleBundle compilado a ser armazenado.

        Raises:
            PolicyBundleUnavailable: se ocorrer erro de I/O ao verificar
                                     existência ou ao escrever no S3.
        """
        key = self._object_key(bundle.artifact_hash)

        # Verificar idempotência antes de escrever — evita PutObject desnecessário
        # em buckets com Object Lock, onde sobrescrever pode ser proibido.
        if self._exists(key):
            logger.info(
                "bundle já existe no storage — operação idempotente ignorada",
                extra={
                    "artifact_hash": bundle.artifact_hash,
                    "policy_set_id": bundle.policy_set_id,
                    "key": key,
                    "bucket": self._bucket,
                },
            )
            return

        # Serializar o bundle para JSON UTF-8 determinístico.
        # RuleBundle.to_json() usa sort_keys=True para garantir reprodutibilidade.
        serialized_content = bundle.to_json()
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
            raise PolicyBundleUnavailable(
                f"Falha ao armazenar bundle '{bundle.artifact_hash}' no S3: {error}"
            ) from error

        logger.info(
            "bundle armazenado com sucesso no S3",
            extra={
                "artifact_hash": bundle.artifact_hash,
                "policy_set_id": bundle.policy_set_id,
                "key": key,
                "bucket": self._bucket,
                "size_bytes": len(content_bytes),
            },
        )

    def _object_key(self, artifact_hash: str) -> str:
        """
        Retorna a chave S3 para um bundle identificado pelo artifact_hash.

        Formato: bundles/{artifact_hash}.json

        O prefixo 'bundles/' separa bundles de snapshots no mesmo bucket,
        permitindo políticas de IAM e lifecycle distintas por prefixo.

        Args:
            artifact_hash: SHA-256 do conteúdo serializado do bundle.

        Returns:
            Chave S3 no formato 'bundles/{artifact_hash}.json'.
        """
        return f"bundles/{artifact_hash}.json"

    def _exists(self, key: str) -> bool:
        """
        Verifica se um objeto já existe no bucket S3.

        Usa HeadObject para verificar existência sem baixar o conteúdo.
        Retorna False para qualquer erro 404 (objeto não encontrado).
        Propaga PolicyBundleUnavailable para outros erros de I/O.

        Args:
            key: chave S3 a verificar.

        Returns:
            True se o objeto existir, False caso contrário.

        Raises:
            PolicyBundleUnavailable: se ocorrer erro de I/O diferente de 404.
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
            raise PolicyBundleUnavailable(
                f"Falha ao verificar existência do objeto '{key}' no S3: {error}"
            ) from error
