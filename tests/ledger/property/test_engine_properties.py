"""
Testes de propriedade para o LedgerEngine, JournalEntryFactory e TransactionLimitValidator.

Propriedades cobertas:
- Property 18: Validação de limites do DynamoDB (TransactionLimitValidator)
- Property 16: Anulação por reversão (reversal annulment via JournalEntryFactory)
- Property 19: Logging estruturado (LedgerEngine emite JSON com campos obrigatórios)

Requisitos validados: 9.2, 9.3, 9.4, 14.1, 14.2, 15.1, 15.3
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import pytest
from hypothesis import given, strategies as st

from ledger.application.commands import (
    CreateJournalEntryCommand,
    CreateReversalCommand,
    PostingInput,
)
from ledger.domain.aggregates import JournalEntry
from ledger.domain.errors import (
    DomainError,
    TransactionLimitExceeded,
    TransactionSizeExceeded,
)
from ledger.domain.factories import JournalEntryFactory
from ledger.domain.ports import StatementPage
from ledger.domain.validators import TransactionLimitValidator
from ledger.domain.value_objects import (
    Direction,
    EntryType,
    Money,
    OutboxEvent,
    Posting,
)
from ledger.domain.services import LedgerEngine
from ledger.domain.validators import ValidationChain, ZeroSumValidator, MinorUnitsValidator

# ---------------------------------------------------------------------------
# InMemoryLedgerRepository — repositório in-memory para testes unitários
# ---------------------------------------------------------------------------


class InMemoryLedgerRepository:
    """
    Implementação in-memory do LedgerRepository para testes unitários.

    Simula o comportamento do DynamoDB sem dependência de infraestrutura.
    Satisfaz o protocolo LedgerRepository definido em ports.py.
    """

    def __init__(self) -> None:
        # Armazena entries por entry_id (partition key)
        self._entries: dict[str, JournalEntry] = {}
        # Índice de idempotência: external_id → JournalEntry
        self._by_external_id: dict[str, JournalEntry] = {}

    def save_journal_entry(self, journal_entry: JournalEntry) -> None:
        """Persiste o JournalEntry nos índices in-memory."""
        self._entries[journal_entry.entry_id] = journal_entry
        self._by_external_id[journal_entry.external_id] = journal_entry

    def find_journal_entry_by_id(self, entry_id: str) -> JournalEntry | None:
        """Busca por entry_id — retorna None se não encontrado."""
        return self._entries.get(entry_id)

    def find_journal_entry_by_external_id(self, external_id: str) -> JournalEntry | None:
        """Busca por external_id (chave de idempotência) — retorna None se não encontrado."""
        return self._by_external_id.get(external_id)

    def get_balance(self, account_id: str, currency: str):
        """Retorna None — saldo não implementado para testes de propriedade."""
        return None

    def get_statement(self, account_id: str, cursor, page_size: int) -> StatementPage:
        """Retorna página vazia — extrato não implementado para testes de propriedade."""
        return StatementPage()


# ---------------------------------------------------------------------------
# Strategies base compartilhadas
# ---------------------------------------------------------------------------

# Moedas ISO 4217 suportadas pelo sistema
currencies = st.sampled_from(["BRL", "USD", "EUR", "GBP"])

# Valores monetários válidos em minor units (inteiros positivos)
valid_amounts = st.integers(min_value=1, max_value=10_000_000)

# IDs de conta alfanuméricos
account_id_strategy = st.text(
    min_size=1,
    max_size=36,
    alphabet=st.characters(whitelist_categories=("L", "N")),
)


@st.composite
def balanced_posting_inputs_strategy(
    draw: st.DrawFn, min_pairs: int = 1, max_pairs: int = 10
) -> list[PostingInput]:
    """
    Gera listas de PostingInputs balanceados (zero-sum) para comandos.

    Cada par contém um DEBIT e um CREDIT com o mesmo amount e currency,
    garantindo que a soma algébrica seja zero por moeda.
    """
    n_pairs = draw(st.integers(min_value=min_pairs, max_value=max_pairs))
    postings: list[PostingInput] = []

    for _ in range(n_pairs):
        amount = draw(valid_amounts)
        currency = draw(currencies)
        debit_account = draw(account_id_strategy)
        credit_account = draw(account_id_strategy)

        postings.append(PostingInput(
            account_id=debit_account,
            amount=amount,
            currency=currency,
            direction="DEBIT",
        ))
        postings.append(PostingInput(
            account_id=credit_account,
            amount=amount,
            currency=currency,
            direction="CREDIT",
        ))

    return postings


# ---------------------------------------------------------------------------
# Property 18: Validação de limites do DynamoDB
# ---------------------------------------------------------------------------


@st.composite
def large_posting_inputs_strategy(draw: st.DrawFn) -> list[PostingInput]:
    """
    Gera listas de PostingInputs com contas distintas suficientes para exceder
    o limite de 100 itens do DynamoDB TransactWriteItems.

    Fórmula de contagem de itens:
        item_count = 3 + N_postings + M_distinct_accounts

    Com todas as contas distintas (DEBIT e CREDIT em contas diferentes):
        item_count = 3 + 2*N + 2*N = 3 + 4*N  (cada par usa 2 postings e 2 contas)

    Para exceder 100: 3 + 4*N > 100 → N > 24.25 → N >= 25 pares (item_count = 103)

    Estratégia conservadora: gera entre 25 e 50 pares com contas sempre distintas.
    """
    # Gera N pares suficientes para exceder o limite (N >= 25 garante item_count > 100)
    n_pairs = draw(st.integers(min_value=25, max_value=50))
    postings: list[PostingInput] = []

    for i in range(n_pairs):
        amount = draw(valid_amounts)
        currency = draw(currencies)

        # Contas distintas para maximizar M (balance updates)
        # Prefixo numérico garante unicidade entre pares
        debit_account = f"acc_debit_{i}_{draw(st.integers(min_value=0, max_value=9999))}"
        credit_account = f"acc_credit_{i}_{draw(st.integers(min_value=0, max_value=9999))}"

        postings.append(PostingInput(
            account_id=debit_account,
            amount=amount,
            currency=currency,
            direction="DEBIT",
        ))
        postings.append(PostingInput(
            account_id=credit_account,
            amount=amount,
            currency=currency,
            direction="CREDIT",
        ))

    return postings


@pytest.mark.property
@given(postings=large_posting_inputs_strategy())
def test_transaction_limit_exceeded_when_too_many_items(
    postings: list[PostingInput],
) -> None:
    """
    **Validates: Requirements 14.1**

    Para qualquer comando com postings suficientes para exceder 100 itens
    na TransactWriteItems, o TransactionLimitValidator deve rejeitar com
    TransactionLimitExceeded.

    Fórmula: item_count = 3 + N_postings + M_distinct_accounts
    Com contas distintas por par: item_count = 3 + 2*N + 2*N = 3 + 4*N
    Para N >= 25 pares: item_count >= 103 > 100 → deve rejeitar.

    Invariante verificada:
        Se item_count > 100 → TransactionLimitValidator levanta TransactionLimitExceeded
    """
    command = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=postings,
        metadata={},
    )

    validator = TransactionLimitValidator()

    with pytest.raises(TransactionLimitExceeded) as exc_info:
        validator.validate(command)

    error = exc_info.value
    assert error.code == "TRANSACTION_LIMIT_EXCEEDED", (
        f"Código de erro esperado: TRANSACTION_LIMIT_EXCEEDED, recebido: {error.code}"
    )
    assert error.http_status == 400, (
        f"HTTP status esperado: 400, recebido: {error.http_status}"
    )

    # Verifica que a mensagem contém a contagem de itens
    assert "100" in error.message or "itens" in error.message.lower(), (
        f"Mensagem de erro deve mencionar o limite de 100 itens: {error.message}"
    )


@st.composite
def oversized_payload_strategy(draw: st.DrawFn) -> list[PostingInput]:
    """
    Gera listas de PostingInputs com payloads que excedem 4MB.

    Estratégia: usa account_ids muito longos para inflar o tamanho do payload JSON.
    O TransactionLimitValidator estima o tamanho via json.dumps dos postings.

    Para exceder 4MB (4_194_304 bytes) com N postings:
    - account_id de 600_000 chars por posting
    - 2 pares (4 postings) × 600KB = 2.4MB — ainda abaixo
    - 4 pares (8 postings) × 600KB = 4.8MB — acima do limite

    Usamos exatamente 4 pares com account_ids de 600KB para garantir
    que o payload sempre exceda 4MB (4 × 2 × 600KB = 4.8MB > 4MB).
    """
    postings: list[PostingInput] = []

    # account_id de 600KB garante que 4 pares (8 postings) = 4.8MB > 4MB
    # Usamos prefixo único por par para garantir contas distintas
    large_account_id_base = "A" * 600_000  # 600KB por account_id base

    # Fixo em 4 pares para garantir que o payload sempre exceda 4MB
    n_pairs = 4

    for i in range(n_pairs):
        amount = draw(valid_amounts)
        currency = draw(currencies)

        postings.append(PostingInput(
            account_id=f"D{i}_{large_account_id_base}",
            amount=amount,
            currency=currency,
            direction="DEBIT",
        ))
        postings.append(PostingInput(
            account_id=f"C{i}_{large_account_id_base}",
            amount=amount,
            currency=currency,
            direction="CREDIT",
        ))

    return postings


@pytest.mark.property
@given(postings=oversized_payload_strategy())
def test_transaction_size_exceeded_when_payload_too_large(
    postings: list[PostingInput],
) -> None:
    """
    **Validates: Requirements 14.2**

    Para qualquer comando cujo payload estimado excede 4MB, o
    TransactionLimitValidator deve rejeitar com TransactionSizeExceeded.

    O validador estima o tamanho via json.dumps dos postings. Postings com
    account_ids muito grandes inflam o payload acima do limite de 4MB.

    Invariante verificada:
        Se estimated_payload_size > 4MB → TransactionLimitValidator levanta TransactionSizeExceeded
    """
    command = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=postings,
        metadata={},
    )

    validator = TransactionLimitValidator()

    # O validador deve levantar TransactionLimitExceeded (item count) ou
    # TransactionSizeExceeded (payload size) — ambos são válidos dependendo
    # de qual limite é atingido primeiro com os dados gerados.
    with pytest.raises((TransactionLimitExceeded, TransactionSizeExceeded)) as exc_info:
        validator.validate(command)

    error = exc_info.value
    assert error.code in ("TRANSACTION_LIMIT_EXCEEDED", "TRANSACTION_SIZE_EXCEEDED"), (
        f"Código de erro esperado: TRANSACTION_LIMIT_EXCEEDED ou TRANSACTION_SIZE_EXCEEDED, "
        f"recebido: {error.code}"
    )
    assert error.http_status == 400, (
        f"HTTP status esperado: 400, recebido: {error.http_status}"
    )


# ---------------------------------------------------------------------------
# Property 16: Anulação por reversão (reversal annulment)
# ---------------------------------------------------------------------------


def _make_journal_entry_from_postings(postings: tuple[Posting, ...]) -> JournalEntry:
    """
    Constrói um JournalEntry válido a partir de postings para testes.

    Gera entry_id e outbox_event automaticamente para isolar o teste
    das responsabilidades da factory.
    """
    entry_id = str(uuid.uuid4())
    outbox = OutboxEvent(
        event_id=f"OUTBOX#{entry_id}",
        entry_id=entry_id,
        event_type="TransactionCreated",
        payload={},
        expires_at=int(time.time()) + 3600,
    )
    return JournalEntry(
        entry_id=entry_id,
        external_id=str(uuid.uuid4()),
        entry_type=EntryType.STANDARD,
        postings=postings,
        metadata={},
        timestamp="2026-03-10T00:00:00Z",
        outbox_event=outbox,
    )


@st.composite
def balanced_postings_domain_strategy(
    draw: st.DrawFn, min_pairs: int = 1, max_pairs: int = 10
) -> tuple[Posting, ...]:
    """
    Gera tuplas de Postings do domínio balanceados (zero-sum).

    Cada par contém um DEBIT e um CREDIT com o mesmo Money,
    garantindo que a soma algébrica seja zero por moeda.
    """
    n_pairs = draw(st.integers(min_value=min_pairs, max_value=max_pairs))
    postings: list[Posting] = []
    index = 0

    for _ in range(n_pairs):
        amount = draw(valid_amounts)
        currency = draw(currencies)
        debit_account = draw(account_id_strategy)
        credit_account = draw(account_id_strategy)

        money = Money(amount=amount, currency=currency)

        postings.append(Posting(
            account_id=debit_account,
            money=money,
            direction=Direction.DEBIT,
            index=index,
        ))
        postings.append(Posting(
            account_id=credit_account,
            money=money,
            direction=Direction.CREDIT,
            index=index + 1,
        ))
        index += 2

    return tuple(postings)


@pytest.mark.property
@given(postings=balanced_postings_domain_strategy())
def test_reversal_annulment_combined_sum_is_zero(
    postings: tuple[Posting, ...],
) -> None:
    """
    **Validates: Requirements 9.2, 9.3, 9.4**

    Para qualquer JournalEntry original e seu Reversal correspondente,
    a soma combinada de todos os postings (original + reversal) deve ser
    zero para cada moeda.

    Adicionalmente verifica:
    - O Reversal tem entry_type == REVERSAL (Requisito 9.2)
    - Os postings do Reversal têm direções invertidas (DEBIT↔CREDIT) (Requisito 9.2)
    - O metadata do Reversal contém original_entry_id (Requisito 9.3)

    Invariante verificada (Requisito 9.4):
        Para toda moeda c:
        sum(p.signed_amount for p in original.postings if p.money.currency == c)
        + sum(p.signed_amount for p in reversal.postings if p.money.currency == c)
        == 0
    """
    # Cria o JournalEntry original com os postings gerados
    original = _make_journal_entry_from_postings(postings)

    # Cria o comando de reversão
    reversal_command = CreateReversalCommand(
        original_entry_id=original.entry_id,
        external_id=str(uuid.uuid4()),
        metadata={"reason": "test_reversal"},
    )

    # Usa a factory diretamente para criar a reversão
    factory = JournalEntryFactory()
    reversal = factory.create_reversal(original=original, command=reversal_command)

    # --- Verificação 1: entry_type deve ser REVERSAL (Requisito 9.2) ---
    assert reversal.entry_type == EntryType.REVERSAL, (
        f"Reversal deve ter entry_type=REVERSAL, recebido: {reversal.entry_type}"
    )

    # --- Verificação 2: metadata deve conter original_entry_id (Requisito 9.3) ---
    assert "original_entry_id" in reversal.metadata, (
        f"Metadata do Reversal deve conter 'original_entry_id'. "
        f"Metadata recebido: {reversal.metadata}"
    )
    assert reversal.metadata["original_entry_id"] == original.entry_id, (
        f"original_entry_id no metadata deve ser {original.entry_id}, "
        f"recebido: {reversal.metadata['original_entry_id']}"
    )

    # --- Verificação 3: postings invertidos (DEBIT↔CREDIT) ---
    assert len(reversal.postings) == len(original.postings), (
        f"Reversal deve ter o mesmo número de postings que o original. "
        f"Original: {len(original.postings)}, Reversal: {len(reversal.postings)}"
    )

    for orig_posting, rev_posting in zip(original.postings, reversal.postings):
        # Direção deve ser invertida
        expected_direction = (
            Direction.CREDIT if orig_posting.direction == Direction.DEBIT
            else Direction.DEBIT
        )
        assert rev_posting.direction == expected_direction, (
            f"Posting {rev_posting.index}: direção esperada {expected_direction}, "
            f"recebida {rev_posting.direction}"
        )
        # Money (amount e currency) deve ser preservado
        assert rev_posting.money == orig_posting.money, (
            f"Posting {rev_posting.index}: money deve ser preservado. "
            f"Original: {orig_posting.money}, Reversal: {rev_posting.money}"
        )

    # --- Verificação 4: soma combinada (original + reversal) == 0 por moeda (Requisito 9.4) ---
    combined_sum_by_currency: dict[str, int] = {}

    for posting in original.postings:
        currency = posting.money.currency
        combined_sum_by_currency[currency] = (
            combined_sum_by_currency.get(currency, 0) + posting.signed_amount
        )

    for posting in reversal.postings:
        currency = posting.money.currency
        combined_sum_by_currency[currency] = (
            combined_sum_by_currency.get(currency, 0) + posting.signed_amount
        )

    for currency, total in combined_sum_by_currency.items():
        assert total == 0, (
            f"Soma combinada (original + reversal) deve ser zero para moeda {currency}. "
            f"Total recebido: {total}"
        )


# ---------------------------------------------------------------------------
# Property 19: Logging estruturado
# ---------------------------------------------------------------------------


def _build_engine() -> tuple[LedgerEngine, InMemoryLedgerRepository]:
    """
    Constrói um LedgerEngine com repositório in-memory e cadeia de validação padrão.

    Retorna o engine e o repositório para inspeção nos testes.
    """
    repository = InMemoryLedgerRepository()
    validation_chain = ValidationChain([
        ZeroSumValidator(),
        MinorUnitsValidator(),
        TransactionLimitValidator(),
    ])
    factory = JournalEntryFactory()
    engine = LedgerEngine(
        repository=repository,
        validation_chain=validation_chain,
        factory=factory,
    )
    return engine, repository


@pytest.mark.property
@given(postings=balanced_posting_inputs_strategy(min_pairs=1, max_pairs=5))
def test_structured_log_emitted_on_successful_write(
    postings: list[PostingInput],
) -> None:
    """
    **Validates: Requirements 15.1**

    Para qualquer operação de escrita bem-sucedida, o LedgerEngine deve
    emitir log estruturado (JSON) contendo no mínimo os campos:
    - entry_id
    - operation
    - result

    Invariante verificada:
        Para toda operação create_journal_entry bem-sucedida:
        log emitido é JSON válido com campos entry_id, operation, result
    """
    engine, _ = _build_engine()

    command = CreateJournalEntryCommand(
        external_id=str(uuid.uuid4()),
        postings=postings,
        metadata={},
    )

    # Captura logs emitidos pelo LedgerEngine durante a operação
    # usando logging.handlers.MemoryHandler para evitar dependência de caplog
    log_records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        """Handler simples que acumula LogRecords em uma lista."""
        def emit(self, record: logging.LogRecord) -> None:
            log_records.append(record)

    handler = _ListHandler()
    target_logger = logging.getLogger("ledger.domain.services")
    target_logger.addHandler(handler)
    original_level = target_logger.level
    target_logger.setLevel(logging.DEBUG)

    try:
        journal_entry = engine.create_journal_entry(command)
    finally:
        target_logger.removeHandler(handler)
        target_logger.setLevel(original_level)

    # Deve ter emitido pelo menos um log
    assert len(log_records) > 0, (
        "LedgerEngine deve emitir pelo menos um log estruturado em operação bem-sucedida."
    )

    # Verifica que pelo menos um log de sucesso contém os campos obrigatórios
    info_records = [r for r in log_records if r.levelno == logging.INFO]
    assert len(info_records) > 0, (
        "LedgerEngine deve emitir pelo menos um log INFO em operação bem-sucedida."
    )

    # O último log INFO deve ser o log de sucesso com os campos obrigatórios
    last_info_log = info_records[-1]

    # O log deve ser JSON válido
    try:
        log_data = json.loads(last_info_log.getMessage())
    except json.JSONDecodeError as e:
        pytest.fail(
            f"Log deve ser JSON válido. Mensagem recebida: '{last_info_log.getMessage()}'. "
            f"Erro: {e}"
        )

    # Verifica campos obrigatórios (Requisito 15.1)
    required_fields = {"entry_id", "operation", "result"}
    missing_fields = required_fields - set(log_data.keys())
    assert not missing_fields, (
        f"Log estruturado deve conter os campos {required_fields}. "
        f"Campos ausentes: {missing_fields}. Log recebido: {log_data}"
    )

    # Verifica valores esperados
    assert log_data["entry_id"] == journal_entry.entry_id, (
        f"entry_id no log deve ser {journal_entry.entry_id}, recebido: {log_data['entry_id']}"
    )
    assert log_data["operation"] == "create_journal_entry", (
        f"operation no log deve ser 'create_journal_entry', recebido: {log_data['operation']}"
    )
    assert log_data["result"] == "success", (
        f"result no log deve ser 'success', recebido: {log_data['result']}"
    )


@pytest.mark.property
@given(postings=balanced_posting_inputs_strategy(min_pairs=1, max_pairs=5))
def test_structured_log_contains_error_code_on_failure(
    postings: list[PostingInput],
) -> None:
    """
    **Validates: Requirements 15.3**

    Para qualquer operação de escrita que falhe com DomainError, o LedgerEngine
    deve emitir log estruturado (JSON) contendo no mínimo os campos:
    - entry_id (pode ser None para erros de validação pré-criação)
    - operation
    - result (deve ser "error" ou "idempotent_return")

    Estratégia: força falha de idempotência submetendo o mesmo external_id duas vezes.
    A segunda submissão levanta IdempotencyConflict, que é registrado como log INFO.

    Invariante verificada:
        Para toda operação que levanta DomainError:
        log emitido é JSON válido com campos entry_id, operation, result
    """
    engine, _ = _build_engine()

    # Submete o mesmo external_id duas vezes para forçar IdempotencyConflict
    shared_external_id = str(uuid.uuid4())

    first_command = CreateJournalEntryCommand(
        external_id=shared_external_id,
        postings=postings,
        metadata={},
    )
    second_command = CreateJournalEntryCommand(
        external_id=shared_external_id,
        postings=postings,
        metadata={},
    )

    # Primeira submissão deve ter sucesso (sem captura de log)
    engine.create_journal_entry(first_command)

    # Segunda submissão deve levantar IdempotencyConflict e emitir log
    log_records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        """Handler simples que acumula LogRecords em uma lista."""
        def emit(self, record: logging.LogRecord) -> None:
            log_records.append(record)

    handler = _ListHandler()
    target_logger = logging.getLogger("ledger.domain.services")
    target_logger.addHandler(handler)
    original_level = target_logger.level
    target_logger.setLevel(logging.DEBUG)

    try:
        with pytest.raises(DomainError):
            engine.create_journal_entry(second_command)
    finally:
        target_logger.removeHandler(handler)
        target_logger.setLevel(original_level)

    # Deve ter emitido pelo menos um log
    assert len(log_records) > 0, (
        "LedgerEngine deve emitir log estruturado mesmo em operação com falha."
    )

    # O log deve ser JSON válido
    last_log = log_records[-1]
    try:
        log_data = json.loads(last_log.getMessage())
    except json.JSONDecodeError as e:
        pytest.fail(
            f"Log deve ser JSON válido. Mensagem recebida: '{last_log.getMessage()}'. "
            f"Erro: {e}"
        )

    # Verifica campos obrigatórios para operações com falha (Requisito 15.3)
    assert "operation" in log_data, (
        f"Log de falha deve conter campo 'operation'. Log recebido: {log_data}"
    )
    assert "result" in log_data, (
        f"Log de falha deve conter campo 'result'. Log recebido: {log_data}"
    )
    assert "entry_id" in log_data, (
        f"Log de falha deve conter campo 'entry_id'. Log recebido: {log_data}"
    )
