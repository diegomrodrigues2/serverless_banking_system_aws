"""
Fixtures para testes de integração contra AWS real (dev).

Usa a tabela DynamoDB real em us-east-1 com credenciais da sessão AWS ativa.
Cada teste cria itens com prefixos únicos (UUID) para evitar colisão com dados
de produção e limpa os itens criados no teardown.

Uso:
    pytest tests/ledger/integration/test_aws_e2e.py -v
"""
from __future__ import annotations

import os
import boto3
import pytest

from ledger.domain.factories import JournalEntryFactory
from ledger.domain.validators import (
    ValidationChain,
    ZeroSumValidator,
    MinorUnitsValidator,
    TransactionLimitValidator,
)
from ledger.domain.services import LedgerEngine
from ledger.infrastructure.dynamodb_repository import DynamoDBLedgerRepository

AWS_REGION = "us-east-1"
AWS_TABLE_NAME = os.environ.get("AWS_LEDGER_TABLE", "ledger-dev")


@pytest.fixture(scope="session")
def aws_dynamodb_client():
    """Cliente DynamoDB apontando para AWS real em us-east-1."""
    return boto3.client("dynamodb", region_name=AWS_REGION)


@pytest.fixture(scope="function")
def aws_repository(aws_dynamodb_client):
    """Repositório apontando para a tabela DynamoDB real."""
    return DynamoDBLedgerRepository(
        dynamodb_client=aws_dynamodb_client,
        table_name=AWS_TABLE_NAME,
    )


@pytest.fixture(scope="function")
def aws_ledger_engine(aws_repository):
    """LedgerEngine completo contra DynamoDB real."""
    chain = ValidationChain([
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
    ])
    return LedgerEngine(
        repository=aws_repository,
        validation_chain=chain,
        factory=JournalEntryFactory(),
    )
