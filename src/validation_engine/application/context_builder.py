"""
CanonicalValidationContextBuilder — construção do contexto canônico de avaliação.

Este módulo é responsável por converter um CreateJournalEntryCommand no
CanonicalValidationContext que a DSL enxerga durante a avaliação.

Toda a lógica de normalização e derivação de fatos fica aqui, não no evaluator.
O evaluator recebe apenas o contexto já construído e o ActivePolicySet.

Responsabilidades:
1. Converter PostingInput → CanonicalPosting (normalização de campos)
2. Calcular DerivedFacts a partir das postings canônicas
3. Isolar policy_context de metadata (a DSL só enxerga policy_context)
4. Incluir context_schema_version para validação de compatibilidade com o bundle

Isolamento policy_context vs metadata (Requisito 8.2):
- policy_context: dados explicitamente fornecidos pelo chamador para consumo pela DSL
- metadata: dados operacionais arbitrários (order_id, trace_id, etc.) — NUNCA visíveis à DSL

Determinismo (Requisito 8.5):
- Para inputs semanticamente equivalentes, o contexto produzido é idêntico.
- Garantido pelo uso de frozen dataclasses, tipos imutáveis e ordenação determinística.

Requisitos cobertos: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from validation_engine.domain.context import (
    CanonicalPosting,
    CanonicalValidationContext,
    DerivedFacts,
)

if TYPE_CHECKING:
    from ledger.application.commands import CreateJournalEntryCommand

# Versão atual do schema do contexto canônico.
# Deve ser incrementada quando campos obrigatórios forem adicionados ou removidos.
# O RuleEvaluator valida esta versão contra a compatibilidade declarada no bundle.
CONTEXT_SCHEMA_VERSION = "1.0"

# Valor de account_type que identifica uma conta de plataforma.
# Contas de plataforma são usadas para taxas, reservas e operações internas.
_PLATFORM_ACCOUNT_TYPE = "PLATFORM"


# ---------------------------------------------------------------------------
# Protocol — contrato público do builder
# ---------------------------------------------------------------------------


class CanonicalValidationContextBuilder(Protocol):
    """
    Protocolo para construção do contexto canônico visível à DSL.

    Toda a lógica de normalização fica aqui, não no evaluator.
    O evaluator recebe apenas o contexto já construído.

    Implementações devem garantir:
    - Determinismo: inputs semanticamente equivalentes → mesmo contexto
    - Isolamento: policy_context e metadata são estritamente separados
    - Completude: DerivedFacts são calculados antes de retornar o contexto

    Requisito: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6
    """

    def build(self, command: "CreateJournalEntryCommand") -> CanonicalValidationContext:
        """
        Constrói o CanonicalValidationContext a partir do comando.

        Args:
            command: Comando de criação de lançamento contábil.

        Returns:
            Contexto canônico imutável pronto para avaliação pela DSL.
        """
        ...


# ---------------------------------------------------------------------------
# Implementação concreta
# ---------------------------------------------------------------------------


class DefaultCanonicalValidationContextBuilder:
    """
    Implementação padrão do CanonicalValidationContextBuilder.

    Converte um CreateJournalEntryCommand em CanonicalValidationContext,
    calculando DerivedFacts e garantindo isolamento entre policy_context
    e metadata.

    Uso:
        builder = DefaultCanonicalValidationContextBuilder()
        context = builder.build(command)

    Notas de design:
    - A classe não mantém estado entre chamadas (stateless).
    - Pode ser instanciada uma vez e reutilizada para múltiplos comandos.
    - Thread-safe por ser stateless.

    Requisito: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6
    """

    def __init__(
        self,
        context_schema_version: str = CONTEXT_SCHEMA_VERSION,
    ) -> None:
        """
        Inicializa o builder com a versão do schema do contexto.

        Args:
            context_schema_version: Versão do schema do contexto canônico.
                Padrão: CONTEXT_SCHEMA_VERSION (constante do módulo).
                Permite injeção para testes com versões específicas.
        """
        # Versão do schema — incluída no contexto para validação de compatibilidade
        self._context_schema_version = context_schema_version

    def build(self, command: "CreateJournalEntryCommand") -> CanonicalValidationContext:
        """
        Constrói o CanonicalValidationContext a partir do comando.

        Pipeline de construção:
        1. Extrai campos de identificação do comando (tenant_id, external_id, etc.)
        2. Converte PostingInputs em CanonicalPostings
        3. Calcula DerivedFacts a partir das postings canônicas
        4. Extrai policy_context (isolado de metadata)
        5. Monta e retorna o CanonicalValidationContext imutável

        Args:
            command: Comando de criação de lançamento contábil.

        Returns:
            Contexto canônico imutável pronto para avaliação pela DSL.
        """
        # Passo 1: Converter postings para representação canônica
        canonical_postings = self._convert_postings(command)

        # Passo 2: Calcular fatos derivados a partir das postings canônicas
        derived_facts = self._calculate_derived_facts(canonical_postings)

        # Passo 3: Extrair policy_context (isolado de metadata)
        # A DSL só enxerga policy_context — metadata nunca é exposto
        policy_context = self._extract_policy_context(command)

        # Passo 4: Extrair campos de identificação do comando
        tenant_id = self._extract_tenant_id(command)
        operation_type = self._extract_operation_type(command)
        product_code = self._extract_product_code(command)
        channel = self._extract_channel(command)

        return CanonicalValidationContext(
            tenant_id=tenant_id,
            external_id=command.external_id,
            operation_type=operation_type,
            product_code=product_code,
            channel=channel,
            postings=canonical_postings,
            policy_context=policy_context,
            facts=derived_facts,
            context_schema_version=self._context_schema_version,
        )

    # ------------------------------------------------------------------
    # Conversão de postings
    # ------------------------------------------------------------------

    def _convert_postings(
        self, command: "CreateJournalEntryCommand"
    ) -> tuple[CanonicalPosting, ...]:
        """
        Converte a lista de PostingInputs em uma tupla de CanonicalPostings.

        A conversão é determinística: a ordem das postings é preservada
        para garantir que inputs semanticamente equivalentes produzam
        o mesmo contexto (Requisito 8.5).

        Campos convertidos:
        - account_id:   copiado diretamente
        - amount:       convertido para int (minor units)
        - currency:     normalizado para maiúsculas (ex: "brl" → "BRL")
        - direction:    normalizado para maiúsculas (ex: "debit" → "DEBIT")
        - account_type: extraído do metadata do posting se disponível, ou None

        Args:
            command: Comando com a lista de PostingInputs.

        Returns:
            Tupla imutável de CanonicalPostings.
        """
        return tuple(
            self._convert_single_posting(posting)
            for posting in command.postings
        )

    def _convert_single_posting(self, posting: object) -> CanonicalPosting:
        """
        Converte um PostingInput individual em CanonicalPosting.

        Normaliza currency e direction para maiúsculas para garantir
        comparações determinísticas na DSL.

        Args:
            posting: PostingInput com os dados brutos do posting.

        Returns:
            CanonicalPosting imutável com campos normalizados.
        """
        # Normaliza currency e direction para maiúsculas
        # Garante que "brl" e "BRL" produzam o mesmo contexto (determinismo)
        currency = str(posting.currency).upper()
        direction = str(posting.direction).upper()

        # Converte amount para int — PostingInput aceita qualquer tipo
        # O MinorUnitsValidator já garantiu que é um int válido antes deste ponto
        amount = int(posting.amount)

        # account_type é opcional — pode não estar presente no PostingInput
        # Verificamos com getattr para compatibilidade com diferentes versões do comando
        account_type = getattr(posting, "account_type", None)

        return CanonicalPosting(
            account_id=posting.account_id,
            amount=amount,
            currency=currency,
            direction=direction,
            account_type=account_type,
        )

    # ------------------------------------------------------------------
    # Cálculo de DerivedFacts
    # ------------------------------------------------------------------

    def _calculate_derived_facts(
        self, postings: tuple[CanonicalPosting, ...]
    ) -> DerivedFacts:
        """
        Calcula os DerivedFacts a partir das postings canônicas.

        Os fatos derivados simplificam a DSL e estabilizam o replay:
        policies não precisam recalcular agregações básicas que seriam
        repetidas em múltiplas rules.

        Invariantes garantidas:
        - posting_count == len(postings)
        - distinct_account_count <= posting_count
        - currencies contém apenas moedas presentes nas postings (ordenadas)
        - total_debits_by_currency soma apenas postings com direction == "DEBIT"
        - total_credits_by_currency soma apenas postings com direction == "CREDIT"
        - max_posting_amount é o maior amount individual entre todas as postings
        - has_platform_account é True se alguma posting tem account_type == "PLATFORM"

        Args:
            postings: Tupla de CanonicalPostings já convertidas.

        Returns:
            DerivedFacts imutável com todos os fatos calculados.
        """
        posting_count = len(postings)

        # Contas distintas — usa set para deduplicação
        distinct_account_count = len({p.account_id for p in postings})

        # Moedas distintas — ordenadas para determinismo (Requisito 8.5)
        currencies = tuple(sorted({p.currency for p in postings}))

        # Totais por moeda separados por direção
        total_debits_by_currency = self._sum_by_currency(postings, direction="DEBIT")
        total_credits_by_currency = self._sum_by_currency(postings, direction="CREDIT")

        # Maior valor individual entre todas as postings
        # Se não há postings, retorna 0 para evitar erro em max() com sequência vazia
        max_posting_amount = max((p.amount for p in postings), default=0)

        # Verifica se alguma posting referencia uma conta de plataforma
        # Uma conta de plataforma tem account_type == "PLATFORM"
        has_platform_account = any(
            p.account_type == _PLATFORM_ACCOUNT_TYPE for p in postings
        )

        return DerivedFacts(
            posting_count=posting_count,
            distinct_account_count=distinct_account_count,
            currencies=currencies,
            total_debits_by_currency=total_debits_by_currency,
            total_credits_by_currency=total_credits_by_currency,
            max_posting_amount=max_posting_amount,
            has_platform_account=has_platform_account,
        )

    def _sum_by_currency(
        self,
        postings: tuple[CanonicalPosting, ...],
        direction: str,
    ) -> Mapping[str, int]:
        """
        Soma os amounts das postings de uma direção específica, agrupados por moeda.

        Retorna um dicionário imutável (dict) com a soma por moeda.
        Apenas moedas com ao menos uma posting na direção especificada aparecem.

        Args:
            postings: Tupla de CanonicalPostings.
            direction: "DEBIT" ou "CREDIT" — direção a filtrar.

        Returns:
            Dicionário {currency: total_amount} para a direção especificada.
        """
        totals: dict[str, int] = {}
        for posting in postings:
            if posting.direction == direction:
                totals[posting.currency] = totals.get(posting.currency, 0) + posting.amount
        return totals

    # ------------------------------------------------------------------
    # Extração de campos do comando
    # ------------------------------------------------------------------

    def _extract_policy_context(
        self, command: "CreateJournalEntryCommand"
    ) -> Mapping[str, str | int | bool]:
        """
        Extrai o policy_context do comando, isolando-o do metadata.

        Isolamento estrito (Requisito 8.2):
        - policy_context: dados explicitamente para consumo pela DSL
        - metadata: dados operacionais arbitrários — NUNCA expostos à DSL

        O campo policy_context pode não existir no comando atual
        (é adicionado na task 11.2). Neste caso, retorna um dict vazio.

        Args:
            command: Comando de criação de lançamento.

        Returns:
            Mapping imutável com os dados de policy_context.
        """
        # Usa getattr para compatibilidade com a versão atual do comando
        # que ainda não tem o campo policy_context (adicionado na task 11.2)
        raw_policy_context = getattr(command, "policy_context", {})

        if not raw_policy_context:
            return {}

        # Filtra apenas tipos permitidos pela DSL: str, int, bool
        # Tipos não permitidos são silenciosamente ignorados para segurança
        return {
            key: value
            for key, value in raw_policy_context.items()
            if isinstance(value, (str, int, bool))
        }

    def _extract_tenant_id(self, command: "CreateJournalEntryCommand") -> str:
        """
        Extrai o tenant_id do comando.

        O campo tenant_id pode não existir no comando atual
        (é adicionado na task 11.2). Neste caso, tenta extrair do metadata
        como fallback, ou retorna uma string vazia.

        Args:
            command: Comando de criação de lançamento.

        Returns:
            Identificador do tenant como string.
        """
        # Tenta o campo direto primeiro (adicionado na task 11.2)
        tenant_id = getattr(command, "tenant_id", None)
        if tenant_id:
            return str(tenant_id)

        # Fallback: tenta extrair do metadata (compatibilidade com versão atual)
        metadata = getattr(command, "metadata", {}) or {}
        tenant_from_metadata = metadata.get("tenant_id", "")
        return str(tenant_from_metadata)

    def _extract_operation_type(self, command: "CreateJournalEntryCommand") -> str:
        """
        Extrai o operation_type do comando.

        O campo operation_type pode não existir no comando atual.
        Tenta extrair do metadata como fallback, ou retorna "UNKNOWN".

        Args:
            command: Comando de criação de lançamento.

        Returns:
            Tipo de operação como string (ex: "TRANSFER", "PAYMENT").
        """
        # Tenta o campo direto primeiro
        operation_type = getattr(command, "operation_type", None)
        if operation_type:
            return str(operation_type).upper()

        # Fallback: tenta extrair do metadata
        metadata = getattr(command, "metadata", {}) or {}
        op_from_metadata = metadata.get("operation_type", "UNKNOWN")
        return str(op_from_metadata).upper()

    def _extract_product_code(self, command: "CreateJournalEntryCommand") -> str | None:
        """
        Extrai o product_code do comando, se disponível.

        Args:
            command: Comando de criação de lançamento.

        Returns:
            Código do produto como string, ou None se não disponível.
        """
        # Tenta o campo direto primeiro
        product_code = getattr(command, "product_code", None)
        if product_code:
            return str(product_code)

        # Fallback: tenta extrair do metadata
        metadata = getattr(command, "metadata", {}) or {}
        return metadata.get("product_code") or None

    def _extract_channel(self, command: "CreateJournalEntryCommand") -> str | None:
        """
        Extrai o channel do comando, se disponível.

        Args:
            command: Comando de criação de lançamento.

        Returns:
            Canal de origem como string, ou None se não disponível.
        """
        # Tenta o campo direto primeiro
        channel = getattr(command, "channel", None)
        if channel:
            return str(channel)

        # Fallback: tenta extrair do metadata
        metadata = getattr(command, "metadata", {}) or {}
        return metadata.get("channel") or None
