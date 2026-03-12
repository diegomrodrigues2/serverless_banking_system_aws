"""
Fixtures de integração local para o Validation Engine.

Fornece:
- Bucket S3 mockado via moto para bundles e snapshots
- AppConfig mockado via moto para manifestos de ativação
- DynamoDB Local para integração com o ledger
- Diretório temporário para LKGStore
- Helpers de bootstrap do ambiente local

Pré-requisito para testes DynamoDB: DynamoDB Local rodando em localhost:8000.
  docker-compose up -d

Os testes S3 e AppConfig usam moto e não requerem serviços externos.

Uso:
    pytest tests/validation_engine/integration/local/ -v
    pytest -m integration_local -v
"""
from __future__ import annotations

import tempfile
from collections.abc import Generator
from dataclasses import dataclass

import boto3
import pytest

# ---------------------------------------------------------------------------
# Constantes de configuração local
# ---------------------------------------------------------------------------

LOCAL_S3_BUCKET = "validation-engine-local-test"
LOCAL_DYNAMODB_ENDPOINT = "http://localhost:8000"
LOCAL_DYNAMODB_TABLE = "ledger-validation-engine-integration-test"
AWS_REGION = "us-east-1"

# Credenciais fictícias — moto e DynamoDB Local não validam credenciais reais.
FAKE_AWS_ACCESS_KEY_ID = "test"
FAKE_AWS_SECRET_ACCESS_KEY = "test"


# ---------------------------------------------------------------------------
# Fixtures S3 (moto)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def moto_s3_client():
    """
    Cliente S3 mockado via moto para testes locais.

    Usa moto para simular o S3 sem dependências de AWS reais.
    O mock é ativado e desativado automaticamente pelo escopo da fixture.

    Yields:
        Cliente boto3 S3 apontando para o mock moto.
    """
    # Importação lazy para evitar erro se moto não estiver instalado
    # em ambientes que não executam testes de integração local
    try:
        from moto import mock_aws
    except ImportError as exc:
        pytest.skip(f"moto não instalado: {exc}")

    with mock_aws():
        client = boto3.client(
            "s3",
            region_name=AWS_REGION,
            aws_access_key_id=FAKE_AWS_ACCESS_KEY_ID,
            aws_secret_access_key=FAKE_AWS_SECRET_ACCESS_KEY,
        )
        yield client


@pytest.fixture(scope="function")
def local_s3_bucket(moto_s3_client):
    """
    Bucket S3 mockado criado e pronto para uso em testes locais.

    Cria o bucket com configuração mínima para simular o ambiente de produção:
    - versionamento habilitado
    - prefixos bundles/ e snapshots/ disponíveis

    Args:
        moto_s3_client: Cliente S3 mockado via moto.

    Yields:
        Nome do bucket criado.
    """
    # Cria o bucket no mock moto
    moto_s3_client.create_bucket(Bucket=LOCAL_S3_BUCKET)

    # Habilita versionamento para simular o ambiente de produção
    moto_s3_client.put_bucket_versioning(
        Bucket=LOCAL_S3_BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )

    yield LOCAL_S3_BUCKET

    # Cleanup: remove todos os objetos e versões antes de deletar o bucket
    _empty_versioned_bucket(moto_s3_client, LOCAL_S3_BUCKET)
    moto_s3_client.delete_bucket(Bucket=LOCAL_S3_BUCKET)


# ---------------------------------------------------------------------------
# Fixtures DynamoDB Local
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def local_dynamodb_client():
    """
    Cliente DynamoDB apontando para DynamoDB Local (localhost:8000).

    Escopo de sessão: o cliente é criado uma vez por sessão de testes.
    Credenciais fictícias são aceitas pelo DynamoDB Local sem validação.

    Yields:
        Cliente boto3 DynamoDB configurado para localhost:8000.
    """
    client = boto3.client(
        "dynamodb",
        endpoint_url=LOCAL_DYNAMODB_ENDPOINT,
        region_name=AWS_REGION,
        aws_access_key_id=FAKE_AWS_ACCESS_KEY_ID,
        aws_secret_access_key=FAKE_AWS_SECRET_ACCESS_KEY,
    )
    yield client


# ---------------------------------------------------------------------------
# Fixtures de diretório temporário (LKGStore)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def lkg_temp_dir() -> Generator[str, None, None]:
    """
    Diretório temporário para o LKGStore em testes locais.

    Usa tempfile.mkdtemp() para criar um diretório isolado por teste.
    O diretório é removido automaticamente após o teste.

    Yields:
        Caminho absoluto do diretório temporário.
    """
    with tempfile.TemporaryDirectory(prefix="validation_engine_lkg_") as temp_dir:
        yield temp_dir


# ---------------------------------------------------------------------------
# Fixtures AppConfig (moto)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def moto_appconfig_client(moto_s3_client):
    """
    Cliente AppConfig mockado via moto para testes locais.

    Reutiliza o contexto mock_aws já ativo via moto_s3_client para garantir
    que ambos os serviços compartilhem o mesmo mock de sessão.

    Yields:
        Cliente boto3 AppConfig apontando para o mock moto.
    """
    # O mock_aws já está ativo via moto_s3_client — apenas cria o cliente
    client = boto3.client(
        "appconfig",
        region_name=AWS_REGION,
        aws_access_key_id=FAKE_AWS_ACCESS_KEY_ID,
        aws_secret_access_key=FAKE_AWS_SECRET_ACCESS_KEY,
    )
    yield client


# ---------------------------------------------------------------------------
# Dataclass de configuração local
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalTestEnvironment:
    """
    Configuração consolidada do ambiente de integração local.

    Agrupa todos os recursos necessários para um teste de integração local:
    S3 mockado, DynamoDB Local e diretório temporário para LKGStore.

    Attributes:
        s3_client:    Cliente S3 mockado via moto.
        bucket_name:  Nome do bucket S3 criado para o teste.
        lkg_dir:      Caminho do diretório temporário para LKGStore.
        region:       Região AWS simulada.
    """

    s3_client: object
    bucket_name: str
    lkg_dir: str
    region: str = AWS_REGION


@pytest.fixture(scope="function")
def local_test_env(local_s3_bucket, moto_s3_client, lkg_temp_dir) -> LocalTestEnvironment:
    """
    Ambiente de integração local completo para um teste.

    Combina S3 mockado, bucket criado e diretório temporário para LKGStore
    em um único objeto de configuração para simplificar o setup dos testes.

    Args:
        local_s3_bucket: Nome do bucket S3 criado via moto.
        moto_s3_client:  Cliente S3 mockado.
        lkg_temp_dir:    Diretório temporário para LKGStore.

    Returns:
        LocalTestEnvironment com todos os recursos configurados.
    """
    return LocalTestEnvironment(
        s3_client=moto_s3_client,
        bucket_name=local_s3_bucket,
        lkg_dir=lkg_temp_dir,
    )


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------


def _empty_versioned_bucket(s3_client, bucket_name: str) -> None:
    """
    Remove todos os objetos e versões de um bucket versionado.

    Necessário para deletar buckets com versionamento habilitado no moto.

    Args:
        s3_client:   Cliente S3 mockado.
        bucket_name: Nome do bucket a esvaziar.
    """
    # Lista e deleta todas as versões de objetos
    paginator = s3_client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket_name):
        versions = page.get("Versions", [])
        delete_markers = page.get("DeleteMarkers", [])

        objects_to_delete = [
            {"Key": obj["Key"], "VersionId": obj["VersionId"]}
            for obj in versions + delete_markers
        ]

        if objects_to_delete:
            s3_client.delete_objects(
                Bucket=bucket_name,
                Delete={"Objects": objects_to_delete},
            )
