"""
Testes unitários do CanonicalValidationContextBuilder.

Verifica:
- Conversão correta de PostingInput → CanonicalPosting
- Cálculo correto de DerivedFacts
- Isolamento estrito entre policy_context e metadata
- Inclusão de context_schema_version no contexto
- Determinismo para inputs semanticamente equivalentes

Requisitos cobertos: 8.1, 8.2, 8.3, 8.4
"""
from __future__ import annotations

import pytest

from ledger.application.commands import CreateJournalEntryCommand, PostingInput
from validation_engine.application.context_builder import (
    CONTEXT_SCHEMA_VERSION,
    DefaultCanonicalValidationContextBuilder,
)
from validation_engine.domain.context import CanonicalPosting, CanonicalValidationContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_command(
    external_id: str = "ext_test_001",
    postings: list[PostingInput] | None = None,
    metadata: dict | None = None,
) -> CreateJournalEntryCommand:
    """Cria um CreateJournalEntryCommand mínimo para testes."""
    return CreateJournalEntryCommand(
        external_id=external_id,
        postings=postings or [
            PostingInput(account_id="acc_debit", amount=10_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_credit", amount=10_000, currency="BRL", direction="CREDIT"),
        ],
        metadata=metadata or {},
    )


@pytest.fixture
def builder() -> DefaultCanonicalValidationContextBuilder:
    """Instância do builder para uso nos testes."""
    return DefaultCanonicalValidationContextBuilder()


# ---------------------------------------------------------------------------
# 8.1 — Construção do contexto canônico antes da avaliação
# ---------------------------------------------------------------------------


class TestContextBuilding:
    """Testa que o builder constrói um CanonicalValidationContext válido."""

    def test_build_returns_canonical_validation_context(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """O builder deve retornar um CanonicalValidationContext."""
        command = _make_command()
        context = builder.build(command)
        assert isinstance(context, CanonicalValidationContext)

    def test_build_preserves_external_id(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """O external_id do comando deve ser preservado no contexto."""
        command = _make_command(external_id="my-unique-ext-id")
        context = builder.build(command)
        assert context.external_id == "my-unique-ext-id"

    def test_build_includes_context_schema_version(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """O contexto deve incluir context_schema_version (Requisito 8.6)."""
        command = _make_command()
        context = builder.build(command)
        assert context.context_schema_version == CONTEXT_SCHEMA_VERSION

    def test_build_with_custom_schema_version(self) -> None:
        """Builder com versão customizada deve usar a versão fornecida."""
        builder = DefaultCanonicalValidationContextBuilder(context_schema_version="2.0")
        command = _make_command()
        context = builder.build(command)
        assert context.context_schema_version == "2.0"

    def test_context_is_frozen(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """O contexto deve ser imutável (frozen dataclass)."""
        command = _make_command()
        context = builder.build(command)
        with pytest.raises((AttributeError, TypeError)):
            context.external_id = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 8.3 — Conversão de postings para CanonicalPosting
# ---------------------------------------------------------------------------


class TestPostingConversion:
    """Testa a conversão de PostingInput para CanonicalPosting."""

    def test_postings_are_converted_to_canonical_postings(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """Cada PostingInput deve ser convertido em CanonicalPosting."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=5_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=5_000, currency="BRL", direction="CREDIT"),
        ])
        context = builder.build(command)

        assert len(context.postings) == 2
        assert all(isinstance(p, CanonicalPosting) for p in context.postings)

    def test_posting_fields_are_correctly_mapped(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """Campos do PostingInput devem ser mapeados corretamente para CanonicalPosting."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_debit_001", amount=12_345, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_credit_001", amount=12_345, currency="BRL", direction="CREDIT"),
        ])
        context = builder.build(command)

        debit = next(p for p in context.postings if p.direction == "DEBIT")
        assert debit.account_id == "acc_debit_001"
        assert debit.amount == 12_345
        assert debit.currency == "BRL"
        assert debit.direction == "DEBIT"

    def test_currency_is_normalized_to_uppercase(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """Currency deve ser normalizada para maiúsculas."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=1_000, currency="brl", direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=1_000, currency="brl", direction="CREDIT"),
        ])
        context = builder.build(command)
        assert all(p.currency == "BRL" for p in context.postings)

    def test_direction_is_normalized_to_uppercase(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """Direction deve ser normalizada para maiúsculas."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=1_000, currency="BRL", direction="debit"),
            PostingInput(account_id="acc_b", amount=1_000, currency="BRL", direction="credit"),
        ])
        context = builder.build(command)
        directions = {p.direction for p in context.postings}
        assert directions == {"DEBIT", "CREDIT"}

    def test_postings_order_is_preserved(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """A ordem das postings deve ser preservada para determinismo."""
        postings = [
            PostingInput(account_id=f"acc_{i}", amount=i * 100, currency="BRL", direction="DEBIT")
            for i in range(1, 5)
        ]
        # Adiciona um CREDIT para balancear (não é validado aqui, mas é boa prática)
        postings.append(PostingInput(account_id="acc_credit", amount=sum(i * 100 for i in range(1, 5)), currency="BRL", direction="CREDIT"))
        command = _make_command(postings=postings)
        context = builder.build(command)

        # Os primeiros 4 postings devem ser os débitos na ordem original
        for i, posting in enumerate(context.postings[:4]):
            assert posting.account_id == f"acc_{i + 1}"

    def test_postings_is_a_tuple(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """Postings no contexto devem ser uma tupla (imutável)."""
        command = _make_command()
        context = builder.build(command)
        assert isinstance(context.postings, tuple)

    def test_account_type_defaults_to_none_when_not_present(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """account_type deve ser None quando não presente no PostingInput."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=1_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=1_000, currency="BRL", direction="CREDIT"),
        ])
        context = builder.build(command)
        assert all(p.account_type is None for p in context.postings)


# ---------------------------------------------------------------------------
# 8.4 — Cálculo de DerivedFacts
# ---------------------------------------------------------------------------


class TestDerivedFactsCalculation:
    """Testa o cálculo correto de DerivedFacts."""

    def test_posting_count_equals_number_of_postings(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """posting_count deve ser igual ao número de postings."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=1_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=500, currency="BRL", direction="CREDIT"),
            PostingInput(account_id="acc_c", amount=500, currency="BRL", direction="CREDIT"),
        ])
        context = builder.build(command)
        assert context.facts.posting_count == 3

    def test_distinct_account_count_deduplicates_accounts(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """distinct_account_count deve contar contas únicas."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=1_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_a", amount=500, currency="BRL", direction="DEBIT"),  # mesma conta
            PostingInput(account_id="acc_b", amount=1_500, currency="BRL", direction="CREDIT"),
        ])
        context = builder.build(command)
        # acc_a aparece 2 vezes mas conta como 1 conta distinta
        assert context.facts.distinct_account_count == 2

    def test_currencies_contains_distinct_currencies_sorted(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """currencies deve conter moedas distintas em ordem alfabética."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=1_000, currency="USD", direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=1_000, currency="BRL", direction="CREDIT"),
            PostingInput(account_id="acc_c", amount=500, currency="USD", direction="DEBIT"),
            PostingInput(account_id="acc_d", amount=500, currency="BRL", direction="CREDIT"),
        ])
        context = builder.build(command)
        # Deve ser ordenado: BRL antes de USD
        assert context.facts.currencies == ("BRL", "USD")

    def test_total_debits_by_currency_sums_debit_postings(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """total_debits_by_currency deve somar apenas postings DEBIT por moeda."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=3_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=2_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_c", amount=5_000, currency="BRL", direction="CREDIT"),
        ])
        context = builder.build(command)
        assert context.facts.total_debits_by_currency == {"BRL": 5_000}

    def test_total_credits_by_currency_sums_credit_postings(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """total_credits_by_currency deve somar apenas postings CREDIT por moeda."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=5_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=3_000, currency="BRL", direction="CREDIT"),
            PostingInput(account_id="acc_c", amount=2_000, currency="BRL", direction="CREDIT"),
        ])
        context = builder.build(command)
        assert context.facts.total_credits_by_currency == {"BRL": 5_000}

    def test_totals_by_currency_handles_multiple_currencies(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """Totais por moeda devem ser calculados separadamente para cada moeda."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=1_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=500, currency="USD", direction="DEBIT"),
            PostingInput(account_id="acc_c", amount=1_000, currency="BRL", direction="CREDIT"),
            PostingInput(account_id="acc_d", amount=500, currency="USD", direction="CREDIT"),
        ])
        context = builder.build(command)
        assert context.facts.total_debits_by_currency == {"BRL": 1_000, "USD": 500}
        assert context.facts.total_credits_by_currency == {"BRL": 1_000, "USD": 500}

    def test_max_posting_amount_is_largest_individual_amount(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """max_posting_amount deve ser o maior amount individual."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=1_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=50_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_c", amount=51_000, currency="BRL", direction="CREDIT"),
        ])
        context = builder.build(command)
        assert context.facts.max_posting_amount == 51_000

    def test_has_platform_account_false_when_no_platform_accounts(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """has_platform_account deve ser False quando não há contas de plataforma."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=1_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=1_000, currency="BRL", direction="CREDIT"),
        ])
        context = builder.build(command)
        assert context.facts.has_platform_account is False

    def test_total_debits_empty_when_no_debit_postings(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """total_debits_by_currency deve ser vazio quando não há postings DEBIT."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=1_000, currency="BRL", direction="CREDIT"),
            PostingInput(account_id="acc_b", amount=1_000, currency="BRL", direction="CREDIT"),
        ])
        context = builder.build(command)
        assert context.facts.total_debits_by_currency == {}

    def test_derived_facts_is_frozen(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """DerivedFacts deve ser imutável (frozen dataclass)."""
        command = _make_command()
        context = builder.build(command)
        with pytest.raises((AttributeError, TypeError)):
            context.facts.posting_count = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 8.2 — Isolamento entre policy_context e metadata
# ---------------------------------------------------------------------------


class TestPolicyContextIsolation:
    """Testa o isolamento estrito entre policy_context e metadata."""

    def test_metadata_is_not_exposed_in_policy_context(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """
        Dados do metadata NÃO devem aparecer em policy_context.

        Isolamento estrito: a DSL nunca enxerga metadata arbitrário.
        Requisito: 8.2
        """
        command = _make_command(metadata={
            "order_id": "order-001",
            "trace_id": "trace-abc",
            "secret_key": "should-never-be-visible",
        })
        context = builder.build(command)

        # Nenhum dado do metadata deve aparecer em policy_context
        assert "order_id" not in context.policy_context
        assert "trace_id" not in context.policy_context
        assert "secret_key" not in context.policy_context

    def test_policy_context_is_empty_when_command_has_no_policy_context(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """policy_context deve ser vazio quando o comando não tem policy_context."""
        command = _make_command(metadata={"some_metadata": "value"})
        context = builder.build(command)
        assert dict(context.policy_context) == {}

    def test_policy_context_from_command_field_is_used_when_available(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """
        Quando o comando tem campo policy_context, ele deve ser usado.

        Simula o comportamento após a task 11.2 que adiciona policy_context
        ao CreateJournalEntryCommand.
        """
        # Simula um comando com policy_context (como será após task 11.2)
        command = _make_command()
        # Injeta policy_context diretamente no objeto para simular task 11.2
        object.__setattr__(command, "policy_context", {"daily_limit_minor": 500_000, "risk_score": 1})

        context = builder.build(command)
        assert context.policy_context.get("daily_limit_minor") == 500_000
        assert context.policy_context.get("risk_score") == 1

    def test_policy_context_filters_non_allowed_types(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """
        policy_context deve conter apenas tipos permitidos: str, int, bool.

        Tipos não permitidos (list, dict, None) são silenciosamente ignorados.
        """
        command = _make_command()
        # Injeta policy_context com tipos mistos
        object.__setattr__(command, "policy_context", {
            "valid_str": "hello",
            "valid_int": 42,
            "valid_bool": True,
            "invalid_list": [1, 2, 3],
            "invalid_dict": {"nested": "value"},
            "invalid_none": None,
        })

        context = builder.build(command)
        # Apenas tipos permitidos devem aparecer
        assert "valid_str" in context.policy_context
        assert "valid_int" in context.policy_context
        assert "valid_bool" in context.policy_context
        assert "invalid_list" not in context.policy_context
        assert "invalid_dict" not in context.policy_context
        assert "invalid_none" not in context.policy_context

    def test_metadata_and_policy_context_are_completely_independent(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """
        metadata e policy_context devem ser completamente independentes.

        Mesmo que ambos tenham a mesma chave, metadata não contamina policy_context.
        """
        command = _make_command(metadata={"shared_key": "from_metadata"})
        object.__setattr__(command, "policy_context", {"shared_key": "from_policy_context"})

        context = builder.build(command)
        # policy_context deve ter o valor de policy_context, não de metadata
        assert context.policy_context.get("shared_key") == "from_policy_context"


# ---------------------------------------------------------------------------
# Extração de campos de identificação
# ---------------------------------------------------------------------------


class TestIdentificationFieldExtraction:
    """Testa a extração de campos de identificação do comando."""

    def test_tenant_id_extracted_from_metadata_fallback(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """tenant_id deve ser extraído do metadata quando não há campo direto."""
        command = _make_command(metadata={"tenant_id": "tenant_from_metadata"})
        context = builder.build(command)
        assert context.tenant_id == "tenant_from_metadata"

    def test_operation_type_extracted_from_metadata_fallback(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """operation_type deve ser extraído do metadata quando não há campo direto."""
        command = _make_command(metadata={"operation_type": "payment"})
        context = builder.build(command)
        assert context.operation_type == "PAYMENT"  # normalizado para maiúsculas

    def test_operation_type_defaults_to_unknown(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """operation_type deve ser 'UNKNOWN' quando não disponível."""
        command = _make_command(metadata={})
        context = builder.build(command)
        assert context.operation_type == "UNKNOWN"

    def test_product_code_extracted_from_metadata_fallback(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """product_code deve ser extraído do metadata quando não há campo direto."""
        command = _make_command(metadata={"product_code": "PIX"})
        context = builder.build(command)
        assert context.product_code == "PIX"

    def test_product_code_is_none_when_not_available(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """product_code deve ser None quando não disponível."""
        command = _make_command(metadata={})
        context = builder.build(command)
        assert context.product_code is None

    def test_channel_extracted_from_metadata_fallback(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """channel deve ser extraído do metadata quando não há campo direto."""
        command = _make_command(metadata={"channel": "MOBILE"})
        context = builder.build(command)
        assert context.channel == "MOBILE"

    def test_channel_is_none_when_not_available(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """channel deve ser None quando não disponível."""
        command = _make_command(metadata={})
        context = builder.build(command)
        assert context.channel is None

    def test_tenant_id_from_direct_field_takes_precedence(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """Campo direto tenant_id deve ter precedência sobre metadata."""
        command = _make_command(metadata={"tenant_id": "from_metadata"})
        object.__setattr__(command, "tenant_id", "from_direct_field")
        context = builder.build(command)
        assert context.tenant_id == "from_direct_field"


# ---------------------------------------------------------------------------
# Testes de borda
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Testa casos de borda do context builder."""

    def test_single_posting_command(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """Builder deve funcionar com um único posting."""
        command = _make_command(postings=[
            PostingInput(account_id="acc_a", amount=1_000, currency="BRL", direction="DEBIT"),
        ])
        context = builder.build(command)
        assert context.facts.posting_count == 1
        assert context.facts.max_posting_amount == 1_000

    def test_builder_is_stateless_between_calls(
        self, builder: DefaultCanonicalValidationContextBuilder
    ) -> None:
        """Builder deve ser stateless — chamadas independentes não se afetam."""
        command1 = _make_command(external_id="ext_001", postings=[
            PostingInput(account_id="acc_a", amount=1_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_b", amount=1_000, currency="BRL", direction="CREDIT"),
        ])
        command2 = _make_command(external_id="ext_002", postings=[
            PostingInput(account_id="acc_c", amount=5_000, currency="USD", direction="DEBIT"),
            PostingInput(account_id="acc_d", amount=5_000, currency="USD", direction="CREDIT"),
        ])

        context1 = builder.build(command1)
        context2 = builder.build(command2)

        assert context1.external_id == "ext_001"
        assert context2.external_id == "ext_002"
        assert context1.facts.posting_count == 2
        assert context2.facts.posting_count == 2
        assert context1.facts.currencies == ("BRL",)
        assert context2.facts.currencies == ("USD",)
