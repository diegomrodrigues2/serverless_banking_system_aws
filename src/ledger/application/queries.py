"""
Queries da camada de aplicação do Double-Entry Ledger.

Queries representam intenções de leitura no sistema (Read Path / CQRS).
São DTOs imutáveis que transportam os parâmetros necessários para que o
QueryHandler execute uma operação de leitura via LedgerRepository.

Componentes:
- GetBalanceQuery: consulta de saldo materializado de uma conta
- GetStatementQuery: consulta de extrato paginado de uma conta

Estas queries são criadas pela camada de API (read_handler.py) a partir
dos parâmetros da requisição HTTP e passadas para o QueryHandler.

Requisitos atendidos: 4.1, 8.1, 8.2
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GetBalanceQuery:
    """
    Query para consultar o saldo materializado de uma conta em uma moeda.

    O Read Path retorna a projeção materializada do saldo, atualizada
    atomicamente pelo Write Path a cada lançamento. A consulta é O(1)
    via GetItem no DynamoDB (Requisito 8.1).

    Consistência eventual: o saldo pode estar defasado em até ~1 segundo
    em relação ao último lançamento (Requisito 8.3).

    Campos:
        account_id: Identificador da conta a ser consultada.
        currency:   Código ISO 4217 da moeda (ex: "BRL", "USD").

    Exemplo:
        GetBalanceQuery(account_id="acc_available_001", currency="BRL")
    """

    account_id: str
    currency: str


@dataclass
class GetStatementQuery:
    """
    Query para consultar o extrato paginado de uma conta.

    Retorna os Postings da conta ordenados cronologicamente pelo
    posting_sort_key (formato: "POSTING#{timestamp}#{entry_id}#{index}").
    A paginação é baseada em cursor (não offset), o que é eficiente
    no DynamoDB e não sofre com o problema de "drift" de páginas (Requisito 8.2).

    Campos:
        account_id: Identificador da conta a ser consultada.
        cursor:     Cursor da página anterior (posting_sort_key do último item).
                    None para a primeira página.
        page_size:  Número máximo de postings por página. Padrão: 20.

    Exemplo (primeira página):
        GetStatementQuery(account_id="acc_available_001", cursor=None, page_size=20)

    Exemplo (página seguinte):
        GetStatementQuery(
            account_id="acc_available_001",
            cursor="POSTING#2026-03-10T14:30:00Z#uuid-abc#0",
            page_size=20,
        )
    """

    account_id: str
    # None indica primeira página; valor não-None é o posting_sort_key do último item da página anterior
    cursor: str | None = None
    # Número máximo de postings por página — padrão razoável para a maioria dos casos
    page_size: int = 20
