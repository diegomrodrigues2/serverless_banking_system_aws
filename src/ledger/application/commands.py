"""
Comandos da camada de aplicação do Double-Entry Ledger.

Comandos representam intenções de escrita no sistema (padrão CQRS).
São DTOs imutáveis que transportam os dados necessários para que o
LedgerEngine execute uma operação de escrita.

Componentes:
- PostingInput: DTO para um posting individual dentro de um comando
- CreateJournalEntryCommand: comando para criar um novo lançamento contábil
- CreateReversalCommand: comando para criar uma reversão de lançamento existente

Estes comandos são criados pela camada de API (write_handler.py) a partir
dos DTOs de request e passados para o LedgerEngine via CommandHandler.

Requisitos atendidos: 1.4, 4.1, 8.1, 9.2
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PostingInput:
    """
    DTO de entrada para um posting individual dentro de um comando de criação.

    Representa os dados brutos de um posting antes da conversão para o
    Value Object Posting do domínio. A validação de tipos (int > 0) é
    responsabilidade do MinorUnitsValidator na ValidationChain.

    Campos:
        account_id: Identificador da conta afetada pelo posting.
        amount:     Valor em minor units (centavos). Tipo raw — validado
                    pelo MinorUnitsValidator (deve ser int > 0).
        currency:   Código ISO 4217 da moeda (ex: "BRL", "USD").
        direction:  Direção do posting: "DEBIT" (débito) ou "CREDIT" (crédito).

    Exemplo:
        PostingInput(
            account_id="acc_available_001",
            amount=1050,       # R$ 10,50 em minor units
            currency="BRL",
            direction="DEBIT",
        )
    """

    account_id: str
    # Tipo raw — validado pelo MinorUnitsValidator antes de chegar à factory.
    # Aceita qualquer tipo para que o validador possa rejeitar float/bool/etc.
    amount: object
    currency: str
    # "DEBIT" | "CREDIT" — convertido para Direction enum pela JournalEntryFactory
    direction: str


@dataclass
class CreateJournalEntryCommand:
    """
    Comando para criar um novo lançamento contábil (JournalEntry padrão).

    Transporta todos os dados necessários para que o LedgerEngine execute
    o fluxo completo de criação:
    1. Verificação de idempotência via external_id
    2. Validação via ValidationChain (zero-sum, minor units, limites, policy)
    3. Criação do aggregate via JournalEntryFactory
    4. Persistência atômica via LedgerRepository

    Campos:
        external_id:    Chave de idempotência fornecida pelo caller. Deve ser
                        única por operação de negócio. Submissões duplicadas
                        com o mesmo external_id retornam o lançamento original.
        postings:       Lista de PostingInputs que compõem o lançamento.
                        Deve ter no mínimo 2 postings e somar zero por moeda.
        tenant_id:      Identificador do tenant proprietário da transação.
                        Usado pelo Validation Engine para resolver o PolicyScope
                        e garantir isolamento multi-tenant (Requisito 5.1, 8.2).
                        Padrão: string vazia para backward compatibility.
        policy_context: Dados adicionais visíveis à DSL de policy, separados
                        do metadata geral. A DSL só enxerga policy_context,
                        nunca metadata arbitrário (Requisito 8.2, 8.3).
                        Padrão: dict vazio.
        metadata:       Dados adicionais opcionais (ex: referência de pedido,
                        identificador de usuário, etc.). Padrão: dict vazio.
                        NÃO é visível à DSL de policy.

    Exemplo:
        CreateJournalEntryCommand(
            external_id="order-payment-001",
            postings=[
                PostingInput("acc_available_001", 1050, "BRL", "DEBIT"),
                PostingInput("acc_fees_platform", 1050, "BRL", "CREDIT"),
            ],
            tenant_id="tenantA",
            policy_context={"product_code": "PIX", "channel": "mobile"},
            metadata={"order_id": "order-001"},
        )
    """

    external_id: str
    postings: list[PostingInput]
    # Identificador do tenant — usado para resolução de PolicyScope.
    # Padrão vazio para backward compatibility com código existente.
    tenant_id: str = ""
    # Dados visíveis à DSL de policy — separados do metadata geral.
    # A DSL só enxerga policy_context, nunca metadata arbitrário.
    policy_context: dict = field(default_factory=dict)
    # Metadados opcionais — padrão é dict vazio para evitar mutabilidade compartilhada
    metadata: dict = field(default_factory=dict)


@dataclass
class CreateReversalCommand:
    """
    Comando para criar uma reversão de um lançamento contábil existente.

    A reversão é a única forma de correção permitida no subledger (imutabilidade).
    Cria um novo JournalEntry do tipo REVERSAL com postings inversos ao original,
    anulando o efeito contábil do lançamento original.

    Campos:
        original_entry_id: entry_id do JournalEntry original a ser revertido.
                           O LedgerEngine busca o original pelo ID antes de criar
                           a reversão. Levanta JournalEntryNotFound se não existir.
        external_id:       Chave de idempotência para a operação de reversão.
                           Deve ser diferente do external_id do lançamento original.
        metadata:          Dados adicionais opcionais para a reversão.
                           O LedgerEngine adiciona automaticamente o campo
                           "original_entry_id" ao metadata da reversão.

    Exemplo:
        CreateReversalCommand(
            original_entry_id="550e8400-e29b-41d4-a716-446655440000",
            external_id="reversal-order-payment-001",
            metadata={"reason": "customer_refund"},
        )
    """

    original_entry_id: str
    external_id: str
    # Metadados opcionais — padrão é dict vazio para evitar mutabilidade compartilhada
    metadata: dict = field(default_factory=dict)
