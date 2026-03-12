"""
Agregados do domínio do Double-Entry Ledger.

O agregado principal é JournalEntry (Aggregate Root), que protege o invariante
fundamental do subledger: a soma algébrica de todos os postings deve ser zero
para cada moeda envolvida (regra de partidas dobradas / double-entry bookkeeping).

Regras do agregado JournalEntry:
1. Mínimo 2 postings por lançamento (Requisito 1.4)
2. Zero-sum por moeda — sum(signed_amount) == 0 para cada currency (Requisito 1.1)
3. Imutável após criação — append-only, sem UPDATE ou DELETE (Requisito 9.1)
4. Correções via reversão — novo JournalEntry tipo REVERSAL com postings inversos (Requisito 9.2)
5. Cada lançamento gera um OutboxEvent gravado atomicamente (Requisito 7.1)

Requisitos atendidos: 1.1, 1.4, 1.5, 9.1
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ledger.domain.value_objects import EntryType, OutboxEvent, Posting


@dataclass(frozen=True)
class JournalEntry:
    """
    Aggregate Root do subledger de partidas dobradas.

    JournalEntry é o objeto central do domínio. Ele agrupa um conjunto de
    postings (linhas de débito e crédito) e protege o invariante de zero-sum:
    para cada moeda presente nos postings, a soma algébrica dos signed_amounts
    deve ser exatamente zero.

    Imutabilidade (frozen=True):
    JournalEntry é imutável após criação — reflete o princípio de append-only
    do subledger. Nenhum campo pode ser alterado após a instanciação. Correções
    são feitas exclusivamente via reversões (novo JournalEntry tipo REVERSAL).

    Nota sobre o campo metadata (dict em frozen dataclass):
    O Python congela a referência ao dict, não o conteúdo. Por convenção,
    callers não devem mutar o dict após a criação do JournalEntry. Para
    reversões, metadata contém {"original_entry_id": "<entry_id>"}.

    Campos:
        entry_id:     UUID v4 — identificador único do lançamento.
        external_id:  Chave de idempotência fornecida pelo caller. Requisições
                      duplicadas com o mesmo external_id retornam o entry_id
                      original sem criar novo lançamento.
        entry_type:   STANDARD (lançamento normal) ou REVERSAL (anulação).
        postings:     Tuple imutável de Postings. Mínimo 2 postings.
                      Tuple é usado em vez de list para garantir imutabilidade
                      estrutural do agregado.
        metadata:     Dados adicionais do lançamento. Para reversões, contém
                      {"original_entry_id": "<entry_id>"} (Requisito 9.3).
        timestamp:    Timestamp de criação no formato ISO 8601.
        outbox_event: Evento transacional associado, gravado atomicamente
                      junto com o JournalEntry via TransactWriteItems.
    """

    entry_id: str                    # UUID v4 — chave primária do lançamento
    external_id: str                 # chave de idempotência fornecida pelo caller
    entry_type: EntryType            # STANDARD | REVERSAL
    postings: tuple[Posting, ...]    # imutável — tuple, não list (mínimo 2)
    metadata: dict                   # dados adicionais; para reversões: {"original_entry_id": ...}
    timestamp: str                   # timestamp de criação (ISO 8601)
    outbox_event: OutboxEvent        # evento transacional para o Outbox Pattern

    def validate_zero_sum(self) -> bool:
        """
        Verifica o invariante fundamental de partidas dobradas (zero-sum).

        Algoritmo:
        1. Agrupa os postings por moeda (currency)
        2. Para cada moeda, soma os signed_amounts:
           - DEBIT contribui com +amount
           - CREDIT contribui com -amount
        3. Verifica que a soma de cada grupo é exatamente zero

        Exemplo válido (transferência de R$ 100,00):
            Posting(account_id="acc_available", money=Money(10000, "BRL"), direction=DEBIT)
            Posting(account_id="acc_hold",      money=Money(10000, "BRL"), direction=CREDIT)
            → sums_by_currency = {"BRL": 10000 + (-10000)} = {"BRL": 0} → True

        Exemplo inválido (postings desbalanceados):
            Posting(account_id="acc_a", money=Money(10000, "BRL"), direction=DEBIT)
            Posting(account_id="acc_b", money=Money(5000,  "BRL"), direction=CREDIT)
            → sums_by_currency = {"BRL": 10000 + (-5000)} = {"BRL": 5000} → False

        Returns:
            True se todos os grupos de moeda somam zero (lançamento válido).
            False se qualquer moeda apresenta soma diferente de zero.
        """
        # Acumula a soma algébrica dos signed_amounts agrupados por moeda.
        # signed_amount: DEBIT = +amount, CREDIT = -amount (convenção contábil).
        sums_by_currency: dict[str, int] = {}
        for posting in self.postings:
            currency = posting.money.currency
            sums_by_currency[currency] = (
                sums_by_currency.get(currency, 0) + posting.signed_amount
            )

        # O lançamento é válido somente se TODAS as moedas somam zero.
        return all(total == 0 for total in sums_by_currency.values())
