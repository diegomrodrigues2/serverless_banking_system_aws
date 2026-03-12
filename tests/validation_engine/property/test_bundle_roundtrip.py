"""
Property-based tests for RuleBundle round-trip serialization.

Validates: Requirements 3.3, 24.2

Property 13 (design.md): Serializing a RuleBundle to JSON and deserializing it
back must produce a structurally equivalent bundle. The artifact_hash must be
preserved across the round-trip.

These tests use Hypothesis to generate arbitrary valid RuleBundle instances and
verify that the serialization/deserialization cycle is lossless — no field is
dropped, coerced, or mutated during the round-trip.

Covered requirements:
- Req 3.3: artifact_hash must be preserved exactly across the round-trip.
- Req 24.2: BundleCompatibility fields (context_schema_version,
  snapshot_schema_version, evaluator_min_version) must be preserved.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
    RuleBundle,
)
from validation_engine.domain.policy_ast import (
    AggregateNode,
    CollectionRefNode,
    CompositionMode,
    ComparisonNode,
    FieldAccessNode,
    LiteralNode,
    LogicalOpNode,
    NotOpNode,
    PolicyEffect,
    PolicyRuleNode,
    PredicateNode,
    RefAccessNode,
    RuleAST,
)


# ---------------------------------------------------------------------------
# Hypothesis strategies for AST node generation
# ---------------------------------------------------------------------------

# Identifiers used in the DSL — kept short and ASCII-safe to avoid JSON
# encoding edge cases that are unrelated to the round-trip property.
_identifier = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_"),
    min_size=1,
    max_size=20,
)

# Literal values supported by LiteralNode: int, float, str, bool.
# We exclude NaN and Infinity because JSON does not support them, which would
# cause a serialization error unrelated to the round-trip property itself.
_literal_value = st.one_of(
    st.integers(min_value=-(2**31), max_value=2**31),
    st.floats(allow_nan=False, allow_infinity=False, min_value=-1e15, max_value=1e15),
    st.text(alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd", "Zs"), whitelist_characters="_-.:"), max_size=50),
    st.booleans(),
)

# Leaf node strategies — these do not recurse.
_literal_node = _literal_value.map(LiteralNode)

_field_access_node = st.lists(
    _identifier, min_size=1, max_size=3
).map(lambda parts: FieldAccessNode(path=tuple(parts)))

_ref_access_node = st.lists(
    _identifier, min_size=1, max_size=2
).map(lambda parts: RefAccessNode(path=tuple(parts)))

_collection_ref_node = st.just(CollectionRefNode(name="postings"))

# Leaf union — used as the base for recursive strategies.
_leaf_node = st.one_of(_literal_node, _field_access_node, _ref_access_node)

# Comparison operators supported by the DSL (Req 23.1).
_comparison_operator = st.sampled_from(["==", "!=", "<", "<=", ">", ">=", "IN"])

# Logical operators supported by the DSL (Req 23.2).
_logical_operator = st.sampled_from(["AND", "OR"])

# Aggregate functions supported by the DSL (Req 23.3).
_aggregate_function = st.sampled_from(["SUM", "COUNT", "MIN", "MAX", "ANY", "ALL"])


def _build_ast_node_strategy(max_depth: int = 2) -> st.SearchStrategy:
    """
    Build a recursive Hypothesis strategy for ASTNode generation.

    Depth is bounded to avoid generating excessively large trees that would
    slow down the test without adding meaningful coverage. At depth 0, only
    leaf nodes are generated to guarantee termination.

    Args:
        max_depth: Maximum nesting depth for composite nodes.

    Returns:
        A Hypothesis strategy that generates valid ASTNode instances.
    """
    if max_depth == 0:
        # Base case: only leaf nodes to guarantee termination.
        return _leaf_node

    # Recursive case: composite nodes that reference sub-nodes.
    child_strategy = _build_ast_node_strategy(max_depth - 1)

    comparison_node = st.builds(
        ComparisonNode,
        left=child_strategy,
        operator=_comparison_operator,
        right=child_strategy,
    )

    logical_op_node = st.builds(
        LogicalOpNode,
        operator=_logical_operator,
        left=child_strategy,
        right=child_strategy,
    )

    not_op_node = st.builds(
        NotOpNode,
        operand=child_strategy,
    )

    predicate_node = st.builds(
        PredicateNode,
        binding=_identifier,
        condition=child_strategy,
    )

    aggregate_node = st.builds(
        AggregateNode,
        function=_aggregate_function,
        collection=_collection_ref_node,
        where=st.one_of(st.none(), predicate_node),
        select=st.one_of(st.none(), _field_access_node),
    )

    return st.one_of(
        _leaf_node,
        comparison_node,
        logical_op_node,
        not_op_node,
        aggregate_node,
    )


# Strategy for a single PolicyRuleNode with a bounded-depth condition.
_policy_rule_node = st.builds(
    PolicyRuleNode,
    name=_identifier,
    priority=st.integers(min_value=1, max_value=1000),
    condition=_build_ast_node_strategy(max_depth=2),
    effect=st.sampled_from(list(PolicyEffect)),
    message=st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd", "Zs"), whitelist_characters="_-.,:!"),
        max_size=100,
    ),
)

# RuleAST requires at least one rule (invariant from design).
_rule_ast = st.lists(_policy_rule_node, min_size=1, max_size=5).map(
    lambda rules: RuleAST(
        rules=tuple(rules),
        composition_mode=CompositionMode.DENY_OVERRIDES,
    )
)


# ---------------------------------------------------------------------------
# Strategies for RuleBundle sub-structures
# ---------------------------------------------------------------------------

# Semantic version strings like "1.0", "2.3.1" — kept simple and valid.
_semver = st.builds(
    lambda major, minor, patch: f"{major}.{minor}.{patch}" if patch else f"{major}.{minor}",
    major=st.integers(min_value=0, max_value=9),
    minor=st.integers(min_value=0, max_value=99),
    patch=st.one_of(st.none(), st.integers(min_value=0, max_value=99)),
)

_bundle_compatibility = st.builds(
    BundleCompatibility,
    dsl_version=_semver,
    context_schema_version=_semver,
    snapshot_schema_version=_semver,
    evaluator_min_version=_semver,
)

# ISO 8601 timestamps — simplified to a fixed format for test stability.
_iso_timestamp = st.builds(
    lambda y, m, d, h, mi, s: f"{y:04d}-{m:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}Z",
    y=st.integers(min_value=2020, max_value=2099),
    m=st.integers(min_value=1, max_value=12),
    d=st.integers(min_value=1, max_value=28),  # 28 is safe for all months
    h=st.integers(min_value=0, max_value=23),
    mi=st.integers(min_value=0, max_value=59),
    s=st.integers(min_value=0, max_value=59),
)

# SHA-256 hash strings in the format used by the domain (sha256:<hex>).
_sha256_hash = st.builds(
    lambda hex_part: f"sha256:{hex_part}",
    hex_part=st.text(
        alphabet="0123456789abcdef",
        min_size=64,
        max_size=64,
    ),
)

_compilation_metadata = st.builds(
    CompilationMetadata,
    author=_identifier,
    description=st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd", "Zs"), whitelist_characters="_-.,:"),
        max_size=200,
    ),
    compiled_at=_iso_timestamp,
    source_hash=_sha256_hash,
)

# execution_plan is an opaque dict stored verbatim — we test a few shapes.
_execution_plan = st.one_of(
    st.just({}),
    st.just({"version": 1, "steps": []}),
    st.fixed_dictionaries({"version": st.integers(min_value=1, max_value=10)}),
)

# Full RuleBundle strategy — the composite root for all round-trip tests.
_rule_bundle = st.builds(
    RuleBundle,
    policy_set_id=_identifier,
    artifact_hash=_sha256_hash,
    ast=_rule_ast,
    execution_plan=_execution_plan,
    compatibility=_bundle_compatibility,
    composition_mode=st.just(CompositionMode.DENY_OVERRIDES),
    metadata=_compilation_metadata,
)


# ---------------------------------------------------------------------------
# Property 13: Round-trip do bundle
# Validates: Requirements 3.3, 24.2
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(bundle=_rule_bundle)
@settings(max_examples=100, deadline=None)
def test_bundle_roundtrip_produces_structurally_equivalent_bundle(bundle: RuleBundle) -> None:
    """
    **Validates: Requirements 3.3, 24.2**

    Property 13 (core): Serializing a RuleBundle to JSON and deserializing it
    back must produce a bundle that is structurally equivalent to the original.

    This property verifies that no field is silently dropped, coerced, or
    mutated during the JSON round-trip. Structural equivalence is defined by
    the frozen dataclass equality (all fields compared by value).
    """
    json_str = bundle.to_json()
    reconstructed = RuleBundle.from_json(json_str)

    assert reconstructed == bundle, (
        f"Round-trip produced a structurally different bundle.\n"
        f"Original policy_set_id: {bundle.policy_set_id}\n"
        f"Reconstructed policy_set_id: {reconstructed.policy_set_id}"
    )


@pytest.mark.property
@given(bundle=_rule_bundle)
@settings(max_examples=100, deadline=None)
def test_bundle_roundtrip_preserves_artifact_hash(bundle: RuleBundle) -> None:
    """
    **Validates: Requirement 3.3**

    The artifact_hash field must survive the JSON round-trip unchanged.

    Req 3.3 states that the Control_Plane SHALL guarantee that artifact_hash
    is the SHA-256 of the serialized Rule_Bundle content, excluding the hash
    field itself. Once stored, the hash must be reproduced exactly on reload —
    any mutation would break integrity verification in the BundleLoader.
    """
    reconstructed = RuleBundle.from_json(bundle.to_json())

    assert reconstructed.artifact_hash == bundle.artifact_hash, (
        f"artifact_hash was mutated during round-trip.\n"
        f"Original:      {bundle.artifact_hash}\n"
        f"Reconstructed: {reconstructed.artifact_hash}"
    )


@pytest.mark.property
@given(bundle=_rule_bundle)
@settings(max_examples=100, deadline=None)
def test_bundle_roundtrip_preserves_bundle_compatibility_fields(bundle: RuleBundle) -> None:
    """
    **Validates: Requirement 24.2**

    All three compatibility version fields declared in BundleCompatibility must
    be preserved across the round-trip:
    - context_schema_version
    - snapshot_schema_version
    - evaluator_min_version

    Req 24.2 states that the Rule_Bundle SHALL declare compatibility with these
    three versions. Losing any of them during deserialization would prevent the
    PolicyRuntimeRegistry from performing compatibility checks before activation.
    """
    reconstructed = RuleBundle.from_json(bundle.to_json())

    original_compat = bundle.compatibility
    reconstructed_compat = reconstructed.compatibility

    assert reconstructed_compat.context_schema_version == original_compat.context_schema_version, (
        "context_schema_version was mutated during round-trip."
    )
    assert reconstructed_compat.snapshot_schema_version == original_compat.snapshot_schema_version, (
        "snapshot_schema_version was mutated during round-trip."
    )
    assert reconstructed_compat.evaluator_min_version == original_compat.evaluator_min_version, (
        "evaluator_min_version was mutated during round-trip."
    )
    assert reconstructed_compat.dsl_version == original_compat.dsl_version, (
        "dsl_version was mutated during round-trip."
    )


@pytest.mark.property
@given(bundle=_rule_bundle)
@settings(max_examples=100, deadline=None)
def test_bundle_roundtrip_preserves_composition_mode(bundle: RuleBundle) -> None:
    """
    **Validates: Requirements 3.3, 24.2**

    The composition_mode field must be preserved across the round-trip.

    CompositionMode is serialized as its string value (e.g. "DENY_OVERRIDES")
    and must be reconstructed as the correct enum member. A mismatch would
    cause the evaluator to apply the wrong composition semantics.
    """
    reconstructed = RuleBundle.from_json(bundle.to_json())

    assert reconstructed.composition_mode == bundle.composition_mode, (
        f"composition_mode was mutated during round-trip.\n"
        f"Original:      {bundle.composition_mode}\n"
        f"Reconstructed: {reconstructed.composition_mode}"
    )


@pytest.mark.property
@given(bundle=_rule_bundle)
@settings(max_examples=100, deadline=None)
def test_bundle_roundtrip_preserves_compilation_metadata(bundle: RuleBundle) -> None:
    """
    **Validates: Requirements 3.3, 24.2**

    All CompilationMetadata fields must be preserved across the round-trip:
    - author
    - description
    - compiled_at
    - source_hash

    These fields are required for auditability and rollback traceability.
    Losing them during deserialization would break the audit trail.
    """
    reconstructed = RuleBundle.from_json(bundle.to_json())

    original_meta = bundle.metadata
    reconstructed_meta = reconstructed.metadata

    assert reconstructed_meta.author == original_meta.author, (
        "metadata.author was mutated during round-trip."
    )
    assert reconstructed_meta.description == original_meta.description, (
        "metadata.description was mutated during round-trip."
    )
    assert reconstructed_meta.compiled_at == original_meta.compiled_at, (
        "metadata.compiled_at was mutated during round-trip."
    )
    assert reconstructed_meta.source_hash == original_meta.source_hash, (
        "metadata.source_hash was mutated during round-trip."
    )


@pytest.mark.property
@given(bundle=_rule_bundle)
@settings(max_examples=100, deadline=None)
def test_bundle_roundtrip_preserves_ast_rule_count_and_effects(bundle: RuleBundle) -> None:
    """
    **Validates: Requirement 3.3**

    The AST must be preserved structurally across the round-trip:
    - The number of rules must be identical.
    - Each rule's effect (ALLOW/DENY) must be preserved as the correct enum member.
    - The composition_mode declared in the AST must match the original.

    Rule effects are serialized as strings and must be reconstructed as
    PolicyEffect enum members. A coercion error here would silently change
    the security semantics of the policy.
    """
    reconstructed = RuleBundle.from_json(bundle.to_json())

    original_rules = bundle.ast.rules
    reconstructed_rules = reconstructed.ast.rules

    assert len(reconstructed_rules) == len(original_rules), (
        f"AST rule count changed during round-trip: "
        f"{len(original_rules)} → {len(reconstructed_rules)}"
    )

    for i, (original_rule, reconstructed_rule) in enumerate(
        zip(original_rules, reconstructed_rules)
    ):
        assert reconstructed_rule.effect == original_rule.effect, (
            f"Rule[{i}] effect was mutated during round-trip: "
            f"{original_rule.effect} → {reconstructed_rule.effect}"
        )
        assert reconstructed_rule.name == original_rule.name, (
            f"Rule[{i}] name was mutated during round-trip: "
            f"{original_rule.name!r} → {reconstructed_rule.name!r}"
        )
        assert reconstructed_rule.priority == original_rule.priority, (
            f"Rule[{i}] priority was mutated during round-trip: "
            f"{original_rule.priority} → {reconstructed_rule.priority}"
        )

    assert reconstructed.ast.composition_mode == bundle.ast.composition_mode, (
        "AST composition_mode was mutated during round-trip."
    )


@pytest.mark.property
@given(bundle=_rule_bundle)
@settings(max_examples=50, deadline=None)
def test_bundle_to_json_is_idempotent(bundle: RuleBundle) -> None:
    """
    **Validates: Requirement 3.3**

    Serializing the same bundle twice must produce identical JSON strings.

    This is a prerequisite for a reproducible artifact_hash: if to_json()
    were non-deterministic, the hash computed at compile time would not match
    the hash computed at load time, breaking integrity verification.
    """
    first_serialization = bundle.to_json()
    second_serialization = bundle.to_json()

    assert first_serialization == second_serialization, (
        "to_json() produced different output on two consecutive calls — "
        "serialization is not deterministic."
    )


@pytest.mark.property
@given(bundle=_rule_bundle)
@settings(max_examples=50, deadline=None)
def test_bundle_roundtrip_double_pass_is_stable(bundle: RuleBundle) -> None:
    """
    **Validates: Requirement 3.3**

    Applying the round-trip twice must produce the same result as applying it
    once. This verifies that the deserialized bundle is itself a valid input
    to the serializer — there are no hidden state differences between a
    freshly-constructed bundle and a deserialized one.
    """
    once = RuleBundle.from_json(bundle.to_json())
    twice = RuleBundle.from_json(once.to_json())

    assert once == twice, (
        "Double round-trip produced a different result than a single round-trip — "
        "the deserialized bundle is not stable under re-serialization."
    )
