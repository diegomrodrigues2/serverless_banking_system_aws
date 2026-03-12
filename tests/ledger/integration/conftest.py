"""
Configuração de testes de integração do Double-Entry Ledger.

Fornece fixtures pytest para:
- Cliente DynamoDB apontando para DynamoDB Local (localhost:8000)
- Criação da tabela single-table com GSI antes de cada teste
- Limpeza da tabela entre testes (delete + recreate para garantir estado limpo)
- Instâncias completas do repositório e engine para testes end-to-end

Pré-requisito: DynamoDB Local rodando em localhost:8000.
  docker-compose up -d

Requisitos atendidos: 13.1, 13.2
"""
from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError

from ledger.domain.factories import JournalEntryFactory
from ledger.domain.validators import ValidationChain, ZeroSumValidator, MinorUnitsValidator, TransactionLimitValidator
from ledger.domain.services import LedgerEngine
from ledger.infrastructure.dynamodb_repository import DynamoDBLedgerRepository

# ---------------------------------------------------------------------------
# Constantes de configuração do DynamoDB Local
# ---------------------------------------------------------------------------

DYNAMODB_LOCAL_ENDPOINT = "http://localhost:8000"
TABLE_NAME = "ledger-integration-test"

# Credenciais fictícias — DynamoDB Local não valida credenciais reais.
# Valores fixos para evitar variação entre execuções.
AWS_REGION = "us-east-1"
AWS_ACCESS_KEY_ID = "test"
AWS_SECRET_ACCESS_KEY = "test"


# ---------------------------------------------------------------------------
# Fixtures de infraestrutura DynamoDB
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def dynamodb_client():
    """
    Cliente DynamoDB apontando para DynamoDB Local.

    Escopo de sessão: o cliente é criado uma vez por sessão de testes.
    Credenciais fictícias são aceitas pelo DynamoDB Local sem validação.

    Yields:
        Cliente boto3 DynamoDB configurado para localhost:8000.
    """
    client = boto3.client(
        "dynamodb",
        endpoint_url=DYNAMODB_LOCAL_ENDPOINT,
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    )
    yield client


@pytest.fixture(scope="function")
def dynamodb_table(dynamodb_client):
    """
    Cria a tabela single-table com GSI antes de cada teste e a remove após.

    Escopo de função: garante estado limpo para cada teste de integração.
    A tabela é criada com o schema completo do single-table design:
    - Chave primária: PK (string) + SK (string)
    - GSI-EntryPostings: entry_id_gsi (PK) + SK (SK)
    - DynamoDB Streams: NEW_IMAGE (para testes do Outbox Pattern)
    - TTL: campo expires_at (para OutboxEvents)

    Yields:
        Nome da tabela criada.
    """
    # Remove tabela anterior se existir (cleanup de execução anterior interrompida)
    _drop_table_if_exists(dynamodb_client, TABLE_NAME)

    # Cria tabela com schema completo do single-table design
    _create_ledger_table(dynamodb_client, TABLE_NAME)

    yield TABLE_NAME

    # Cleanup após o teste
    _drop_table_if_exists(dynamodb_client, TABLE_NAME)


# ---------------------------------------------------------------------------
# Fixtures de repositório e engine
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def repository(dynamodb_client, dynamodb_table):
    """
    Instância do DynamoDBLedgerRepository apontando para DynamoDB Local.

    Depende de dynamodb_table para garantir que a tabela existe antes
    de instanciar o repositório.

    Args:
        dynamodb_client: Cliente DynamoDB para localhost:8000.
        dynamodb_table:  Nome da tabela criada para o teste.

    Returns:
        DynamoDBLedgerRepository configurado para testes de integração.
    """
    return DynamoDBLedgerRepository(
        dynamodb_client=dynamodb_client,
        table_name=dynamodb_table,
    )


@pytest.fixture(scope="function")
def ledger_engine(repository):
    """
    Instância completa do LedgerEngine com todos os colaboradores reais.

    Usa o repositório DynamoDB Local para testes end-to-end do Write Path.
    A ValidationChain e JournalEntryFactory são as implementações reais
    do domínio — sem mocks.

    Args:
        repository: DynamoDBLedgerRepository para DynamoDB Local.

    Returns:
        LedgerEngine configurado para testes de integração.
    """
    validation_chain = ValidationChain(validators=[
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
    ])
    factory = JournalEntryFactory()
    return LedgerEngine(
        repository=repository,
        validation_chain=validation_chain,
        factory=factory,
    )


# ---------------------------------------------------------------------------
# Helpers privados de setup/teardown da tabela
# ---------------------------------------------------------------------------


def _drop_table_if_exists(client, table_name: str) -> None:
    """
    Remove a tabela DynamoDB se ela existir.

    Ignora o erro ResourceNotFoundException caso a tabela não exista.
    Aguarda a remoção completa antes de retornar.

    Args:
        client:     Cliente DynamoDB.
        table_name: Nome da tabela a remover.
    """
    try:
        client.delete_table(TableName=table_name)
        # Aguarda a remoção completa antes de prosseguir
        waiter = client.get_waiter("table_not_exists")
        waiter.wait(TableName=table_name)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code not in ("ResourceNotFoundException", "ResourceInUseException"):
            raise


def _create_ledger_table(client, table_name: str) -> None:
    """
    Cria a tabela DynamoDB com o schema completo do single-table design.

    Schema:
        Chave primária: PK (HASH) + SK (RANGE)
        GSI-EntryPostings: entry_id_gsi (HASH) + SK (RANGE)
            - Permite buscar todos os postings de um JournalEntry por entry_id
        Streams: NEW_IMAGE — captura novos itens para o Outbox Pattern
        TTL: campo expires_at — limpeza automática de OutboxEvents

    Args:
        client:     Cliente DynamoDB.
        table_name: Nome da tabela a criar.
    """
    client.create_table(
        TableName=table_name,
        # Definição das chaves primárias e dos atributos usados em índices
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            # Atributo do GSI para busca de postings por entry_id
            {"AttributeName": "entry_id_gsi", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI-EntryPostings",
                "KeySchema": [
                    {"AttributeName": "entry_id_gsi", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
                "ProvisionedThroughput": {
                    "ReadCapacityUnits": 5,
                    "WriteCapacityUnits": 5,
                },
            }
        ],
        # Modo provisionado com capacidade mínima para testes locais
        ProvisionedThroughput={
            "ReadCapacityUnits": 5,
            "WriteCapacityUnits": 5,
        },
        # DynamoDB Streams: captura NEW_IMAGE para testes do Outbox Pattern
        StreamSpecification={
            "StreamEnabled": True,
            "StreamViewType": "NEW_IMAGE",
        },
    )

    # Aguarda a tabela ficar ativa antes de prosseguir com os testes
    waiter = client.get_waiter("table_exists")
    waiter.wait(TableName=table_name)
