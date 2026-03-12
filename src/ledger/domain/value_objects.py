"""
Value Objects do domínio do Double-Entry Ledger.

Value Objects são imutáveis, comparáveis por valor e sem efeitos colaterais.
Cada Value Object encapsula invariantes de domínio e as valida na construção,
garantindo que objetos inválidos nunca existam no sistema.

Módulo contém:
- Money: representação monetária em minor units (centavos)
- Direction: enum de débito/crédito
- Posting: linha individual de débito ou crédito
- AccountType: tipos de conta suportados
- EntryType: tipos de lançamento contábil
- Balance: projeção materializada de saldo com OCC
- OutboxEvent: evento transacional para o Outbox Pattern
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class Money:
    """
    Value Object imutável para representação monetária em minor units.

    Invariantes de domínio:
    - amount deve ser int (não float, não bool — bool é subclasse de int em Python)
    - amount deve ser > 0 (valores absolutos; direção é controlada por Direction)
    - currency deve ter exatamente 3 caracteres (código ISO 4217, ex: "BRL", "USD")

    Exemplos:
        Money(amount=1050, currency="BRL")  # R$ 10,50
        Money(amount=100, currency="USD")   # US$ 1,00
    """

    amount: int    # minor units (centavos). Ex: R$ 10,50 = 1050
    currency: str  # código ISO 4217. Ex: "BRL", "USD", "EUR"

    def __post_init__(self) -> None:
        # Verificar bool ANTES de int, pois bool é subclasse de int em Python.
        # isinstance(True, int) retorna True, então a ordem importa.
        if isinstance(self.amount, bool):
            raise ValueError(
                f"amount deve ser int, não bool. Recebido: {type(self.amount).__name__}"
            )
        if not isinstance(self.amount, int):
            raise ValueError(
                f"amount deve ser int (minor units), recebido: {type(self.amount).__name__}. "
                "Valores float ou Decimal não são aceitos para evitar erros de arredondamento."
            )
        if self.amount <= 0:
            raise ValueError(
                f"amount deve ser > 0 (minor units positivos), recebido: {self.amount}. "
                "A direção contábil (débito/crédito) é controlada pelo campo Direction."
            )
        if len(self.currency) != 3:
            raise ValueError(
                f"currency deve ter exatamente 3 caracteres (ISO 4217), recebido: '{self.currency}' "
                f"({len(self.currency)} caracteres)."
            )


class Direction(str, Enum):
    """
    Enum que representa a direção contábil de um posting.

    Convenção de partidas dobradas:
    - DEBIT (débito): valor positivo na soma algébrica
    - CREDIT (crédito): valor negativo na soma algébrica

    Herda de str para serialização direta em JSON e DynamoDB.
    """

    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


@dataclass(frozen=True)
class Posting:
    """
    Value Object imutável representando uma linha individual de débito ou crédito.

    Cada Posting pertence a um JournalEntry e representa a movimentação
    de um valor monetário em uma conta específica.

    Convenção contábil:
    - DEBIT: signed_amount > 0 (aumenta saldo em contas de ativo)
    - CREDIT: signed_amount < 0 (diminui saldo em contas de ativo)

    O campo index representa a posição ordinal do posting dentro do JournalEntry,
    usado para ordenação e geração do posting_sort_key no DynamoDB.
    """

    account_id: str      # identificador da conta afetada
    money: Money         # valor monetário em minor units
    direction: Direction # direção contábil (DEBIT ou CREDIT)
    index: int           # posição ordinal dentro do JournalEntry (0-based)

    @property
    def signed_amount(self) -> int:
        """
        Retorna o valor com sinal para cálculo de zero-sum (partidas dobradas).

        Débito  → +amount (valor positivo)
        Crédito → -amount (valor negativo)

        A soma de signed_amount de todos os postings de um JournalEntry
        deve ser zero para cada moeda (invariante de partidas dobradas).
        """
        if self.direction == Direction.DEBIT:
            return self.money.amount
        return -self.money.amount


class AccountType(str, Enum):
    """
    Enum dos tipos de conta suportados pelo subledger.

    Tipos de conta de usuário:
    - AVAILABLE: saldo líquido disponível para uso
    - HOLD: saldo bloqueado (reservado para operações pendentes)

    Tipos de conta de plataforma:
    - FEES: conta de taxas da plataforma
    - CLEARING: conta de compensação para liquidação entre partes

    Herda de str para serialização direta em JSON e DynamoDB.
    """

    AVAILABLE = "AVAILABLE"   # saldo disponível do usuário
    HOLD = "HOLD"             # saldo bloqueado do usuário
    FEES = "FEES"             # conta de plataforma — taxas
    CLEARING = "CLEARING"     # conta de plataforma — compensação


class EntryType(str, Enum):
    """
    Enum dos tipos de lançamento contábil.

    - STANDARD: lançamento contábil padrão
    - REVERSAL: reversão de um lançamento anterior (única forma de correção permitida)

    Reversões criam um novo JournalEntry com postings inversos ao original,
    garantindo trilha de auditoria completa e imutabilidade dos registros.

    Herda de str para serialização direta em JSON e DynamoDB.
    """

    STANDARD = "STANDARD"   # lançamento padrão
    REVERSAL = "REVERSAL"   # reversão de lançamento anterior


@dataclass
class Balance:
    """
    Projeção materializada do saldo corrente de uma conta em uma moeda específica.

    Diferente dos outros Value Objects, Balance NÃO é frozen (mutável) porque
    precisa ser atualizado atomicamente pelo Write Path via OCC (Optimistic
    Concurrency Control). O campo version é incrementado a cada atualização
    e usado como ConditionExpression no DynamoDB TransactWriteItems.

    Invariantes de OCC:
    - version é incrementado em exatamente 1 a cada atualização bem-sucedida
    - Atualizações concorrentes com version divergente são rejeitadas com
      OptimisticLockConflict (HTTP 409)

    Nota: balance_amount pode ser negativo em contas de plataforma (ex: CLEARING).
    """

    account_id: str       # identificador da conta
    currency: str         # código ISO 4217 da moeda
    balance_amount: int   # saldo em minor units (pode ser negativo)
    version: int          # versão para OCC — incrementado a cada atualização
    last_update: str      # timestamp da última atualização (ISO 8601)


@dataclass(frozen=True)
class OutboxEvent:
    """
    Evento transacional gravado atomicamente junto com o JournalEntry.

    Implementa o Transactional Outbox Pattern:
    1. Gravado na mesma TransactWriteItems que o JournalEntry
    2. Capturado via DynamoDB Streams (filtro por prefixo "OUTBOX#")
    3. Publicado pelo Lambda Publisher no EventBridge/SNS/SQS

    Invariante:
    - event_id deve começar com "OUTBOX#" para facilitar filtragem no DynamoDB Stream
      e no Event Source Mapping da Lambda Publisher

    O campo expires_at (TTL Unix timestamp) garante limpeza automática do DynamoDB
    após o evento ser processado, evitando acúmulo de registros.
    """

    event_id: str    # "OUTBOX#{entry_id}" — prefixo obrigatório para filtragem no Stream
    entry_id: str    # UUID do JournalEntry associado
    event_type: str  # "TransactionCreated" | "TransactionReversed"
    payload: dict    # serialização do JournalEntry para publicação no barramento
    expires_at: int  # Unix timestamp para TTL do DynamoDB (limpeza automática)

    def __post_init__(self) -> None:
        # Validar prefixo obrigatório para garantir filtragem correta no DynamoDB Stream.
        # O Event Source Mapping da Lambda Publisher filtra por prefixo "OUTBOX#".
        if not self.event_id.startswith("OUTBOX#"):
            raise ValueError(
                f"event_id deve começar com 'OUTBOX#' para filtragem no DynamoDB Stream. "
                f"Recebido: '{self.event_id}'"
            )
