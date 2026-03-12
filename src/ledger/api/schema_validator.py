"""
Schema Validator da camada de API do Double-Entry Ledger.

Responsabilidade: validar o payload JSON de entrada antes de invocar
qualquer lógica de domínio. Esta é a primeira linha de defesa contra
dados malformados ou com tipos incorretos.

Validações realizadas:
1. Presença de campos obrigatórios
2. Tipos corretos (rejeita float/decimal em amount — Requisito 2.2)
3. Estrutura do payload (postings é lista, metadata é dict, etc.)

Importante: esta camada NÃO valida regras de negócio (zero-sum, minor units > 0).
Essas validações são responsabilidade do ValidationChain no domínio.
O objetivo aqui é rejeitar payloads estruturalmente inválidos antes de
construir DTOs ou invocar o engine.

Requisitos atendidos: 2.2, 16.3
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Resultado de validação
# ---------------------------------------------------------------------------


@dataclass
class SchemaValidationResult:
    """
    Resultado da validação de schema.

    Carrega o status (válido/inválido) e a lista de erros encontrados.
    Erros são acumulados para que o cliente receba todos os problemas
    de uma vez, em vez de um por um.
    """

    is_valid: bool
    errors: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls) -> "SchemaValidationResult":
        """Cria resultado de validação bem-sucedida."""
        return cls(is_valid=True)

    @classmethod
    def fail(cls, errors: list[str]) -> "SchemaValidationResult":
        """Cria resultado de validação com falha."""
        return cls(is_valid=False, errors=errors)


# ---------------------------------------------------------------------------
# Validador de schema para POST /entries
# ---------------------------------------------------------------------------


def validate_create_entry_payload(payload: Any) -> SchemaValidationResult:
    """
    Valida o payload JSON de POST /entries.

    Verifica:
    - payload é um dict
    - external_id presente e é string não-vazia
    - postings presente, é lista não-vazia
    - cada posting tem account_id (str), amount (int, não float/bool),
      currency (str de 3 chars), direction ("DEBIT" ou "CREDIT")
    - metadata, se presente, é dict

    Rejeita explicitamente float e bool em amount (Requisito 2.2):
    - bool é subclasse de int em Python, então verificamos bool antes de int
    - float é rejeitado com código INVALID_AMOUNT_TYPE

    Args:
        payload: Objeto Python desserializado do JSON da requisição.

    Returns:
        SchemaValidationResult com is_valid=True ou lista de erros.
    """
    errors: list[str] = []

    # Verifica que o payload é um dict
    if not isinstance(payload, dict):
        return SchemaValidationResult.fail(
            [f"Payload deve ser um objeto JSON, recebido: {type(payload).__name__}"]
        )

    # Valida external_id
    external_id = payload.get("external_id")
    if external_id is None:
        errors.append("Campo obrigatório ausente: 'external_id'")
    elif not isinstance(external_id, str) or not external_id.strip():
        errors.append("'external_id' deve ser uma string não-vazia")

    # Valida postings
    postings = payload.get("postings")
    if postings is None:
        errors.append("Campo obrigatório ausente: 'postings'")
    elif not isinstance(postings, list):
        errors.append(f"'postings' deve ser uma lista, recebido: {type(postings).__name__}")
    elif len(postings) == 0:
        errors.append("'postings' não pode ser uma lista vazia")
    else:
        # Valida cada posting individualmente
        for i, posting in enumerate(postings):
            posting_errors = _validate_posting(posting, index=i)
            errors.extend(posting_errors)

    # Valida metadata (opcional, mas se presente deve ser dict)
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append(
            f"'metadata' deve ser um objeto JSON (dict), recebido: {type(metadata).__name__}"
        )

    if errors:
        return SchemaValidationResult.fail(errors)
    return SchemaValidationResult.ok()


# ---------------------------------------------------------------------------
# Validador de schema para POST /reversals
# ---------------------------------------------------------------------------


def validate_create_reversal_payload(payload: Any) -> SchemaValidationResult:
    """
    Valida o payload JSON de POST /reversals.

    Verifica:
    - payload é um dict
    - original_entry_id presente e é string não-vazia
    - external_id presente e é string não-vazia
    - metadata, se presente, é dict

    Args:
        payload: Objeto Python desserializado do JSON da requisição.

    Returns:
        SchemaValidationResult com is_valid=True ou lista de erros.
    """
    errors: list[str] = []

    # Verifica que o payload é um dict
    if not isinstance(payload, dict):
        return SchemaValidationResult.fail(
            [f"Payload deve ser um objeto JSON, recebido: {type(payload).__name__}"]
        )

    # Valida original_entry_id
    original_entry_id = payload.get("original_entry_id")
    if original_entry_id is None:
        errors.append("Campo obrigatório ausente: 'original_entry_id'")
    elif not isinstance(original_entry_id, str) or not original_entry_id.strip():
        errors.append("'original_entry_id' deve ser uma string não-vazia")

    # Valida external_id
    external_id = payload.get("external_id")
    if external_id is None:
        errors.append("Campo obrigatório ausente: 'external_id'")
    elif not isinstance(external_id, str) or not external_id.strip():
        errors.append("'external_id' deve ser uma string não-vazia")

    # Valida metadata (opcional)
    metadata = payload.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        errors.append(
            f"'metadata' deve ser um objeto JSON (dict), recebido: {type(metadata).__name__}"
        )

    if errors:
        return SchemaValidationResult.fail(errors)
    return SchemaValidationResult.ok()


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------


def _validate_posting(posting: Any, index: int) -> list[str]:
    """
    Valida um posting individual dentro da lista de postings.

    Verifica campos obrigatórios, tipos e valores permitidos.
    Rejeita explicitamente float e bool em amount (Requisito 2.2).

    Args:
        posting: Objeto do posting a ser validado.
        index:   Índice do posting na lista (para mensagens de erro contextuais).

    Returns:
        Lista de erros encontrados. Vazia se o posting é válido.
    """
    errors: list[str] = []
    prefix = f"postings[{index}]"

    if not isinstance(posting, dict):
        errors.append(f"{prefix}: deve ser um objeto JSON, recebido: {type(posting).__name__}")
        # Não faz sentido continuar validando campos se o posting não é dict
        return errors

    # Valida account_id
    account_id = posting.get("account_id")
    if account_id is None:
        errors.append(f"{prefix}: campo obrigatório ausente: 'account_id'")
    elif not isinstance(account_id, str) or not account_id.strip():
        errors.append(f"{prefix}: 'account_id' deve ser uma string não-vazia")

    # Valida amount — rejeita float e bool explicitamente (Requisito 2.2)
    amount = posting.get("amount")
    if amount is None:
        errors.append(f"{prefix}: campo obrigatório ausente: 'amount'")
    else:
        amount_errors = _validate_amount_type(amount, prefix)
        errors.extend(amount_errors)

    # Valida currency
    currency = posting.get("currency")
    if currency is None:
        errors.append(f"{prefix}: campo obrigatório ausente: 'currency'")
    elif not isinstance(currency, str):
        errors.append(f"{prefix}: 'currency' deve ser uma string ISO 4217")
    elif len(currency) != 3:
        errors.append(
            f"{prefix}: 'currency' deve ter exatamente 3 caracteres ISO 4217, "
            f"recebido: '{currency}' ({len(currency)} chars)"
        )

    # Valida direction
    direction = posting.get("direction")
    if direction is None:
        errors.append(f"{prefix}: campo obrigatório ausente: 'direction'")
    elif direction not in ("DEBIT", "CREDIT"):
        errors.append(
            f"{prefix}: 'direction' deve ser 'DEBIT' ou 'CREDIT', recebido: '{direction}'"
        )

    return errors


def _validate_amount_type(amount: Any, prefix: str) -> list[str]:
    """
    Valida o tipo do campo amount de um posting.

    Regras (Requisito 2.2):
    - bool é rejeitado (True/False não são valores monetários válidos)
    - float é rejeitado (valores monetários devem ser inteiros em minor units)
    - int é aceito (validação de valor > 0 é feita pelo MinorUnitsValidator no domínio)

    A verificação de bool ANTES de int é necessária porque bool é subclasse
    de int em Python: isinstance(True, int) == True.

    Args:
        amount: Valor do campo amount a ser validado.
        prefix: Prefixo para mensagens de erro (ex: "postings[0]").

    Returns:
        Lista de erros. Vazia se o tipo é válido.
    """
    # bool deve ser verificado antes de int (bool é subclasse de int em Python)
    if isinstance(amount, bool):
        return [
            f"{prefix}: 'amount' deve ser um inteiro (minor units), "
            f"recebido: bool ({amount}). Use inteiros como 1050 para R$ 10,50."
        ]

    if isinstance(amount, float):
        return [
            f"{prefix}: 'amount' deve ser um inteiro (minor units), "
            f"recebido: float ({amount}). "
            f"Valores monetários são representados em centavos: use {int(amount * 100)} "
            f"em vez de {amount}."
        ]

    if not isinstance(amount, int):
        return [
            f"{prefix}: 'amount' deve ser um inteiro (minor units), "
            f"recebido: {type(amount).__name__}"
        ]

    # Tipo válido — validação de valor (> 0) é responsabilidade do MinorUnitsValidator
    return []
