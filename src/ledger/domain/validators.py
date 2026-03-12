"""
Cadeia de validação do domínio do Double-Entry Ledger.

Implementa o padrão GoF Chain of Responsibility para validação de comandos
de criação de journal entries. Cada validador é independente e encadeado
sequencialmente — o primeiro erro interrompe o fluxo e levanta um DomainError.

Validadores disponíveis:
- ZeroSumValidator: soma algébrica dos postings == 0 por moeda (Requisito 1.1, 1.2)
- MinorUnitsValidator: todos os amounts são int > 0 (Requisito 2.1, 2.3)
- TransactionLimitValidator: respeita limites do DynamoDB TransactWriteItems (Requisito 14.1, 14.2)

Uso típico:
    chain = ValidationChain([
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
    ])
    result = chain.validate(command)  # levanta DomainError se inválido
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ledger.domain.errors import (
    InvalidAmountType,
    TransactionLimitExceeded,
    TransactionSizeExceeded,
    ZeroSumViolation,
)

if TYPE_CHECKING:
    from ledger.application.commands import CreateJournalEntryCommand

# ---------------------------------------------------------------------------
# Limite do DynamoDB TransactWriteItems
# ---------------------------------------------------------------------------

# Número máximo de itens em uma única TransactWriteItems
_DYNAMO_MAX_ITEMS = 100

# Tamanho máximo do payload de uma TransactWriteItems (4 MB)
_DYNAMO_MAX_SIZE_BYTES = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# Protocolos de duck-typing para evitar importações circulares
# ---------------------------------------------------------------------------


@runtime_checkable
class PostingData(Protocol):
    """
    Protocolo que descreve os campos esperados de um posting no comando.

    Usado para type hints nos validadores sem depender da implementação
    concreta de CreateJournalEntryCommand (evita importação circular).
    """

    account_id: str
    amount: object   # raw — pode ser qualquer tipo; os validadores verificam
    currency: str
    direction: str   # "DEBIT" | "CREDIT"


@runtime_checkable
class CommandData(Protocol):
    """
    Protocolo que descreve os campos esperados do comando de criação.

    Permite que os validadores operem sobre qualquer objeto que satisfaça
    esta interface, sem depender da classe concreta do comando.
    """

    external_id: str
    postings: list[PostingData]
    tenant_id: str
    policy_context: dict
    metadata: dict


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationArtifacts:
    """
    Artefatos produzidos pela validação de policy para persistência pelo ledger.

    Carrega o DecisionSummary que deve ser persistido atomicamente com o
    JournalEntry pelo LedgerEngine. A facade NÃO escreve no banco de dados —
    ela apenas produz este artefato e o retorna via ValidationResult.

    O campo decision_summary é tipado como Any para evitar dependência
    circular entre o bounded context do ledger e o do validation engine.
    Em runtime, o valor é um DecisionSummary do validation engine.

    Requisito: 7.2, 12.3
    """

    decision_summary: object | None = None

    def merge(self, other: "ValidationArtifacts") -> "ValidationArtifacts":
        """
        Combina dois conjuntos de artefatos, priorizando o mais recente.

        O DecisionSummary do `other` prevalece se presente; caso contrário,
        mantém o do `self`. Isso permite que a ValidationChain acumule
        artefatos de múltiplos validadores sem perder dados.

        Args:
            other: Artefatos a serem mesclados (prioridade sobre self).

        Returns:
            Novo ValidationArtifacts com o merge dos dois conjuntos.
        """
        return ValidationArtifacts(
            decision_summary=other.decision_summary or self.decision_summary,
        )


@dataclass(frozen=True)
class ValidationResult:
    """
    Resultado de uma validação.

    Carrega o status (is_valid), a lista de erros encontrados e artefatos
    opcionais produzidos pela validação (ex: DecisionSummary de policy).

    O campo artifacts permite que validadores como o PolicyValidationFacade
    retornem dados adicionais para persistência pelo LedgerEngine, sem
    mutar o comando original.

    Requisito: 7.2, 12.3
    """

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    artifacts: ValidationArtifacts = field(default_factory=ValidationArtifacts)

    @classmethod
    def success(
        cls, artifacts: ValidationArtifacts | None = None,
    ) -> "ValidationResult":
        """
        Cria um resultado de validação bem-sucedida (sem erros).

        Args:
            artifacts: Artefatos opcionais produzidos pela validação.
        """
        return cls(
            is_valid=True,
            errors=[],
            artifacts=artifacts or ValidationArtifacts(),
        )

    @classmethod
    def failure(cls, errors: list[str]) -> "ValidationResult":
        """
        Cria um resultado de validação com falha.

        Args:
            errors: lista de mensagens de erro descritivas.
        """
        return cls(is_valid=False, errors=errors)


# ---------------------------------------------------------------------------
# ValidationStrategy Protocol
# ---------------------------------------------------------------------------


class ValidationStrategy(Protocol):
    """
    GoF Strategy — interface que cada validador deve implementar.

    Cada validador recebe o comando completo e retorna um ValidationResult.
    Se a validação falhar, o validador DEVE levantar o DomainError apropriado
    antes de retornar (a cadeia interrompe no primeiro erro).
    """

    def validate(self, command: "CreateJournalEntryCommand") -> ValidationResult:
        """
        Valida o comando e retorna ValidationResult.

        Levanta DomainError se a validação falhar.
        Retorna ValidationResult.success() se a validação passar.
        """
        ...


# ---------------------------------------------------------------------------
# ZeroSumValidator
# ---------------------------------------------------------------------------


class ZeroSumValidator:
    """
    Valida que a soma algébrica dos postings é zero para cada moeda.

    Implementa a invariante central do subledger (partidas dobradas):
    para cada moeda presente nos postings, a soma de signed_amounts
    (DEBIT=+amount, CREDIT=-amount) deve ser exatamente zero.

    Levanta ZeroSumViolation se a invariante for violada.

    Requisitos: 1.1, 1.2
    """

    def validate(self, command: "CreateJournalEntryCommand") -> ValidationResult:
        """
        Agrupa postings por moeda e verifica que cada grupo soma zero.

        A convenção de sinais segue o padrão contábil:
        - DEBIT: valor positivo (+amount)
        - CREDIT: valor negativo (-amount)

        Args:
            command: comando com a lista de postings a validar.

        Returns:
            ValidationResult.success() se todos os grupos somam zero.

        Raises:
            ZeroSumViolation: se qualquer moeda não soma zero.
        """
        # Acumula a soma algébrica por moeda
        sums_by_currency: dict[str, int] = {}

        for posting in command.postings:
            currency = posting.currency
            # Aplica a convenção de sinais: DEBIT=+, CREDIT=-
            # Nota: neste ponto o amount pode ser de qualquer tipo;
            # o MinorUnitsValidator garante que é int > 0.
            # Aqui assumimos que o amount é numérico para o cálculo.
            if posting.direction == "DEBIT":
                signed_amount = posting.amount
            else:
                signed_amount = -posting.amount  # type: ignore[operator]

            sums_by_currency[currency] = sums_by_currency.get(currency, 0) + signed_amount  # type: ignore[operator]

        # Verifica que todas as moedas somam zero
        for currency, total in sums_by_currency.items():
            if total != 0:
                raise ZeroSumViolation(currency=currency, total=total)

        return ValidationResult.success()


# ---------------------------------------------------------------------------
# MinorUnitsValidator
# ---------------------------------------------------------------------------


class MinorUnitsValidator:
    """
    Valida que todos os amounts são inteiros maiores que zero (minor units).

    O subledger representa valores monetários exclusivamente em minor units
    (inteiros) para eliminar erros de arredondamento de ponto flutuante.

    Regras de validação:
    - bool é rejeitado (bool é subclasse de int em Python, mas não é válido)
    - float é rejeitado
    - int <= 0 é rejeitado (zero e negativos não são válidos)
    - qualquer outro tipo não-int é rejeitado

    Levanta InvalidAmountType se qualquer amount violar as regras.

    Requisitos: 2.1, 2.3
    """

    def validate(self, command: "CreateJournalEntryCommand") -> ValidationResult:
        """
        Verifica que todos os amounts nos postings são int > 0.

        A ordem de verificação é importante:
        1. Verifica bool ANTES de int (bool é subclasse de int em Python)
        2. Verifica se é int
        3. Verifica se é > 0

        Args:
            command: comando com a lista de postings a validar.

        Returns:
            ValidationResult.success() se todos os amounts são int > 0.

        Raises:
            InvalidAmountType: se qualquer amount não for int > 0.
        """
        for posting in command.postings:
            amount = posting.amount

            # Verifica bool PRIMEIRO — bool é subclasse de int em Python,
            # então isinstance(True, int) retorna True. Devemos rejeitar bool
            # explicitamente antes de verificar int.
            if isinstance(amount, bool):
                raise InvalidAmountType(received_type=type(amount).__name__)

            # Verifica se é int (após descartar bool)
            if not isinstance(amount, int):
                raise InvalidAmountType(received_type=type(amount).__name__)

            # Verifica se é maior que zero (zero e negativos são inválidos)
            if amount <= 0:
                raise InvalidAmountType(
                    received_type=f"int com valor inválido: {amount}"
                )

        return ValidationResult.success()


# ---------------------------------------------------------------------------
# TransactionLimitValidator
# ---------------------------------------------------------------------------


class TransactionLimitValidator:
    """
    Valida que a transação não excede os limites do DynamoDB TransactWriteItems.

    O DynamoDB impõe dois limites para TransactWriteItems:
    1. Máximo de 100 itens por transação
    2. Payload máximo de 4MB por transação

    Fórmula para contagem de itens:
        3 (JournalEntry + Idempotency + OutboxEvent)
        + N (número de postings)
        + M (número de contas distintas — uma atualização de Balance por conta)

    Para estimativa de tamanho, serializa os postings como JSON e usa
    o tamanho resultante como proxy conservador do payload total.

    Levanta TransactionLimitExceeded ou TransactionSizeExceeded conforme
    o limite violado.

    Requisitos: 14.1, 14.2
    """

    def validate(self, command: "CreateJournalEntryCommand") -> ValidationResult:
        """
        Verifica contagem de itens e tamanho estimado do payload.

        Args:
            command: comando com a lista de postings a validar.

        Returns:
            ValidationResult.success() se dentro dos limites.

        Raises:
            TransactionLimitExceeded: se item_count > 100.
            TransactionSizeExceeded: se estimated_size > 4MB.
        """
        postings = command.postings
        n_postings = len(postings)

        # Calcula o número de contas distintas (M) para estimar Balance updates
        distinct_accounts = {posting.account_id for posting in postings}
        m_distinct_accounts = len(distinct_accounts)

        # Fórmula: 3 itens fixos + N postings + M balance updates
        # 3 = JournalEntry + Idempotency record + OutboxEvent
        item_count = 3 + n_postings + m_distinct_accounts

        if item_count > _DYNAMO_MAX_ITEMS:
            raise TransactionLimitExceeded(item_count=item_count)

        # Estimativa conservadora do tamanho do payload via serialização JSON
        # dos postings. Usa vars() para serializar os atributos do posting.
        try:
            postings_payload = [
                {
                    "account_id": p.account_id,
                    "amount": p.amount,
                    "currency": p.currency,
                    "direction": p.direction,
                }
                for p in postings
            ]
            estimated_size = len(json.dumps(postings_payload).encode("utf-8"))
        except (TypeError, ValueError):
            # Se a serialização falhar (ex: amount não serializável),
            # usa uma estimativa conservadora baseada no número de postings
            estimated_size = n_postings * 1024  # 1KB por posting como fallback

        if estimated_size > _DYNAMO_MAX_SIZE_BYTES:
            raise TransactionSizeExceeded(size_bytes=estimated_size)

        return ValidationResult.success()


# ---------------------------------------------------------------------------
# ValidationChain
# ---------------------------------------------------------------------------


class ValidationChain:
    """
    GoF Chain of Responsibility — encadeia validadores sequencialmente.

    Percorre a lista de validadores em ordem. O primeiro validador que
    levantar um DomainError interrompe a cadeia (fail-fast). Se todos
    os validadores passarem, retorna ValidationResult.success().

    Uso típico:
        chain = ValidationChain([
            ZeroSumValidator(),
            MinorUnitsValidator(),
            TransactionLimitValidator(),
        ])
        result = chain.validate(command)

    A ordem dos validadores importa: validadores mais baratos (computacionalmente)
    devem vir antes para falhar rapidamente em casos inválidos comuns.
    """

    def __init__(self, validators: list[ValidationStrategy]) -> None:
        """
        Inicializa a cadeia com a lista de validadores.

        Args:
            validators: lista ordenada de validadores a executar.
                        A ordem determina a sequência de execução.
        """
        # Armazena a lista de validadores para execução sequencial
        self._validators = validators

    def validate(self, command: "CreateJournalEntryCommand") -> ValidationResult:
        """
        Executa todos os validadores em sequência (fail-fast).

        Itera sobre os validadores na ordem fornecida. Se qualquer
        validador levantar um DomainError, a exceção se propaga
        imediatamente (a cadeia para no primeiro erro).

        Artefatos produzidos por cada validador são acumulados via merge.
        Isso permite que o PolicyValidationFacade retorne DecisionSummary
        para persistência pelo LedgerEngine sem mutar o comando.

        Args:
            command: comando a ser validado.

        Returns:
            ValidationResult com artefatos acumulados de todos os validadores.

        Raises:
            DomainError: levantado pelo primeiro validador que falhar.
        """
        # Acumula artefatos de todos os validadores que passam.
        accumulated_artifacts = ValidationArtifacts()

        for validator in self._validators:
            # Cada validador levanta DomainError se falhar —
            # a exceção se propaga naturalmente, interrompendo a cadeia.
            result = validator.validate(command)
            # Merge de artefatos: o validador mais recente prevalece.
            accumulated_artifacts = accumulated_artifacts.merge(result.artifacts)

        return ValidationResult.success(artifacts=accumulated_artifacts)
