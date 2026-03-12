"""
BundleLoader — carregamento de RuleBundle do S3 com cache em memória.

Responsabilidade:
    Carregar RuleBundles do S3 de forma eficiente, verificando integridade
    criptográfica e compatibilidade com o runtime antes de disponibilizá-los
    para o PolicyRuntimeRegistry.

Estratégia de cache:
    Cache em memória (dict) indexado por artifact_hash. Em steady state,
    bundles já carregados são servidos diretamente do cache sem I/O.
    O cache é invalidado explicitamente via invalidate() para cenários
    de refresh forçado.

Verificação de integridade:
    O artifact_hash é SHA-256 do conteúdo serializado do bundle EXCLUINDO
    o próprio campo "artifact_hash". Após carregar o JSON do S3, o loader
    recalcula o hash e compara com o esperado. Divergência levanta
    PolicyBundleIntegrityFailure.

Verificação de compatibilidade:
    Valida que bundle.compatibility.context_schema_version e
    bundle.compatibility.evaluator_min_version são compatíveis com o
    runtime atual. Incompatibilidade levanta InvalidPolicyBundle.

Erros levantados:
    - PolicyBundleUnavailable:      objeto não encontrado no S3 ou erro de I/O
    - PolicyBundleIntegrityFailure: hash calculado != hash esperado
    - InvalidPolicyBundle:          incompatibilidade de versão com o runtime

Requisitos cobertos: 3.3, 3.4, 11.6, 17.3, 20.3, 20.4
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

import botocore.exceptions

from validation_engine.domain.errors import (
    InvalidPolicyBundle,
    PolicyBundleIntegrityFailure,
    PolicyBundleUnavailable,
)
from validation_engine.domain.models import RuleBundle

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

logger = logging.getLogger(__name__)


class BundleLoader:
    """
    Adaptador de infraestrutura para carregamento de RuleBundles do S3.

    Mantém cache em memória para evitar leituras repetidas do S3 em steady state.
    Cada bundle carregado passa por verificação de integridade (SHA-256) e
    verificação de compatibilidade com o runtime antes de ser disponibilizado.

    Uso típico (Data Plane — PolicyRuntimeRegistry):
        loader = BundleLoader(
            s3_client=s3,
            bucket_name="my-bundles",
            current_context_schema_version="1.0",
            current_evaluator_version="1.2.0",
        )
        bundle = loader.load("sha256:abc123...")  # cache miss → S3 → verifica → cacheia
        bundle = loader.load("sha256:abc123...")  # cache hit → retorna direto
    """

    def __init__(
        self,
        s3_client: "S3Client",
        bucket_name: str,
        current_context_schema_version: str,
        current_evaluator_version: str,
    ) -> None:
        """
        Inicializa o BundleLoader.

        Args:
            s3_client:                       cliente boto3 S3 configurado.
            bucket_name:                     nome do bucket S3 com os bundles.
            current_context_schema_version:  versão do schema do contexto canônico
                                             suportada pelo runtime atual.
            current_evaluator_version:       versão do evaluator em execução.
        """
        self._s3 = s3_client
        self._bucket = bucket_name
        self._current_context_schema_version = current_context_schema_version
        self._current_evaluator_version = current_evaluator_version

        # Cache em memória: artifact_hash → RuleBundle já verificado e compatível.
        # Evita leituras repetidas do S3 em steady state (Requisito 6.3, 19.3).
        self._cache: dict[str, RuleBundle] = {}

    def load(self, artifact_hash: str) -> RuleBundle:
        """
        Carrega um RuleBundle pelo artifact_hash.

        Em cache hit: retorna o bundle diretamente do cache sem I/O.
        Em cache miss: busca no S3, verifica integridade e compatibilidade,
        armazena no cache e retorna.

        Args:
            artifact_hash: SHA-256 do conteúdo serializado do bundle.

        Returns:
            RuleBundle verificado e compatível com o runtime atual.

        Raises:
            PolicyBundleUnavailable:      se o objeto não existir no S3 ou
                                          ocorrer erro de I/O.
            PolicyBundleIntegrityFailure: se o hash calculado divergir do
                                          artifact_hash esperado.
            InvalidPolicyBundle:          se o bundle for incompatível com
                                          o runtime atual.
        """
        # Verificar cache antes de qualquer I/O — hot path em steady state.
        if artifact_hash in self._cache:
            logger.debug(
                "bundle servido do cache em memória",
                extra={"artifact_hash": artifact_hash},
            )
            return self._cache[artifact_hash]

        logger.info(
            "cache miss — carregando bundle do S3",
            extra={"artifact_hash": artifact_hash, "bucket": self._bucket},
        )

        # Buscar JSON bruto do S3.
        raw_json = self._fetch_from_s3(artifact_hash)

        # Verificar integridade criptográfica antes de desserializar.
        # Isso garante que o conteúdo não foi corrompido ou adulterado (Requisito 20.3).
        self._verify_integrity(raw_json, artifact_hash)

        # Desserializar o bundle após confirmação de integridade.
        bundle = RuleBundle.from_json(raw_json)

        # Verificar compatibilidade com o runtime atual antes de aceitar o bundle.
        # Incompatibilidade aqui indica que o manifesto foi ativado com versões erradas.
        self._verify_compatibility(bundle)

        # Armazenar no cache apenas após todas as verificações passarem.
        # Bundles inválidos nunca entram no cache.
        self._cache[artifact_hash] = bundle

        logger.info(
            "bundle carregado, verificado e armazenado em cache",
            extra={
                "artifact_hash": artifact_hash,
                "policy_set_id": bundle.policy_set_id,
                "context_schema_version": bundle.compatibility.context_schema_version,
                "evaluator_min_version": bundle.compatibility.evaluator_min_version,
            },
        )

        return bundle

    def invalidate(self, artifact_hash: str) -> None:
        """
        Remove um bundle do cache em memória.

        Usado em cenários de refresh forçado, onde o PolicyRuntimeRegistry
        precisa garantir que o próximo load() busque o bundle atualizado do S3.

        Args:
            artifact_hash: SHA-256 do bundle a remover do cache.
        """
        removed = self._cache.pop(artifact_hash, None)
        if removed is not None:
            logger.info(
                "bundle removido do cache",
                extra={"artifact_hash": artifact_hash},
            )
        else:
            logger.debug(
                "invalidate chamado para bundle não presente no cache",
                extra={"artifact_hash": artifact_hash},
            )

    def _object_key(self, artifact_hash: str) -> str:
        """
        Retorna a chave S3 para um bundle identificado pelo artifact_hash.

        Formato: bundles/{artifact_hash}.json

        Mantém consistência com o BundleStore, que usa o mesmo formato de chave.

        Args:
            artifact_hash: SHA-256 do conteúdo serializado do bundle.

        Returns:
            Chave S3 no formato 'bundles/{artifact_hash}.json'.
        """
        return f"bundles/{artifact_hash}.json"

    def _fetch_from_s3(self, artifact_hash: str) -> str:
        """
        Busca o JSON bruto de um bundle no S3.

        Retorna o conteúdo como string UTF-8 sem nenhuma transformação.
        A verificação de integridade e desserialização são responsabilidade
        do chamador (método load).

        Args:
            artifact_hash: SHA-256 do bundle a buscar.

        Returns:
            Conteúdo JSON bruto como string UTF-8.

        Raises:
            PolicyBundleUnavailable: se o objeto não existir (404) ou
                                     ocorrer qualquer erro de I/O.
        """
        key = self._object_key(artifact_hash)
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
            raw_bytes = response["Body"].read()
            return raw_bytes.decode("utf-8")
        except botocore.exceptions.ClientError as error:
            error_code = error.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                raise PolicyBundleUnavailable(
                    f"Bundle '{artifact_hash}' não encontrado no S3 (chave: {key})"
                ) from error
            raise PolicyBundleUnavailable(
                f"Erro de I/O ao carregar bundle '{artifact_hash}' do S3: {error}"
            ) from error

    def _verify_integrity(self, raw_json: str, expected_hash: str) -> None:
        """
        Verifica a integridade criptográfica do bundle carregado.

        Algoritmo de verificação (Requisito 3.3, 20.3):
        1. Parsear o JSON bruto para dicionário.
        2. Remover o campo "artifact_hash" do dicionário.
        3. Re-serializar com sort_keys=True (mesmo algoritmo do DSLCompiler).
        4. Calcular SHA-256 do conteúdo re-serializado em UTF-8.
        5. Comparar com o expected_hash.

        A remoção do campo "artifact_hash" antes do cálculo é necessária
        porque o hash foi calculado sobre o conteúdo SEM o próprio campo,
        evitando dependência circular na geração do hash.

        Args:
            raw_json:      JSON bruto carregado do S3.
            expected_hash: artifact_hash esperado (do manifesto de ativação).

        Raises:
            PolicyBundleIntegrityFailure: se o hash calculado divergir do esperado.
        """
        # Parsear o JSON para poder remover o campo artifact_hash.
        parsed = json.loads(raw_json)

        # Remover o campo artifact_hash antes de recalcular o hash.
        # O hash foi gerado sobre o conteúdo sem este campo.
        content_without_hash = {k: v for k, v in parsed.items() if k != "artifact_hash"}

        # Re-serializar de forma determinística para garantir reprodutibilidade.
        canonical_content = json.dumps(content_without_hash, ensure_ascii=False, sort_keys=True)

        # Calcular SHA-256 do conteúdo canônico.
        computed_hash = hashlib.sha256(canonical_content.encode("utf-8")).hexdigest()

        if computed_hash != expected_hash:
            logger.error(
                "falha de integridade do bundle — hash divergente",
                extra={
                    "expected_hash": expected_hash,
                    "computed_hash": computed_hash,
                    "bucket": self._bucket,
                },
            )
            raise PolicyBundleIntegrityFailure(
                f"Falha de integridade do bundle: hash esperado '{expected_hash}', "
                f"hash calculado '{computed_hash}'"
            )

    def _verify_compatibility(self, bundle: RuleBundle) -> None:
        """
        Verifica compatibilidade do bundle com o runtime atual.

        Validações realizadas (Requisito 11.6, 24.3):
        1. context_schema_version: deve ser igual à versão suportada pelo runtime.
        2. evaluator_min_version:  deve ser igual à versão do evaluator em execução.

        A comparação usa igualdade exata de string. Versões semânticas mais
        sofisticadas (semver range) podem ser adicionadas futuramente sem
        quebrar a interface.

        Args:
            bundle: RuleBundle a verificar.

        Raises:
            InvalidPolicyBundle: se qualquer verificação de compatibilidade falhar.
        """
        # Verificar compatibilidade do schema do contexto canônico.
        # O bundle foi compilado para uma versão específica do contexto —
        # usar com versão diferente pode produzir resultados incorretos.
        bundle_context_version = bundle.compatibility.context_schema_version
        if bundle_context_version != self._current_context_schema_version:
            logger.error(
                "bundle incompatível — context_schema_version divergente",
                extra={
                    "bundle_context_schema_version": bundle_context_version,
                    "runtime_context_schema_version": self._current_context_schema_version,
                    "artifact_hash": bundle.artifact_hash,
                },
            )
            raise InvalidPolicyBundle(
                f"Bundle incompatível: context_schema_version do bundle é "
                f"'{bundle_context_version}', runtime suporta "
                f"'{self._current_context_schema_version}'"
            )

        # Verificar compatibilidade da versão mínima do evaluator.
        # O bundle declara a versão mínima necessária para avaliação correta.
        bundle_evaluator_min = bundle.compatibility.evaluator_min_version
        if bundle_evaluator_min != self._current_evaluator_version:
            logger.error(
                "bundle incompatível — evaluator_min_version divergente",
                extra={
                    "bundle_evaluator_min_version": bundle_evaluator_min,
                    "runtime_evaluator_version": self._current_evaluator_version,
                    "artifact_hash": bundle.artifact_hash,
                },
            )
            raise InvalidPolicyBundle(
                f"Bundle incompatível: evaluator_min_version do bundle é "
                f"'{bundle_evaluator_min}', runtime executa versão "
                f"'{self._current_evaluator_version}'"
            )
