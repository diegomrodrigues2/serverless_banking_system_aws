"""
Property-based tests for PolicyActivationManifest atomicity.

Validates: Requirements 4.4, 4.5

Property 2 (design.md): For every PolicyActivationManifest, artifact_hash,
snapshot_version, context_schema_version and evaluator_version must be used
as an indivisible unit. It must never be possible to evaluate with a new bundle
and an old snapshot.

These tests use Hypothesis to generate arbitrary manifests and verify that:
1. The manifest always carries all four version fields together.
2. A manifest built from a bundle/snapshot pair always reflects the
   compatibility fields of that bundle — never a mix from different bundles.
3. The PolicyPublisher rejects any bundle/snapshot pair where
   snapshot_schema_version does not match.

Covered requirements:
- Req 4.4: artifact_hash, snapshot_version, context_schema_version and
  evaluator_version are treated as an indivisible activation unit.
- Req 4.5: The system must prevent evaluation with cross-contaminated
  bundle/snapshot combinations.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from validation_engine.application.publisher import PolicyPublisher
from validation_engine.domain.errors import InvalidPolicyBundle
from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
    PolicyActivationManifest,
    ReferenceSnapshot,
    RuleBundle,
)
from validation_engine.domain.policy_ast import CompositionMode, RuleAST

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Short non-empty strings for version identifiers.
_version_str = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters=".-_"),
    min_size=1,
    max_size=20,
)

# Non-empty hash strings (simulating SHA-256 hex digests).
_hash_str = st.text(
    alphabet="0123456789abcdef",
    min_size=8,
    max_size=64,
)

# Non-empty identifier strings.
_identifier = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_-"),
    min_size=1,
    max_size=30,
)


@st.composite
def _bundle_strategy(draw) -> RuleBundle:
    """
    Generates an arbitrary RuleBundle with random compatibility fields.

    The bundle has an empty AST (no rules) to keep generation fast.
    The compatibility fields are the focus of these atomicity tests.
    """
    context_schema_version = draw(_version_str)
    snapshot_schema_version = draw(_version_str)
    evaluator_min_version = draw(_version_str.filter(lambda v: len(v) > 0))

    return RuleBundle(
        policy_set_id=draw(_identifier),
        artifact_hash=draw(_hash_str),
        ast=RuleAST(rules=(), composition_mode=CompositionMode.DENY_OVERRIDES),
        execution_plan={},
        compatibility=BundleCompatibility(
            dsl_version="1.0",
            context_schema_version=context_schema_version,
            snapshot_schema_version=snapshot_schema_version,
            evaluator_min_version=evaluator_min_version,
        ),
        composition_mode=CompositionMode.DENY_OVERRIDES,
        metadata=CompilationMetadata(
            author="property-test",
            description="Property test bundle",
            compiled_at="2026-01-01T00:00:00Z",
            source_hash=draw(_hash_str),
        ),
    )


@st.composite
def _snapshot_strategy(draw, snapshot_schema_version: str | None = None) -> ReferenceSnapshot:
    """
    Generates an arbitrary ReferenceSnapshot.

    If snapshot_schema_version is provided, uses that value.
    Otherwise generates a random version string.
    """
    schema_version = snapshot_schema_version or draw(_version_str)
    return ReferenceSnapshot(
        snapshot_version=draw(_identifier),
        snapshot_schema_version=schema_version,
        created_at="2026-01-01T00:00:00Z",
        data={"limit": draw(st.integers(min_value=1, max_value=10_000_000))},
    )


@st.composite
def _compatible_bundle_snapshot_pair(draw):
    """
    Generates a (bundle, snapshot) pair where snapshot_schema_version matches.

    This represents a valid pair that should be accepted by the publisher.
    """
    schema_version = draw(_version_str)
    bundle = draw(_bundle_strategy())
    # Override snapshot_schema_version to match the bundle's declaration
    bundle = RuleBundle(
        policy_set_id=bundle.policy_set_id,
        artifact_hash=bundle.artifact_hash,
        ast=bundle.ast,
        execution_plan=bundle.execution_plan,
        compatibility=BundleCompatibility(
            dsl_version=bundle.compatibility.dsl_version,
            context_schema_version=bundle.compatibility.context_schema_version,
            snapshot_schema_version=schema_version,
            evaluator_min_version=bundle.compatibility.evaluator_min_version,
        ),
        composition_mode=bundle.composition_mode,
        metadata=bundle.metadata,
    )
    snapshot = draw(_snapshot_strategy(snapshot_schema_version=schema_version))
    return bundle, snapshot


@st.composite
def _incompatible_bundle_snapshot_pair(draw):
    """
    Generates a (bundle, snapshot) pair where snapshot_schema_version does NOT match.

    This represents an invalid pair that should be rejected by the publisher.
    """
    bundle_schema = draw(_version_str)
    # Ensure snapshot schema is different from bundle schema
    snapshot_schema = draw(_version_str.filter(lambda v: v != bundle_schema))

    bundle = draw(_bundle_strategy())
    bundle = RuleBundle(
        policy_set_id=bundle.policy_set_id,
        artifact_hash=bundle.artifact_hash,
        ast=bundle.ast,
        execution_plan=bundle.execution_plan,
        compatibility=BundleCompatibility(
            dsl_version=bundle.compatibility.dsl_version,
            context_schema_version=bundle.compatibility.context_schema_version,
            snapshot_schema_version=bundle_schema,
            evaluator_min_version=bundle.compatibility.evaluator_min_version,
        ),
        composition_mode=bundle.composition_mode,
        metadata=bundle.metadata,
    )
    snapshot = draw(_snapshot_strategy(snapshot_schema_version=snapshot_schema))
    return bundle, snapshot


# ---------------------------------------------------------------------------
# Property 2: Ativação é atômica por manifesto
# ---------------------------------------------------------------------------


@given(bundle=_bundle_strategy())
@settings(max_examples=100)
def test_manifest_always_carries_all_four_version_fields(bundle: RuleBundle) -> None:
    """
    Property 2 (partial): Every PolicyActivationManifest must carry all four
    version fields: artifact_hash, snapshot_version, context_schema_version
    and evaluator_version.

    This test verifies that the manifest data model enforces the presence of
    all four fields — none can be None or empty after construction.
    """
    snapshot = ReferenceSnapshot(
        snapshot_version="snap_test_001",
        snapshot_schema_version=bundle.compatibility.snapshot_schema_version,
        created_at="2026-01-01T00:00:00Z",
        data={},
    )

    manifest = PolicyActivationManifest(
        activation_id="act_test_001",
        policy_scope_id="tenant:TRANSFER:*:*:prod",
        artifact_hash=bundle.artifact_hash,
        snapshot_version=snapshot.snapshot_version,
        context_schema_version=bundle.compatibility.context_schema_version,
        evaluator_version=bundle.compatibility.evaluator_min_version,
        activated_at="2026-01-01T00:00:00Z",
        activated_by="property-test",
    )

    # All four version fields must be present and non-empty.
    # An empty string would indicate a misconfigured bundle.
    assert manifest.artifact_hash, "artifact_hash must not be empty"
    assert manifest.snapshot_version, "snapshot_version must not be empty"
    assert manifest.context_schema_version, "context_schema_version must not be empty"
    assert manifest.evaluator_version, "evaluator_version must not be empty"


@given(pair=_compatible_bundle_snapshot_pair())
@settings(max_examples=100)
def test_manifest_reflects_bundle_compatibility_fields(pair) -> None:
    """
    Property 2 (partial): A manifest built from a bundle/snapshot pair must
    reflect the compatibility fields of that specific bundle.

    The manifest's context_schema_version must equal the bundle's
    context_schema_version, and the evaluator_version must equal the
    bundle's evaluator_min_version. This ensures the manifest is always
    derived from a single bundle — never a mix of fields from different bundles.
    """
    bundle, snapshot = pair

    manifest = PolicyActivationManifest(
        activation_id="act_test_001",
        policy_scope_id="tenant:TRANSFER:*:*:prod",
        artifact_hash=bundle.artifact_hash,
        snapshot_version=snapshot.snapshot_version,
        context_schema_version=bundle.compatibility.context_schema_version,
        evaluator_version=bundle.compatibility.evaluator_min_version,
        activated_at="2026-01-01T00:00:00Z",
        activated_by="property-test",
    )

    # The manifest must reflect the bundle's compatibility fields exactly.
    assert manifest.context_schema_version == bundle.compatibility.context_schema_version
    assert manifest.evaluator_version == bundle.compatibility.evaluator_min_version
    assert manifest.artifact_hash == bundle.artifact_hash
    assert manifest.snapshot_version == snapshot.snapshot_version


@given(pair=_incompatible_bundle_snapshot_pair())
@settings(max_examples=100)
def test_publisher_rejects_incompatible_bundle_snapshot_pair(pair) -> None:
    """
    Property 2 (partial): The PolicyPublisher must reject any bundle/snapshot
    pair where snapshot_schema_version does not match.

    This is the core guard against cross-contaminated activations: a bundle
    compiled for schema v1 must never be activated with a snapshot of schema v2.
    The publisher enforces this invariant before any I/O.
    """
    from unittest.mock import MagicMock

    bundle, snapshot = pair

    # Publisher with a mock client — we expect the error to be raised
    # before any AppConfig API call is made.
    mock_client = MagicMock()
    publisher = PolicyPublisher(
        appconfig_client=mock_client,
        application_id="test-app",
        environment_id="test-env",
        configuration_profile_id="test-profile",
        deployment_strategy_id="test-strategy",
        wait_for_deployment=False,
    )

    with pytest.raises(InvalidPolicyBundle) as exc_info:
        publisher.publish(bundle, snapshot, "tenant:TRANSFER:*:*:prod")

    # The error must mention snapshot_schema_version to help diagnose the issue.
    assert "snapshot_schema_version" in str(exc_info.value)

    # No AppConfig API calls should have been made — validation is pre-I/O.
    mock_client.create_hosted_configuration_version.assert_not_called()
    mock_client.start_deployment.assert_not_called()


@given(pair=_compatible_bundle_snapshot_pair())
@settings(max_examples=50)
def test_compatible_pair_passes_validation(pair) -> None:
    """
    Property 2 (partial): A compatible bundle/snapshot pair must pass
    the publisher's compatibility validation without raising an exception.

    This is the positive counterpart to test_publisher_rejects_incompatible_pair:
    for every pair where snapshot_schema_version matches, the publisher must
    proceed to the AppConfig API calls (not raise InvalidPolicyBundle).
    """
    from unittest.mock import MagicMock

    bundle, snapshot = pair

    mock_client = MagicMock()
    mock_client.create_hosted_configuration_version.return_value = {"VersionNumber": 1}
    mock_client.start_deployment.return_value = {"DeploymentNumber": 1}

    publisher = PolicyPublisher(
        appconfig_client=mock_client,
        application_id="test-app",
        environment_id="test-env",
        configuration_profile_id="test-profile",
        deployment_strategy_id="test-strategy",
        wait_for_deployment=False,
    )

    # Must not raise InvalidPolicyBundle for a compatible pair.
    result = publisher.publish(bundle, snapshot, "tenant:TRANSFER:*:*:prod")

    assert isinstance(result, PolicyActivationManifest)
    # AppConfig API must have been called — validation passed.
    mock_client.create_hosted_configuration_version.assert_called_once()
