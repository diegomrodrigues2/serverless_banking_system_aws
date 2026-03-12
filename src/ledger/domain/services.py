"""
Serviços de domínio do Double-Entry Ledger.

O serviço principal é LedgerEngine, que atua como fachada (GoF Facade)
orquestrando validação, criação de lançamentos e persistência atômica.

O LedgerEngine não contém lógica de persistência nem de validação —
delega essas responsabilidades para os colaboradores injetados:
- ValidationChain: valida o comando antes de criar o aggregate
- JournalEntryFactory: cria o aggregate JournalEntry
- LedgerRepository: persiste atomicamente via TransactWriteItems

Fluxo de criação de lançamento (create_journal_entry):
1. Verifica idempotência via external_id → levanta IdempotencyConflict se duplicado
2. Valida via ValidationChain → levanta DomainError se inválido
3. Cria aggregate via JournalEntryFactory
4. Persiste atomicamente via LedgerRepository
5. Emite log estruturado JSON com entry_id, operation e result

Fluxo de reversão (create_reversal):
1. Busca lançamento original por original_entry_id → levanta JournalEntryNotFound se ausente
2. Cria reversão via JournalEntryFactory (postings invertidos)
3. Persiste atomicamente via LedgerRepository
4. Emite log estruturado JSON com entry_id, operation e result

Requisitos atendidos: 1.1, 1.2, 4.1, 4.2, 9.2, 15.1, 15.3
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ledger.domain.aggregates import JournalEntry
from ledger.domain.errors import DomainError, JournalEntryNotFound, IdempotencyConflict
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.ports import LedgerRepository
from ledger.domain.validators import ValidationChain

if TYPE_CHECKING:
    from ledger.application.commands import (
        CreateJournalEntryCommand,
        CreateReversalCommand,
    )

# Logger do módulo — emite logs estruturados JSON para observabilidade.
# O nome do logger segue a hierarquia do pacote para facilitar filtragem.
logger = logging.getLogger(__name__)


class LedgerEngine:
    """
    GoF Facade — orquestra o fluxo completo de criação de lançamentos contábeis.

    Atua como ponto de entrada único para operações de escrita no subledger.
    Coordena os colaboradores sem conter lógica de negócio própria:

    - ValidationChain: responsável por validar regras de negócio (zero-sum,
      minor units, limites do DynamoDB)
    - JournalEntryFactory: responsável por criar o aggregate JournalEntry
      com entry_id, timestamp e OutboxEvent
    - LedgerRepository: responsável por persistir atomicamente via
      TransactWriteItems (JournalEntry + Postings + Balances + OutboxEvent)

    Todos os DomainErrors levantados pelos colaboradores são propagados
    para a camada de aplicação/API sem swallow — o LedgerEngine apenas
    registra o erro antes de re-levantar.

    Injeção de dependência via construtor permite substituição por
    implementações in-memory em testes unitários.
    """

    def __init__(
        self,
        repository: LedgerRepository,
        validation_chain: ValidationChain,
        factory: JournalEntryFactory,
    ) -> None:
        """
        Inicializa o LedgerEngine com seus colaboradores.

        Args:
            repository:       Implementação do LedgerRepository (DynamoDB ou in-memory).
            validation_chain: Cadeia de validadores configurada para o domínio.
            factory:          Factory para criação de JournalEntries.
        """
        # Armazena colaboradores para uso nos métodos de escrita
        self._repository = repository
        self._validation_chain = validation_chain
        self._factory = factory

    def create_journal_entry(
        self, command: CreateJournalEntryCommand
    ) -> JournalEntry:
        """
        Fluxo principal de escrita — cria um novo lançamento contábil padrão.

        Algoritmo (fail-fast em cada etapa):
        1. Verifica idempotência: busca external_id no repositório.
           Se encontrado, levanta IdempotencyConflict com o entry_id original.
        2. Valida o comando via ValidationChain (zero-sum, minor units, limites).
           Se inválido, levanta o DomainError correspondente.
        3. Cria o aggregate JournalEntry via JournalEntryFactory.
        4. Persiste atomicamente via LedgerRepository (TransactWriteItems).
        5. Emite log estruturado JSON de sucesso.
        6. Retorna o JournalEntry criado.

        Em caso de erro em qualquer etapa, registra log estruturado JSON
        com o tipo de falha e re-levanta o DomainError original.

        Args:
            command: Comando com external_id, postings e metadata.

        Returns:
            JournalEntry criado e persistido com sucesso.

        Raises:
            IdempotencyConflict:      Se external_id já existe no sistema.
            ZeroSumViolation:         Se postings não somam zero por moeda.
            InvalidAmountType:        Se qualquer amount não é int > 0.
            TransactionLimitExceeded: Se transação excede 100 itens.
            TransactionSizeExceeded:  Se transação excede 4MB.
            OptimisticLockConflict:   Se versão do Balance diverge (escrita concorrente).
        """
        # Etapa 1: Verificação de idempotência.
        # Busca o external_id antes de qualquer validação para retornar
        # rapidamente em caso de requisição duplicada (Requisito 4.1).
        existing_entry = self._repository.find_journal_entry_by_external_id(
            command.external_id
        )
        if existing_entry is not None:
            # Requisição duplicada detectada — levanta IdempotencyConflict
            # com o entry_id original para que a camada de API retorne HTTP 200.
            conflict = IdempotencyConflict(
                external_id=command.external_id,
                existing_entry_id=existing_entry.entry_id,
            )
            logger.info(
                json.dumps({
                    "entry_id": existing_entry.entry_id,
                    "operation": "create_journal_entry",
                    "result": "idempotent_return",
                    "external_id": command.external_id,
                })
            )
            raise conflict

        # Etapa 2: Validação via ValidationChain.
        # A cadeia levanta DomainError no primeiro validador que falhar.
        # O resultado carrega artefatos (ex: DecisionSummary) para persistência.
        try:
            validation_result = self._validation_chain.validate(command)
        except DomainError as validation_error:
            logger.error(
                json.dumps({
                    "entry_id": None,
                    "operation": "create_journal_entry",
                    "result": "error",
                    "error_code": validation_error.code,
                    "error_message": validation_error.message,
                })
            )
            raise

        # Etapa 3: Criação do aggregate via JournalEntryFactory.
        # A factory gera entry_id, timestamp e OutboxEvent.
        # Os artefatos de validação (DecisionSummary) são passados para que
        # a factory os persista atomicamente no metadata do JournalEntry (Requisito 12.4).
        journal_entry = self._factory.create_standard(
            command,
            validation_artifacts=validation_result.artifacts,
        )

        # Etapa 4: Persistência atômica via LedgerRepository.
        # O repositório usa TransactWriteItems para garantir atomicidade
        # (JournalEntry + Postings + Balances + OutboxEvent + Idempotency).
        try:
            self._repository.save_journal_entry(journal_entry)
        except DomainError as persistence_error:
            logger.error(
                json.dumps({
                    "entry_id": journal_entry.entry_id,
                    "operation": "create_journal_entry",
                    "result": "error",
                    "error_code": persistence_error.code,
                    "error_message": persistence_error.message,
                })
            )
            raise

        # Etapa 5: Log estruturado de sucesso (Requisito 15.1).
        logger.info(
            json.dumps({
                "entry_id": journal_entry.entry_id,
                "operation": "create_journal_entry",
                "result": "success",
            })
        )

        # Etapa 6: Retorna o aggregate criado.
        return journal_entry

    def create_reversal(
        self, command: CreateReversalCommand
    ) -> JournalEntry:
        """
        Fluxo de reversão — cria um lançamento que anula um entry existente.

        A reversão é a única forma de correção permitida no subledger.
        Cria um novo JournalEntry do tipo REVERSAL com postings inversos
        ao original, garantindo que a soma combinada (original + reversal)
        seja zero por moeda (propriedade de anulação — Requisito 9.4).

        Algoritmo:
        1. Busca o lançamento original por original_entry_id.
           Se não encontrado, levanta JournalEntryNotFound.
        2. Cria o JournalEntry de reversão via JournalEntryFactory
           (postings com direções invertidas, entry_type=REVERSAL,
           metadata com referência ao original).
        3. Persiste atomicamente via LedgerRepository.
        4. Emite log estruturado JSON de sucesso.
        5. Retorna o JournalEntry de reversão criado.

        Em caso de erro em qualquer etapa, registra log estruturado JSON
        com o tipo de falha e re-levanta o DomainError original.

        Args:
            command: Comando com original_entry_id, external_id e metadata.

        Returns:
            Novo JournalEntry do tipo REVERSAL com postings invertidos.

        Raises:
            JournalEntryNotFound:   Se original_entry_id não existe no sistema.
            OptimisticLockConflict: Se versão do Balance diverge (escrita concorrente).
            IdempotencyConflict:    Se external_id da reversão já existe.
        """
        # Etapa 1: Busca o lançamento original pelo entry_id.
        # A reversão só pode ser criada se o original existir (Requisito 9.2).
        original_entry = self._repository.find_journal_entry_by_id(
            command.original_entry_id
        )
        if original_entry is None:
            not_found_error = JournalEntryNotFound(
                entry_id=command.original_entry_id
            )
            logger.error(
                json.dumps({
                    "entry_id": command.original_entry_id,
                    "operation": "create_reversal",
                    "result": "error",
                    "error_code": not_found_error.code,
                    "error_message": not_found_error.message,
                })
            )
            raise not_found_error

        # Etapa 2: Cria o JournalEntry de reversão via JournalEntryFactory.
        # A factory inverte as direções dos postings (DEBIT↔CREDIT) e
        # adiciona "original_entry_id" ao metadata (Requisito 9.3).
        reversal_entry = self._factory.create_reversal(
            original=original_entry,
            command=command,
        )

        # Etapa 3: Persistência atômica via LedgerRepository.
        try:
            self._repository.save_journal_entry(reversal_entry)
        except DomainError as persistence_error:
            logger.error(
                json.dumps({
                    "entry_id": reversal_entry.entry_id,
                    "operation": "create_reversal",
                    "result": "error",
                    "error_code": persistence_error.code,
                    "error_message": persistence_error.message,
                })
            )
            raise

        # Etapa 4: Log estruturado de sucesso (Requisito 15.1).
        logger.info(
            json.dumps({
                "entry_id": reversal_entry.entry_id,
                "operation": "create_reversal",
                "result": "success",
            })
        )

        # Etapa 5: Retorna o aggregate de reversão criado.
        return reversal_entry
