"""
Fixtures para testes de integração contra AWS dev do Validation Engine.

Usa recursos AWS reais (S3, AppConfig, DynamoDB, Firehose) em ambiente dev.
Cada sessão de testes usa um prefixo único (UUID) para isolar artefatos de teste
e evitar colisão com dados de outros testes ou de produção.

Convenção de naming:
- Bucket: definido por VALIDATION_ENGINE_TEST_BUCKET
- Prefixo de teste: test/{run_id}/ onde run_id é UUID gerado por sessão
- Bundles: test/{run_id}/bundles/
- Snapshots: test/{run_id}/snapshots/
- Decision trails: test/{run_id}/trails/

Política de cleanup:
- Todos os objetos sob test/{run_id}/ são deletados ao final da sessão.
- O cleanup é best-effort: falhas de cleanup são logadas mas não falham os testes.

Variáveis de ambiente necessárias:
- VALIDATION_ENGINE_TEST_BUCKET: bucket S3 para bundles e snapshots de teste
- VALIDATION_ENGINE_TEST_PREFIX: prefixo base (opcional, padrão: "test/")
- VALIDATION_ENGINE_TEST_APPCONFIG_APP: nome da aplicação AppConfig de teste
- VALIDATION_ENGINE_TEST_DYNAMODB_TABLE: tabela DynamoDB de teste
- AWS_REGION: região AWS (padrão: us-east-1)

Uso:
    pytest tests/validation_engine/integration/aws_dev/ -v
    pytest -m integration_aws_dev -v
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field

import boto3
import pytest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuração do ambiente AWS dev
# ---------------------------------------------------------------------------

AWS_REGION_DEFAULT = "us-east-1"
TEST_PREFIX_BASE = "test/"


@dataclass(frozen=True)
class AWSDevTestConfig:
    """
    Configuração para testes de integração AWS dev.

    Lê variáveis de ambiente e gera um run_id único por sessão para
    isolar artefatos de teste. Todos os recursos criados durante a sessão
    ficam sob o prefixo test/{run_id}/.

    Attributes:
        bucket:              Bucket S3 para bundles e snapshots de teste.
        run_id:              UUID único gerado por sessão de testes.
        base_prefix:         Prefixo base configurável (padrão: "test/").
        appconfig_app:       Nome da aplicação AppConfig de teste.
        dynamodb_table:      Tabela DynamoDB de teste.
        region:              Região AWS.
    """

    bucket: str
    run_id: str
    base_prefix: str
    appconfig_app: str
    dynamodb_table: str
    region: str

    @property
    def test_prefix(self) -> str:
        """Prefixo completo de isolamento para esta sessão: test/{run_id}/"""
        return f"{self.base_prefix}{self.run_id}/"

    @property
    def bundles_prefix(self) -> str:
        """Prefixo para bundles de teste: test/{run_id}/bundles/"""
        return f"{self.test_prefix}bundles/"

    @property
    def snapshots_prefix(self) -> str:
        """Prefixo para snapshots de teste: test/{run_id}/snapshots/"""
        return f"{self.test_prefix}snapshots/"

    @property
    def trails_prefix(self) -> str:
        """Prefixo para decision trails de teste: test/{run_id}/trails/"""
        return f"{self.test_prefix}trails/"

    @classmethod
    def from_environment(cls) -> "AWSDevTestConfig":
        """
        Cria configuração a partir de variáveis de ambiente.

        Gera um run_id único para esta sessão de testes.

        Returns:
            AWSDevTestConfig populado com variáveis de ambiente.

        Raises:
            pytest.skip: Se VALIDATION_ENGINE_TEST_BUCKET não estiver definido.
        """
        bucket = os.environ.get("VALIDATION_ENGINE_TEST_BUCKET", "")
        if not bucket:
            pytest.skip(
                "VALIDATION_ENGINE_TEST_BUCKET não definido — "
                "testes de integração AWS dev ignorados"
            )

        return cls(
            bucket=bucket,
            run_id=str(uuid.uuid4()),
            base_prefix=os.environ.get("VALIDATION_ENGINE_TEST_PREFIX", TEST_PREFIX_BASE),
            appconfig_app=os.environ.get("VALIDATION_ENGINE_TEST_APPCONFIG_APP", ""),
            dynamodb_table=os.environ.get("VALIDATION_ENGINE_TEST_DYNAMODB_TABLE", ""),
            region=os.environ.get("AWS_REGION", AWS_REGION_DEFAULT),
        )


# ---------------------------------------------------------------------------
# Fixtures de sessão AWS dev
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def aws_dev_config() -> AWSDevTestConfig:
    """
    Configuração da sessão de testes AWS dev.

    Gera um run_id único para isolar todos os artefatos desta sessão.
    Ignora a sessão se as variáveis de ambiente necessárias não estiverem definidas.

    Returns:
        AWSDevTestConfig com run_id único para esta sessão.
    """
    return AWSDevTestConfig.from_environment()


@pytest.fixture(scope="session")
def aws_dev_s3_client(aws_dev_config: AWSDevTestConfig):
    """
    Cliente S3 apontando para AWS real em ambiente dev.

    Args:
        aws_dev_config: Configuração da sessão AWS dev.

    Returns:
        Cliente boto3 S3 configurado para a região do ambiente dev.
    """
    return boto3.client("s3", region_name=aws_dev_config.region)


@pytest.fixture(scope="session")
def aws_dev_dynamodb_client(aws_dev_config: AWSDevTestConfig):
    """
    Cliente DynamoDB apontando para AWS real em ambiente dev.

    Args:
        aws_dev_config: Configuração da sessão AWS dev.

    Returns:
        Cliente boto3 DynamoDB configurado para a região do ambiente dev.
    """
    return boto3.client("dynamodb", region_name=aws_dev_config.region)


@pytest.fixture(scope="session", autouse=False)
def aws_dev_cleanup(aws_dev_config: AWSDevTestConfig, aws_dev_s3_client):
    """
    Cleanup de artefatos de teste ao final da sessão AWS dev.

    Remove todos os objetos sob o prefixo test/{run_id}/ no bucket de teste.
    O cleanup é best-effort: falhas são logadas mas não falham os testes.

    Esta fixture não é ativada automaticamente (autouse=False).
    Inclua-a explicitamente em testes que precisam de cleanup garantido,
    ou ative-a via conftest de nível superior quando necessário.

    Args:
        aws_dev_config:    Configuração da sessão AWS dev.
        aws_dev_s3_client: Cliente S3 para AWS dev.
    """
    # Executa os testes primeiro
    yield

    # Cleanup ao final da sessão
    logger.info(
        "Iniciando cleanup de artefatos de teste AWS dev",
        extra={
            "bucket": aws_dev_config.bucket,
            "prefix": aws_dev_config.test_prefix,
            "run_id": aws_dev_config.run_id,
        },
    )

    try:
        _delete_objects_under_prefix(
            s3_client=aws_dev_s3_client,
            bucket=aws_dev_config.bucket,
            prefix=aws_dev_config.test_prefix,
        )
        logger.info(
            "Cleanup de artefatos de teste AWS dev concluído",
            extra={"run_id": aws_dev_config.run_id},
        )
    except Exception as exc:
        # Cleanup best-effort: falha não deve quebrar o relatório de testes
        logger.warning(
            "Falha no cleanup de artefatos de teste AWS dev",
            extra={"run_id": aws_dev_config.run_id, "error": str(exc)},
        )


# ---------------------------------------------------------------------------
# Helpers privados de cleanup
# ---------------------------------------------------------------------------


def _delete_objects_under_prefix(s3_client, bucket: str, prefix: str) -> None:
    """
    Remove todos os objetos sob um prefixo S3.

    Usa paginação para lidar com buckets com muitos objetos.
    Deleta em lotes de até 1000 objetos (limite da API S3).

    Args:
        s3_client: Cliente boto3 S3.
        bucket:    Nome do bucket.
        prefix:    Prefixo dos objetos a remover.
    """
    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = page.get("Contents", [])
        if not objects:
            continue

        objects_to_delete = [{"Key": obj["Key"]} for obj in objects]
        s3_client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": objects_to_delete},
        )
        logger.debug(
            "Deletados %d objetos de teste",
            len(objects_to_delete),
            extra={"bucket": bucket, "prefix": prefix},
        )
