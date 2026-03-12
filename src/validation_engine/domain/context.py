"""
Contexto canônico de avaliação do Validation Engine.

Este módulo define a representação canônica, tipada e estável do comando
que é visível à DSL durante a avaliação. A DSL nunca enxerga o comando
bruto da API — ela opera exclusivamente sobre este contexto.

Motivação para o contexto canônico:
- Determinismo: inputs semanticamente equivalentes produzem o mesmo contexto.
- Replay: o contexto pode ser persistido e reutilizado para reproduzir decisões.
- Isolamento: a DSL não acessa metadata arbitrário, apenas namespaces explícitos.
- Compatibilidade: context_schema_version permite evolução controlada do schema.

Namespaces disponíveis para a DSL (Requisito 23.5):
- postings.*        → tuple de CanonicalPosting
- facts.*           → DerivedFacts calculados antes da avaliação
- policy_context.*  → dados de contexto fornecidos pelo chamador da API
- ref.*             → dados do ReferenceSnapshot (via RefAccessNode)

Requisitos cobertos: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class CanonicalPosting:
    """
    Representação canônica de uma partida (posting) visível à DSL.

    Contém apenas os campos necessários para avaliação de policies.
    Campos internos do ledger (como entry_id, version, etc.) são
    deliberadamente excluídos para manter o contexto estável e replayable.

    Campos:
    - account_id:   identificador da conta (string opaca para a DSL)
    - amount:       valor em minor units (centavos, satoshis, etc.)
    - currency:     código ISO 4217 da moeda (ex: "BRL", "USD")
    - direction:    "DEBIT" ou "CREDIT"
    - account_type: tipo da conta, opcional (ex: "ASSET", "LIABILITY")

    Requisito: 8.1, 8.3
    """

    account_id: str
    # Valor em minor units — sempre inteiro para evitar aritmética de ponto flutuante
    amount: int
    # Código ISO 4217 da moeda
    currency: str
    # Direção da partida: "DEBIT" ou "CREDIT"
    direction: str
    # Tipo da conta — opcional, pode ser None se não disponível
    account_type: str | None = None


@dataclass(frozen=True)
class DerivedFacts:
    """
    Fatos derivados calculados a partir do comando canônico antes da avaliação.

    DerivedFacts serve dois propósitos:
    1. Simplificar a DSL: evita que policies precisem recalcular agregações
       básicas que seriam repetidas em múltiplas rules.
    2. Estabilizar replay: os fatos são calculados deterministicamente a partir
       do contexto canônico, garantindo reprodutibilidade.

    Todos os campos são calculados pelo CanonicalValidationContextBuilder
    antes da avaliação e disponibilizados no namespace "facts.*" da DSL.

    Invariantes:
    - posting_count == len(postings) no contexto pai
    - distinct_account_count <= posting_count
    - currencies contém apenas moedas presentes nas postings
    - total_debits_by_currency e total_credits_by_currency somam apenas
      postings com direction "DEBIT" e "CREDIT" respectivamente

    Requisito: 8.4
    """

    # Número total de postings no comando
    posting_count: int
    # Número de contas distintas referenciadas nas postings
    distinct_account_count: int
    # Tupla de moedas distintas presentes nas postings (ordenada para determinismo)
    currencies: tuple[str, ...]
    # Soma dos débitos por moeda: {"BRL": 10000, "USD": 5000}
    total_debits_by_currency: Mapping[str, int]
    # Soma dos créditos por moeda: {"BRL": 10000, "USD": 5000}
    total_credits_by_currency: Mapping[str, int]
    # Maior valor individual entre todas as postings (em minor units)
    max_posting_amount: int
    # True se alguma posting referencia uma conta de plataforma
    # (determinado pelo account_type == "PLATFORM" ou convenção equivalente)
    has_platform_account: bool


@dataclass(frozen=True)
class CanonicalValidationContext:
    """
    Contexto canônico completo visível à DSL durante a avaliação.

    Este é o único input de dados que o RuleEvaluator recebe além do
    ActivePolicySet. A DSL não tem acesso a nenhuma outra fonte de dados.

    Namespaces disponíveis para a DSL:
    - postings:       tuple de CanonicalPosting (namespace "postings.*")
    - policy_context: dados fornecidos pelo chamador (namespace "policy_context.*")
    - facts:          DerivedFacts calculados (namespace "facts.*")
    - ref.*:          dados do ReferenceSnapshot (resolvido pelo evaluator via ActivePolicySet)

    Campos de identificação (não acessíveis pela DSL, usados para auditoria):
    - tenant_id:      identificador do tenant
    - external_id:    identificador externo da transação
    - operation_type: tipo de operação (ex: "TRANSFER", "PAYMENT")
    - product_code:   código do produto, opcional
    - channel:        canal de origem, opcional

    Versionamento:
    - context_schema_version: versão do schema deste contexto.
      Validada contra a compatibilidade declarada no RuleBundle antes da avaliação.
      Permite evolução controlada do contexto sem quebrar bundles existentes.

    Determinismo (Requisito 8.5):
    Para inputs semanticamente equivalentes (mesmas postings, mesmo policy_context,
    mesmos campos de identificação), o contexto produzido deve ser idêntico.
    Isso é garantido pelo uso de frozen dataclasses e tipos imutáveis.

    Requisito: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6
    """

    # Identificação da transação — usada para auditoria, não acessível pela DSL
    tenant_id: str
    external_id: str
    operation_type: str
    product_code: str | None
    channel: str | None

    # Partidas da transação — acessíveis via namespace "postings.*"
    postings: tuple[CanonicalPosting, ...]

    # Dados de contexto fornecidos pelo chamador — acessíveis via "policy_context.*"
    # Tipos permitidos: str, int, bool (sem objetos aninhados para manter a DSL simples)
    policy_context: Mapping[str, str | int | bool]

    # Fatos derivados calculados antes da avaliação — acessíveis via "facts.*"
    facts: DerivedFacts

    # Versão do schema deste contexto — validada contra compatibilidade do bundle
    # Formato sugerido: "1.0", "1.1", "2.0" (semver simplificado)
    context_schema_version: str
