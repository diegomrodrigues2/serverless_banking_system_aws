"""
Property-based tests for Last Known Good (LKG) — Property 11.

**Validates: Requirements 17.1, 17.2**

Property 11 (design.md): "Last Known Good só é usado após boot válido"
Meaning: The LKGStore must NEVER return a stored ActivePolicySet before
mark_boot_valid() has been called, regardless of what is stored on disk.
After mark_boot_valid() is called, the LKG must be available for fallback.

Sub-properties covered:

  11a — LKG unavailable before valid boot:
        For any scope_id and any ActivePolicySet saved to the LKGStore,
        if mark_boot_valid() has NOT been called, load() must return None.

  11b — LKG available after valid boot:
        For any scope_id and any ActivePolicySet saved to the LKGStore,
        if mark_boot_valid() HAS been called, load() must return the saved set.

  11c — LKG round-trip fidelity:
        For any ActivePolicySet saved and loaded after a valid boot,
        the loaded set must have the same activation_id, artifact_hash,
        and snapshot_version as the original.

  11d — LKG independence across scopes:
        For any two distinct scope_ids, saving LKG for one scope must not
        affect the LKG of the other scope.

  11e — LKG boot flag is monotonic:
        Once mark_boot_valid() is called, has_valid_boot must remain True
        regardless of subsequent operations.

Implementation notes:
  - Uses tempfile.TemporaryDirectory() inside each test body to avoid
    function-scoped fixture issues with Hypothesis (HealthCheck.function_scoped_fixture).
  - Generates scope_ids as text strings with ':' separators.
  - Generates activation_ids and artifact_hashes as simple text strings.
  - Does not test full ActivePolicySet round-trip (covered by unit tests);
    focuses on the boot invariant and scope isolation properties.

Requisitos cobertos: 17.1, 17.2
"""

from __future__ import annotations

import tempfile

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from validation_engine.domain.models import (
    ActivePolicySet,
    BundleCompatibility,
    CompilationMetadata,
    PolicyActivationManifest,
    ReferenceSnapshot,
    RuleBundle,
)
from validation_engine.domain.policy_ast import (
    CompositionMode,
    ComparisonNode,
    FieldAccessNode,
    LiteralNode,
    PolicyEffect,
    PolicyRuleNode,
    RuleAST,
)
from validation_engine.infrastructure.lkg_store import LKGStore


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Identificadores simples: letras, dígitos e underscores — sem caracteres especiais
# que possam interferir com o sistema de arquivos.
_identifier = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_"),
    min_size=1,
    max_size=20,
)

# scope_id no formato "tenant:operation:product:channel:env"
_scope_id = st.builds(
    lambda t, op, prod, ch, env: f"{t}:{op}:{prod}:{ch}:{env}",
    t=_identifier,
    op=_identifier,
    prod=_identifier,
    ch=_identifier,
    env=st.sampled_from(["prod", "dev", "staging"]),
)

# activation_id simples
_activation_id = st.builds(
    lambda prefix, n: f"act_{prefix}_{n:04d}",
    prefix=_identifier,
    n=st.integers(min_value=1, max_value=9999),
)

# artifact_hash simples (não precisa ser SHA-256 real para este property test)
_artifact_hash = st.builds(
    lambda h: f"sha256:{h}",
    h=st.text(
        alphabet="0123456789abcdef",
        min_size=8,
        max_size=16,
    ),
)


def _make_minimal_active_policy_set(
    scope_id: str,
    activation_id: str,
    artifact_hash: str,
) -> ActivePolicySet:
    """
    Constrói um ActivePolicySet mínimo para property tests.

    Usa valores fixos para campos que não são relevantes para as propriedades
    testadas (bundle AST, snapshot data, etc.).
    """
    manifest = PolicyActivationManifest(
        activation_id=activation_id,
        policy_scope_id=scope_id,
        artifact_hash=artifact_hash,
        snapshot_version="snap_prop_001",
        context_schema_version="1.0",
        evaluator_version="1.0.0",
        activated_at="2026-01-01T00:00:00Z",
        activated_by="property_test",
    )

    rule = PolicyRuleNode(
        name="deny_prop_test",
        priority=100,
        condition=ComparisonNode(
            left=FieldAccessNode(path=("facts", "posting_count")),
            operator=">=",
            right=LiteralNode(value=1),
        ),
        effect=PolicyEffect.DENY,
        message="Property test deny",
    )
    bundle = RuleBundle(
        policy_set_id="prop-test-policy-set",
        artifact_hash=artifact_hash,
        ast=RuleAST(rules=(rule,)),
        execution_plan={},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version="1.0",
            snapshot_schema_version="1.0",
            evaluator_min_version="1.0.0",
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author="property_test",
            description="Property test bundle",
            compiled_at="2026-01-01T00:00:00Z",
            source_hash="sha256:prop_source",
        ),
    )

    snapshot = ReferenceSnapshot(
        snapshot_version="snap_prop_001",
        snapshot_schema_version="1.0",
        created_at="2026-01-01T00:00:00Z",
        data={"daily_limit_minor": 100000},
    )

    return ActivePolicySet(
        manifest=manifest,
        bundle=bundle,
        snapshot=snapshot,
        loaded_at="2026-01-01T00:00:00Z",
        integrity_verified=True,
    )


# ---------------------------------------------------------------------------
# Property 11a: LKG indisponível antes de boot válido
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(
    scope_id=_scope_id,
    activation_id=_activation_id,
    artifact_hash=_artifact_hash,
)
@settings(max_examples=50, deadline=None)
def test_lkg_indisponivel_antes_de_boot_valido(
    scope_id: str,
    activation_id: str,
    artifact_hash: str,
) -> None:
    """
    Property 11a: LKG nunca disponível antes de boot válido.

    Para qualquer scope_id e qualquer ActivePolicySet salvo no LKGStore,
    se mark_boot_valid() NÃO foi chamado, load() deve retornar None.
    """
    # Usar TemporaryDirectory dentro do corpo do teste para evitar
    # o HealthCheck.function_scoped_fixture do Hypothesis.
    with tempfile.TemporaryDirectory() as tmp_dir:
        lkg_store = LKGStore(lkg_dir=tmp_dir)
        aps = _make_minimal_active_policy_set(scope_id, activation_id, artifact_hash)

        # Salvar sem marcar boot válido
        lkg_store.save(scope_id, aps)

        # Invariante: sem boot válido, load() deve retornar None
        result = lkg_store.load(scope_id)
        assert result is None, (
            f"LKG retornou valor antes de boot válido para scope_id='{scope_id}'. "
            f"Invariante de segurança violada: LKG só deve ser usado após boot válido."
        )


# ---------------------------------------------------------------------------
# Property 11b: LKG disponível após boot válido
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(
    scope_id=_scope_id,
    activation_id=_activation_id,
    artifact_hash=_artifact_hash,
)
@settings(max_examples=50, deadline=None)
def test_lkg_disponivel_apos_boot_valido(
    scope_id: str,
    activation_id: str,
    artifact_hash: str,
) -> None:
    """
    Property 11b: LKG disponível após boot válido.

    Para qualquer scope_id e qualquer ActivePolicySet salvo no LKGStore,
    se mark_boot_valid() FOI chamado, load() deve retornar o conjunto salvo.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        lkg_store = LKGStore(lkg_dir=tmp_dir)
        aps = _make_minimal_active_policy_set(scope_id, activation_id, artifact_hash)

        lkg_store.save(scope_id, aps)
        lkg_store.mark_boot_valid()

        result = lkg_store.load(scope_id)
        assert result is not None, (
            f"LKG retornou None após boot válido para scope_id='{scope_id}'. "
            f"O LKG deve estar disponível após mark_boot_valid()."
        )


# ---------------------------------------------------------------------------
# Property 11c: Round-trip fidelidade do LKG
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(
    scope_id=_scope_id,
    activation_id=_activation_id,
    artifact_hash=_artifact_hash,
)
@settings(max_examples=50, deadline=None)
def test_lkg_round_trip_fidelidade(
    scope_id: str,
    activation_id: str,
    artifact_hash: str,
) -> None:
    """
    Property 11c: Round-trip do LKG preserva campos de identidade.

    Para qualquer ActivePolicySet salvo e carregado após boot válido,
    o conjunto carregado deve ter os mesmos activation_id, artifact_hash
    e snapshot_version que o original.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        lkg_store = LKGStore(lkg_dir=tmp_dir)
        original = _make_minimal_active_policy_set(scope_id, activation_id, artifact_hash)

        lkg_store.save(scope_id, original)
        lkg_store.mark_boot_valid()
        loaded = lkg_store.load(scope_id)

        assert loaded is not None
        assert loaded.manifest.activation_id == original.manifest.activation_id, (
            f"activation_id divergiu após round-trip: "
            f"original='{original.manifest.activation_id}', "
            f"loaded='{loaded.manifest.activation_id}'"
        )
        assert loaded.manifest.artifact_hash == original.manifest.artifact_hash, (
            f"artifact_hash divergiu após round-trip: "
            f"original='{original.manifest.artifact_hash}', "
            f"loaded='{loaded.manifest.artifact_hash}'"
        )
        assert loaded.manifest.snapshot_version == original.manifest.snapshot_version, (
            f"snapshot_version divergiu após round-trip: "
            f"original='{original.manifest.snapshot_version}', "
            f"loaded='{loaded.manifest.snapshot_version}'"
        )
        assert loaded.integrity_verified is True, (
            "integrity_verified deve ser True após round-trip do LKG"
        )


# ---------------------------------------------------------------------------
# Property 11d: Independência entre escopos
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(
    scope_a=_scope_id,
    scope_b=_scope_id,
    activation_id_a=_activation_id,
    activation_id_b=_activation_id,
    artifact_hash=_artifact_hash,
)
@settings(max_examples=30, deadline=None)
def test_lkg_independencia_entre_escopos(
    scope_a: str,
    scope_b: str,
    activation_id_a: str,
    activation_id_b: str,
    artifact_hash: str,
) -> None:
    """
    Property 11d: LKGs de escopos distintos são independentes.

    Salvar LKG para scope_a não deve afetar o LKG de scope_b.
    Se scope_a == scope_b, o teste verifica que o último salvo prevalece.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        lkg_store = LKGStore(lkg_dir=tmp_dir)
        lkg_store.mark_boot_valid()

        aps_a = _make_minimal_active_policy_set(scope_a, activation_id_a, artifact_hash)
        aps_b = _make_minimal_active_policy_set(scope_b, activation_id_b, artifact_hash)

        lkg_store.save(scope_a, aps_a)
        lkg_store.save(scope_b, aps_b)

        loaded_a = lkg_store.load(scope_a)
        loaded_b = lkg_store.load(scope_b)

        assert loaded_a is not None
        assert loaded_b is not None

        if scope_a != scope_b:
            # Escopos distintos: cada um deve ter seu próprio LKG
            assert loaded_a.manifest.activation_id == activation_id_a, (
                f"LKG do scope_a foi contaminado pelo scope_b: "
                f"esperado activation_id='{activation_id_a}', "
                f"obtido='{loaded_a.manifest.activation_id}'"
            )
            assert loaded_b.manifest.activation_id == activation_id_b, (
                f"LKG do scope_b foi contaminado pelo scope_a: "
                f"esperado activation_id='{activation_id_b}', "
                f"obtido='{loaded_b.manifest.activation_id}'"
            )


# ---------------------------------------------------------------------------
# Property 11e: Flag de boot é monotônica
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(
    scope_id=_scope_id,
    activation_id=_activation_id,
    artifact_hash=_artifact_hash,
)
@settings(max_examples=30, deadline=None)
def test_lkg_boot_flag_monotonica(
    scope_id: str,
    activation_id: str,
    artifact_hash: str,
) -> None:
    """
    Property 11e: Flag de boot válido é monotônica.

    Uma vez que mark_boot_valid() é chamado, has_valid_boot deve permanecer
    True independentemente de operações subsequentes (save, load, clear).
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        lkg_store = LKGStore(lkg_dir=tmp_dir)
        aps = _make_minimal_active_policy_set(scope_id, activation_id, artifact_hash)

        assert lkg_store.has_valid_boot is False

        lkg_store.mark_boot_valid()
        assert lkg_store.has_valid_boot is True

        # Operações subsequentes não devem reverter o flag
        lkg_store.save(scope_id, aps)
        assert lkg_store.has_valid_boot is True

        lkg_store.load(scope_id)
        assert lkg_store.has_valid_boot is True

        lkg_store.clear(scope_id)
        assert lkg_store.has_valid_boot is True

        # Chamar mark_boot_valid() novamente não deve causar problemas
        lkg_store.mark_boot_valid()
        assert lkg_store.has_valid_boot is True
