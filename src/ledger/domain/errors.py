"""
Hierarquia de erros do domínio do Double-Entry Ledger.

Todos os erros de domínio herdam de DomainError e carregam:
- code: código de erro estruturado (ex: ZERO_SUM_VIOLATION)
- message: descrição legível por humanos
- http_status: código HTTP correspondente para a camada de API

Mapeamento de erros para HTTP status codes:
- 400 Bad Request: erros de validação (ZeroSumViolation, InvalidAmountType,
                   TransactionLimitExceeded, TransactionSizeExceeded)
- 404 Not Found:   recurso não encontrado (JournalEntryNotFound)
- 409 Conflict:    conflito de concorrência (OptimisticLockConflict)
- 200 OK:          idempotência — requisição duplicada retorna resultado original
                   (IdempotencyConflict), não é um erro real do ponto de vista do cliente

A camada de API (write_handler.py) é responsável por traduzir estes erros
em respostas HTTP estruturadas usando o campo http_status de cada instância.

Requisitos cobertos: 1.2, 2.2, 4.2, 5.2, 14.1, 14.2
"""


class DomainError(Exception):
    """
    Erro base do domínio.

    Todos os erros específicos do domínio herdam desta classe.
    Carrega um código estruturado (code), uma mensagem legível (message)
    e o HTTP status code correspondente (http_status) para facilitar
    a tradução na camada de API sem acoplamento entre camadas.
    """

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        # Armazena o código estruturado para identificação programática do erro
        self.code = code
        # Mensagem legível por humanos — pode ser exposta ao cliente da API
        self.message = message
        # HTTP status code para tradução na camada de API
        self.http_status = http_status
        # Inicializa a Exception base com a mensagem para compatibilidade com str(e)
        super().__init__(message)


class ZeroSumViolation(DomainError):
    """
    Violação da regra de partidas dobradas (zero-sum).

    Levantado quando a soma algébrica dos postings de um JournalEntry
    não é zero para pelo menos uma moeda. Esta é a invariante central
    do subledger: débitos e créditos devem se equilibrar por moeda.

    Exemplo: postings com BRL somando 100 (em vez de 0) levantam este erro.

    Requisito: 1.2
    """

    def __init__(self, currency: str, total: int) -> None:
        super().__init__(
            code="ZERO_SUM_VIOLATION",
            message=f"Postings não somam zero para moeda {currency}: total={total}",
            http_status=400,
        )


class InvalidAmountType(DomainError):
    """
    Valor monetário com tipo inválido.

    Levantado quando um amount não é um inteiro (int). O subledger
    representa valores monetários exclusivamente em minor units (inteiros)
    para evitar erros de arredondamento de ponto flutuante.

    Exemplo: amount=10.50 (float) ou amount=Decimal("10.50") levantam este erro.

    Requisito: 2.2
    """

    def __init__(self, received_type: str) -> None:
        super().__init__(
            code="INVALID_AMOUNT_TYPE",
            message=f"Valor monetário deve ser int, recebido: {received_type}",
            http_status=400,
        )


class OptimisticLockConflict(DomainError):
    """
    Conflito de versão no Balance (Optimistic Concurrency Control).

    Levantado quando a versão esperada do Balance diverge da versão
    atual no DynamoDB durante uma TransactWriteItems. Indica que outro
    processo atualizou o saldo concorrentemente.

    O cliente deve retentar a operação com os dados atualizados.

    Requisito: 4.2, 5.2
    """

    def __init__(self, account_id: str, expected_version: int) -> None:
        super().__init__(
            code="OPTIMISTIC_LOCK_CONFLICT",
            message=f"Conflito de versão para conta {account_id}, version esperada: {expected_version}",
            http_status=409,
        )


class IdempotencyConflict(DomainError):
    """
    Requisição duplicada detectada via external_id (idempotência).

    Levantado quando um external_id já existe no sistema. Não é um erro
    real — indica que a mesma operação foi submetida mais de uma vez.
    O campo existing_entry_id permite ao cliente recuperar o resultado
    original sem reprocessamento.

    HTTP status 200 (não 409) porque a operação foi bem-sucedida
    anteriormente e o resultado original é retornado ao cliente.

    Requisito: 4.2
    """

    def __init__(self, external_id: str, existing_entry_id: str) -> None:
        # Armazena o entry_id original para que a camada de API possa
        # retornar o resultado da operação original ao cliente
        self.existing_entry_id = existing_entry_id
        super().__init__(
            code="IDEMPOTENCY_CONFLICT",
            message=f"external_id {external_id} já existe com entry_id {existing_entry_id}",
            http_status=200,  # Idempotência retorna 200, não erro
        )


class TransactionLimitExceeded(DomainError):
    """
    Número de itens excede o limite do DynamoDB TransactWriteItems.

    O DynamoDB limita TransactWriteItems a 100 itens por transação.
    Para um JournalEntry com N postings afetando M contas distintas,
    o total de itens é: 3 + N + M (JournalEntry + Idempotency + OutboxEvent
    + N Postings + M Balance updates).

    Requisito: 14.1
    """

    def __init__(self, item_count: int) -> None:
        super().__init__(
            code="TRANSACTION_LIMIT_EXCEEDED",
            message=f"TransactWriteItems excede 100 itens: {item_count}",
            http_status=400,
        )


class TransactionSizeExceeded(DomainError):
    """
    Payload excede o limite de tamanho do DynamoDB TransactWriteItems.

    O DynamoDB limita o payload total de uma TransactWriteItems a 4MB.
    Este erro é levantado preventivamente pelo TransactionLimitValidator
    antes de tentar a operação no DynamoDB.

    Requisito: 14.2
    """

    def __init__(self, size_bytes: int) -> None:
        super().__init__(
            code="TRANSACTION_SIZE_EXCEEDED",
            message=f"TransactWriteItems excede 4MB: {size_bytes} bytes",
            http_status=400,
        )


class JournalEntryNotFound(DomainError):
    """
    Journal entry não encontrado.

    Levantado quando uma busca por entry_id não retorna resultado.
    Usado principalmente no fluxo de reversão, quando o entry original
    referenciado não existe no sistema.

    Requisito: 9.x (reversão de entry inexistente)
    """

    def __init__(self, entry_id: str) -> None:
        super().__init__(
            code="JOURNAL_ENTRY_NOT_FOUND",
            message=f"Journal entry não encontrado: {entry_id}",
            http_status=404,
        )
