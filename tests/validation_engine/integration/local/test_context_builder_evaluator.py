"""
Integração local: context builder + evaluator.

Exercita o pipeline completo:
  CreateJournalEntryCommand
    → CanonicalValidationContextBuilder.build()
    → CanonicalValidationContext
    → RuleEvaluator.evaluate(context, active_policy_set)
    → EvaluationResult

Não usa AWS real. Não usa mocks para a lógica de negócio.
O ActivePolicySet é construído diretamente com um bundle compilado via DSLCompiler.

Cobre:
- Pipeline completo: comando → contexto → avaliação
- Isolamento de policy_context vs metadata no pipeline real
- Determinismo end-to-end: mesmo comando → mesmo veredito
- Fatos derivados corretos chegam ao evaluator
- Veredito APPROVED e REJECTED com bundle compilado real

Requisitos cobertos: 8.1, 9.1
"""
from __future__ import annotations

import pytest

from ledger.application.commands import CreateJournalEntryCommand, PostingInput
from validation_engine.application.context_builder import (
    CONTEXT_SCHEMA_VERSION,
    DefaultCanonicalValidationContextBuilder,
)
from validation_engine.domain.compiler import DSLCompiler
from validation_engine.domain.evaluator import EVALUATOR_VERSION, RuleEvaluator
from validation_engine.domain.models import (
    ActivePolicySet,
    BundleCompatibility,
    CompilationMetadata,
    PolicyActivationManifest,
    ReferenceSnapshot,
)
from validation_engine.domain.policy_ast import FinalVerdict

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

_CONTEXT_SCHEMA_VERSION = CONTEXT_SCHEMA_VERSION
_EVALUATOR_VERSION = EVALUATOR_VERSION

_DEFAULT_COMPAT = BundleCompatibility(
    dsl_version="1.0",
    context_schema_version=_CONTEXT_SCHEMA_VERSION,
    snapshot_schema_version="1.0",
    evaluator_min_version=_EVALUATOR_VERSION,
)

_DEFAULT_META = CompilationMetadata(
    author="integration-test",
    description="Context builder + evaluator integration test",
    compiled_at="2024-01-01T00:00:00Z",
    source_hash="sha256:context_builder_integration",
)

# DSL com regras que exercitam os namespaces do contexto canônico:
# - facts.*          (fatos derivados)
# - policy_context.* (dados de contexto do chamador)
# - ref.*            (dados do snapshot)
# - postings.*       (coleção de postings)
_INTEGRATION_DSL = """
POLICY deny_over_daily_limit PRIORITY 100
  WHEN SUM(postings WHERE direction == "DEBIT" SELECT amount) > ref.daily_limit_minor
  THEN DENY "Transaction exceeds daily debit limit"

POLICY deny_blocked_account PRIORITY 90
  WHEN ANY(postings WHERE account_id IN ref.blocked_accounts)
  THEN DENY "Blocked account detected"

POLICY deny_high_risk_channel PRIORITY 80
  WHEN policy_context.channel_risk_score > 5
  THEN DENY "High risk channel"

POLICY allow_standard_brl PRIORITY 10
  WHEN facts.posting_count >= 2
    AND COUNT(postings WHERE currency == "BRL") == facts.posting_count
  THEN ALLOW "Standard BRL flow"
"""

# Snapshot com dados de referência para os testes
_SNAPSHOT = ReferenceSnapshot(
    snapshot_version="snap_context_builder_integration_001",
    snapshot_schema_version="1.0",
    created_at="2024-01-01T00:00:00Z",
    data={
        "daily_limit_minor": 500_000,   # R$ 5.000,00
        "blocked_accounts": ("blocked_acc_001", "blocked_acc_002"),
    },
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_command(
    external_id: str = "ext_integration_001",
    postings: list[PostingInput] | None = None,
    metadata: dict | None = None,
    policy_context: dict | None = None,
) -> CreateJournalEntryCommand:
    """
    Cria um CreateJournalEntryCommand para testes de integração.

    Suporta injeção de policy_context para simular o comportamento
    após a task 11.2 que adiciona o campo ao comando.

    O policy_context padrão inclui channel_risk_score=1 (baixo risco)
    para que a rule deny_high_risk_channel não seja acionada por padrão.
    """
    command = CreateJournalEntryCommand(
        external_id=external_id,
        postings=postings or [
            PostingInput(account_id="acc_debit", amount=10_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_credit", amount=10_000, currency="BRL", direction="CREDIT"),
        ],
        metadata=metadata or {
            "tenant_id": "tenant_integration_test",
            "operation_type": "TRANSFER",
            "product_code": "PIX",
            "channel": "MOBILE",
        },
    )
    # Fornece policy_context padrão com channel_risk_score baixo para evitar
    # que a rule deny_high_risk_channel seja acionada em testes que não a testam.
    # A rule acessa policy_context.channel_risk_score e falha se o campo não existir.
    default_policy_context = {"channel_risk_score": 1}
    if policy_context is not None:
        # Mescla o policy_context fornecido com o padrão (fornecido tem precedência)
        merged = {**default_policy_context, **policy_context}
        object.__setattr__(command, "policy_context", merged)
    else:
        object.__setattr__(command, "policy_context", default_policy_context)
    return command


def _build_active_policy_set(bundle) -> ActivePolicySet:
    """Constrói um ActivePolicySet a partir de um bundle compilado."""
    manifest = PolicyActivationManifest(
        activation_id="act_context_builder_integration_001",
        policy_scope_id="tenant_integration_test:TRANSFER:PIX:MOBILE:prod",
        artifact_hash=bundle.artifact_hash,
        snapshot_version=_SNAPSHOT.snapshot_version,
        context_schema_version=_CONTEXT_SCHEMA_VERSION,
        evaluator_version=_EVALUATOR_VERSION,
        activated_at="2024-01-01T00:00:00Z",
        activated_by="integration-test",
    )
    return ActivePolicySet(
        manifest=manifest,
        bundle=bundle,
        snapshot=_SNAPSHOT,
        loaded_at="2024-01-01T00:00:00Z",
        integrity_verified=True,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def compiled_bundle():
    """Bundle compilado a partir do DSL de integração."""
    compiler = DSLCompiler.create_default()
    return compiler.compile(
        dsl_source=_INTEGRATION_DSL,
        policy_set_id="context_builder_integration_bundle",
        metadata=_DEFAULT_META,
        compatibility=_DEFAULT_COMPAT,
    )


@pytest.fixture(scope="module")
def active_policy_set(compiled_bundle) -> ActivePolicySet:
    """ActivePolicySet construído diretamente com o bundle compilado."""
    return _build_active_policy_set(compiled_bundle)


@pytest.fixture(scope="module")
def builder() -> DefaultCanonicalValidationContextBuilder:
    """Instância do context builder para uso nos testes."""
    return DefaultCanonicalValidationContextBuilder()


@pytest.fixture(scope="module")
def evaluator() -> RuleEvaluator:
    """Instância do evaluator para uso nos testes."""
    return RuleEvaluator()


# ---------------------------------------------------------------------------
# Testes de integração: pipeline completo
# ---------------------------------------------------------------------------


@pytest.mark.integration_local
class TestContextBuilderEvaluatorPipeline:
    """
    Testa o pipeline completo: comando → contexto → avaliação.

    Verifica que o CanonicalValidationContextBuilder e o RuleEvaluator
    funcionam corretamente em conjunto, sem mocks de lógica de negócio.
    """

    def test_pipeline_approves_transaction_within_limit(
        self,
        builder: DefaultCanonicalValidationContextBuilder,
        evaluator: RuleEvaluator,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        Pipeline completo: comando dentro do limite → APPROVED.

        Verifica que o contexto construído pelo builder é corretamente
        avaliado pelo evaluator.
        """
        command = _make_command(postings=[
            PostingInput(account_id="acc_debit", amount=100_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_credit", amount=100_000, currency="BRL", direction="CREDIT"),
        ])

        # Pipeline: comando → contexto → avaliação
        context = builder.build(command)
        result = evaluator.evaluate(context, active_policy_set)

        assert result.decision.final_verdict == FinalVerdict.APPROVED
        assert result.decision.matched_deny_rule is None

    def test_pipeline_rejects_transaction_over_daily_limit(
        self,
        builder: DefaultCanonicalValidationContextBuilder,
        evaluator: RuleEvaluator,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        Pipeline completo: comando acima do limite → REJECTED.

        Verifica que os DerivedFacts calculados pelo builder (total_debits_by_currency)
        são corretamente usados pelo evaluator para aplicar a regra de limite.
        """
        command = _make_command(postings=[
            # 600.000 minor units = R$ 6.000,00 > limite de R$ 5.000,00
            PostingInput(account_id="acc_debit", amount=600_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_credit", amount=600_000, currency="BRL", direction="CREDIT"),
        ])

        context = builder.build(command)
        result = evaluator.evaluate(context, active_policy_set)

        assert result.decision.final_verdict == FinalVerdict.REJECTED
        assert result.decision.matched_deny_rule == "deny_over_daily_limit"

    def test_pipeline_rejects_blocked_account(
        self,
        builder: DefaultCanonicalValidationContextBuilder,
        evaluator: RuleEvaluator,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        Pipeline completo: conta bloqueada → REJECTED.

        Verifica que as postings convertidas pelo builder são corretamente
        avaliadas pelo evaluator contra o snapshot de contas bloqueadas.
        """
        command = _make_command(postings=[
            PostingInput(account_id="blocked_acc_001", amount=10_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_credit", amount=10_000, currency="BRL", direction="CREDIT"),
        ])

        context = builder.build(command)
        result = evaluator.evaluate(context, active_policy_set)

        assert result.decision.final_verdict == FinalVerdict.REJECTED
        assert result.decision.matched_deny_rule == "deny_blocked_account"

    def test_pipeline_rejects_high_risk_channel_from_policy_context(
        self,
        builder: DefaultCanonicalValidationContextBuilder,
        evaluator: RuleEvaluator,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        Pipeline completo: policy_context.channel_risk_score alto → REJECTED.

        Verifica que o policy_context fornecido no comando é corretamente
        isolado e disponibilizado para o evaluator via namespace policy_context.*.
        """
        command = _make_command(
            policy_context={"channel_risk_score": 10},  # > 5 → DENY
        )

        context = builder.build(command)
        result = evaluator.evaluate(context, active_policy_set)

        assert result.decision.final_verdict == FinalVerdict.REJECTED
        assert result.decision.matched_deny_rule == "deny_high_risk_channel"

    def test_pipeline_approves_low_risk_channel_from_policy_context(
        self,
        builder: DefaultCanonicalValidationContextBuilder,
        evaluator: RuleEvaluator,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        Pipeline completo: policy_context.channel_risk_score baixo → APPROVED.

        Verifica que o policy_context é corretamente passado ao evaluator
        e que valores baixos não acionam a regra de alto risco.
        """
        command = _make_command(
            postings=[
                PostingInput(account_id="acc_debit", amount=10_000, currency="BRL", direction="DEBIT"),
                PostingInput(account_id="acc_credit", amount=10_000, currency="BRL", direction="CREDIT"),
            ],
            policy_context={"channel_risk_score": 1},  # <= 5 → não aciona DENY
        )

        context = builder.build(command)
        result = evaluator.evaluate(context, active_policy_set)

        assert result.decision.final_verdict == FinalVerdict.APPROVED

    def test_metadata_does_not_affect_evaluation(
        self,
        builder: DefaultCanonicalValidationContextBuilder,
        evaluator: RuleEvaluator,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        Metadata do comando não deve vazar para policy_context.

        Verifica o isolamento: mesmo que metadata contenha dados que pareçam
        relevantes para a policy, eles não chegam ao evaluator via policy_context.

        Nota: o policy_context padrão inclui channel_risk_score=1 para que
        a rule deny_high_risk_channel não seja acionada (o evaluator requer
        que o campo exista em policy_context quando a rule o referencia).
        """
        # Comando com metadata que parece relevante mas não deve vazar para policy_context
        # O _make_command injeta policy_context padrão com channel_risk_score=1
        command_with_metadata = _make_command(
            metadata={
                "tenant_id": "tenant_integration_test",
                "operation_type": "TRANSFER",
                # Estes campos estão em metadata — não devem aparecer em policy_context
                "extra_metadata_key": "should_not_leak",
                "another_metadata_key": "also_should_not_leak",
            }
        )

        context = builder.build(command_with_metadata)

        # Chaves de metadata não devem aparecer em policy_context
        assert "extra_metadata_key" not in context.policy_context
        assert "another_metadata_key" not in context.policy_context

        # A avaliação deve ser APPROVED (policy_context tem apenas channel_risk_score=1)
        result = evaluator.evaluate(context, active_policy_set)
        assert result.decision.final_verdict == FinalVerdict.APPROVED

    def test_derived_facts_are_correctly_used_in_evaluation(
        self,
        builder: DefaultCanonicalValidationContextBuilder,
        evaluator: RuleEvaluator,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        DerivedFacts calculados pelo builder são corretamente usados pelo evaluator.

        Verifica que facts.posting_count e COUNT(postings WHERE currency == "BRL")
        são consistentes — a rule allow_standard_brl usa ambos.
        """
        command = _make_command(postings=[
            PostingInput(account_id="acc_debit", amount=10_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_credit", amount=10_000, currency="BRL", direction="CREDIT"),
        ])

        context = builder.build(command)

        # Verifica que os fatos derivados estão corretos
        assert context.facts.posting_count == 2
        assert context.facts.currencies == ("BRL",)
        assert context.facts.total_debits_by_currency == {"BRL": 10_000}
        assert context.facts.total_credits_by_currency == {"BRL": 10_000}

        # A avaliação deve usar esses fatos corretamente
        result = evaluator.evaluate(context, active_policy_set)
        assert result.decision.final_verdict == FinalVerdict.APPROVED

    def test_pipeline_is_deterministic_for_same_command(
        self,
        builder: DefaultCanonicalValidationContextBuilder,
        evaluator: RuleEvaluator,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        Pipeline completo é determinístico: mesmo comando → mesmo veredito.

        Verifica que o pipeline builder + evaluator é determinístico
        para o mesmo comando, independentemente de quantas vezes é executado.
        """
        command = _make_command(postings=[
            PostingInput(account_id="acc_debit", amount=200_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_credit", amount=200_000, currency="BRL", direction="CREDIT"),
        ])

        # Executa o pipeline 3 vezes com o mesmo comando
        results = []
        for _ in range(3):
            context = builder.build(command)
            result = evaluator.evaluate(context, active_policy_set)
            results.append(result.decision.final_verdict)

        # Todos os vereditos devem ser iguais
        assert len(set(results)) == 1

    def test_context_schema_version_is_compatible_with_bundle(
        self,
        builder: DefaultCanonicalValidationContextBuilder,
        evaluator: RuleEvaluator,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        context_schema_version do contexto deve ser compatível com o bundle.

        Verifica que o builder inclui a versão correta do schema e que
        o evaluator aceita o contexto sem erro de incompatibilidade.
        """
        command = _make_command()
        context = builder.build(command)

        # Verifica que a versão do schema é a esperada pelo bundle
        assert context.context_schema_version == _CONTEXT_SCHEMA_VERSION
        assert context.context_schema_version == active_policy_set.bundle.compatibility.context_schema_version

        # A avaliação deve funcionar sem erro de incompatibilidade
        result = evaluator.evaluate(context, active_policy_set)
        assert result is not None

    def test_all_rules_are_evaluated_in_pipeline(
        self,
        builder: DefaultCanonicalValidationContextBuilder,
        evaluator: RuleEvaluator,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        Todas as rules do bundle são avaliadas no pipeline completo.

        Verifica que o evaluator avalia todas as 4 rules do bundle
        e registra os resultados no EvaluationResult.
        """
        command = _make_command(postings=[
            PostingInput(account_id="acc_debit", amount=10_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_credit", amount=10_000, currency="BRL", direction="CREDIT"),
        ])

        context = builder.build(command)
        result = evaluator.evaluate(context, active_policy_set)

        # O bundle tem 4 rules
        assert result.metrics.evaluated_rules == 4
        rule_names = {r.rule_name for r in result.decision.rules}
        assert "deny_over_daily_limit" in rule_names
        assert "deny_blocked_account" in rule_names
        assert "deny_high_risk_channel" in rule_names
        assert "allow_standard_brl" in rule_names

    def test_deny_overrides_allow_in_full_pipeline(
        self,
        builder: DefaultCanonicalValidationContextBuilder,
        evaluator: RuleEvaluator,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        DENY_OVERRIDES no pipeline completo: DENY prevalece sobre ALLOW.

        Mesmo que allow_standard_brl case (BRL, 2 postings), um DENY
        por conta bloqueada deve prevalecer.
        """
        command = _make_command(postings=[
            # Conta bloqueada mas dentro do limite e em BRL → DENY prevalece
            PostingInput(account_id="blocked_acc_002", amount=50_000, currency="BRL", direction="DEBIT"),
            PostingInput(account_id="acc_credit", amount=50_000, currency="BRL", direction="CREDIT"),
        ])

        context = builder.build(command)
        result = evaluator.evaluate(context, active_policy_set)

        assert result.decision.final_verdict == FinalVerdict.REJECTED
        assert result.decision.matched_deny_rule == "deny_blocked_account"

    def test_currency_normalization_affects_evaluation(
        self,
        builder: DefaultCanonicalValidationContextBuilder,
        evaluator: RuleEvaluator,
        active_policy_set: ActivePolicySet,
    ) -> None:
        """
        Normalização de currency pelo builder afeta a avaliação corretamente.

        Postings com currency em minúsculas devem ser normalizadas para
        maiúsculas pelo builder, garantindo que a DSL funcione corretamente.
        """
        # Postings com currency em minúsculas
        command = _make_command(postings=[
            PostingInput(account_id="acc_debit", amount=10_000, currency="brl", direction="debit"),
            PostingInput(account_id="acc_credit", amount=10_000, currency="brl", direction="credit"),
        ])

        context = builder.build(command)

        # Verifica que a normalização ocorreu
        assert all(p.currency == "BRL" for p in context.postings)
        assert all(p.direction in ("DEBIT", "CREDIT") for p in context.postings)

        # A avaliação deve funcionar corretamente com os valores normalizados
        result = evaluator.evaluate(context, active_policy_set)
        assert result.decision.final_verdict == FinalVerdict.APPROVED
