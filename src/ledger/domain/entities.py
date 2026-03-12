"""
Entidades do domínio do Double-Entry Ledger.

Entidades são objetos com identidade própria que persistem ao longo do tempo.
Diferente de Value Objects, entidades são identificadas pelo seu account_id,
não pelos seus atributos — duas contas com o mesmo tenant e tipo são entidades
distintas se possuírem account_ids diferentes.

A entidade principal deste módulo é Account, que representa uma conta no
subledger. Cada conta pertence a um tenant e possui um tipo que determina
seu papel nas operações financeiras (Available, Hold, Fees, Clearing).

Requisitos atendidos: 6.1, 6.4, 6.5
"""
from __future__ import annotations

from dataclasses import dataclass

from ledger.domain.value_objects import AccountType


@dataclass
class Account:
    """
    Entidade que representa uma conta no subledger de partidas dobradas.

    Cada conta pertence a um tenant (isolamento multi-tenant) e possui um tipo
    que determina seu papel nas operações financeiras:

    - AVAILABLE: saldo líquido disponível do usuário para uso imediato
    - HOLD: saldo bloqueado do usuário (reservado para operações pendentes)
    - FEES: conta de plataforma para registro de taxas cobradas
    - CLEARING: conta de plataforma para compensação entre partes

    Regra de negócio (Requisito 6.1):
    Cada usuário deve ter no mínimo duas contas: uma AVAILABLE e uma HOLD.
    Operações de bloqueio movimentam saldo de AVAILABLE → HOLD via JournalEntry.
    Operações de liberação movimentam saldo de HOLD → AVAILABLE via JournalEntry.

    Imutabilidade:
    Account NÃO é frozen porque status pode ser alterado (ACTIVE → INACTIVE).
    No entanto, account_id, tenant_id e account_type são imutáveis por convenção
    — alterações nesses campos exigiriam criação de nova conta.

    Campos:
        account_id:   Identificador único da conta (UUID v4 recomendado).
        tenant_id:    Identificador do tenant proprietário da conta.
        account_type: Tipo da conta (AccountType enum).
        status:       Estado atual da conta — "ACTIVE" ou "INACTIVE".
        created_at:   Timestamp de criação no formato ISO 8601.
    """

    account_id: str          # identificador único da conta (UUID v4)
    tenant_id: str           # tenant proprietário — isolamento multi-tenant
    account_type: AccountType  # tipo da conta (AVAILABLE, HOLD, FEES, CLEARING)
    status: str              # "ACTIVE" | "INACTIVE"
    created_at: str          # timestamp de criação (ISO 8601)
