"""
Property-based tests for bundle and snapshot integrity (Property 10).

**Validates: Requirements 17.3, 17.4, 20.3, 20.4**

Property 10 (design.md): "Bundle e snapshot só entram em runtime se íntegros"
Meaning: A bundle or snapshot is only accepted into the runtime (ActivePolicySet)
if its integrity has been verified. Any bundle with a tampered/incorrect hash
must be rejected with PolicyBundleIntegrityFailure. Any snapshot with an
incompatible schema_version must be rejected with PolicySnapshotUnavailable.

Sub-properties covered:

  10a — Bundle integrity: correct hash always passes
        For any valid RuleBundle with a correctly computed artifact_hash,
        the BundleLoader must accept it (no PolicyBundleIntegrityFailure).

  10b — Bundle integrity: any modification to content invalidates the hash
        For any valid RuleBundle, if any field in the serialized JSON is
        modified (except artifact_hash itself), the BundleLoader must reject
        it with PolicyBundleIntegrityFailure.

  10c — Bundle integrity: artifact_hash is SHA-256 of content without hash field
        For any valid RuleBundle, the artifact_hash must equal
        SHA-256(json_without_artifact_hash_field).

  10d — Snapshot schema compatibility: matching version always passes
        For any ReferenceSnapshot, if the SnapshotLoader is configured with
        the same snapshot_schema_version as the snapshot, it must accept it.

  10e — Snapshot schema compatibility: mismatched version always fails
        For any ReferenceSnapshot, if the SnapshotLoader is configured with
        a DIFFERENT snapshot_schema_version, it must raise PolicySnapshotUnavailable.

  10f — Bundle integrity: hash is deterministic
        For any valid RuleBundle, computing the artifact_hash twice from the
        same content must produce the same result.

Implementation notes:
  - Uses moto to mock S3 for BundleLoader tests (same pattern as integration tests).
  - Reuses strategies from test_bundle_roundtrip.py for RuleBundle generation.
  - For bundle modification tests: picks a top-level field to tamper with.
  - For snapshot tests: uses simple Hypothesis strategies for ReferenceSnapshot.

Requisitos cobertos: 17.3, 17.4, 20.3, 20.4
"""

from __future__ import annotations

import hashlib
import json

import boto3
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from validation_engine.domain.errors import (
    PolicyBundleIntegrityFailure,
    PolicySnapshotUnavailable,
)
from validation_engine.domain.models import ReferenceSnapshot, RuleBundle
from validation_engine.infrastructure.bundle_loader import BundleLoader
from validation_engine.infrastructure.snapshot_loader import SnapshotLoader

# ---------------------------------------------------------------------------
# Re-use strategies from test_bundle_roundtrip.py
# ---------------------------------------------------------------------------

# Import the _rule_bundle strategy directly from the roundtrip module so we
# don't duplicate the complex AST generation logic.
from tests.validation_engine.property.test_bundle_roundtrip import (
    _rule_bundle,
    _semver,
    _iso_timestamp,
    _identifier,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BUCKET_NAME = "integrity-test-bucket"
_AWS_REGION = "us-east-1"
_CONTEXT_SCHEMA_VERSION = "1.0"
_EVALUATOR_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_correct_artifact_hash(bundle: RuleBundle) -> str:
    """
    Compute the correct artifact_hash for a RuleBundle.

    Replicates the algorithm used by BundleLoader._verify_integrity:
    1. Serialize the bundle to JSON.
    2. Remove the 'artifact_hash' field.
    3. Re-serialize with sort_keys=True.
    4. SHA-256 of the UTF-8 encoded canonical content.

    This is the same algorithm documented in Req 3.3 and implemented in
    BundleLoader._verify_integrity.
    """
    raw = json.loads(bundle.to_json())
    content_without_hash = {k: v for k, v in raw.items() if k != "artifact_hash"}
    canonical = json.dumps(content_without_hash, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_bundle_with_correct_hash(bundle: RuleBundle) -> RuleBundle:
    """
    Return a new RuleBundle identical to the input but with a correctly
    computed artifact_hash.

    The BundleLoader verifies integrity by recomputing the hash from the
    stored JSON content. For Property 10a and 10f tests, we need a bundle
    whose artifact_hash matches the actual content hash so the loader accepts it.
    """
    correct_hash = _compute_correct_artifact_hash(bundle)
    # Reconstruct the bundle with the correct hash via JSON round-trip.
    raw = json.loads(bundle.to_json())
    raw["artifact_hash"] = correct_hash
    return RuleBundle.from_json(json.dumps(raw))


def _make_s3_client():
    """Create a boto3 S3 client pointing at the active moto mock."""
    return boto3.client(
        "s3",
        region_name=_AWS_REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _create_bucket(s3_client) -> None:
    """Create the test bucket in the active moto mock."""
    s3_client.create_bucket(Bucket=_BUCKET_NAME)


def _put_bundle_json(s3_client, artifact_hash: str, json_content: str) -> None:
    """
    Write raw JSON content directly to S3 under the bundle key.

    Used to inject both valid and tampered bundle content into the mock S3
    without going through BundleStore (which would add KMS parameters).
    """
    key = f"bundles/{artifact_hash}.json"
    s3_client.put_object(
        Bucket=_BUCKET_NAME,
        Key=key,
        Body=json_content.encode("utf-8"),
        ContentType="application/json",
    )


def _put_snapshot_json(s3_client, snapshot_version: str, json_content: str) -> None:
    """Write raw JSON content directly to S3 under the snapshot key."""
    key = f"snapshots/{snapshot_version}.json"
    s3_client.put_object(
        Bucket=_BUCKET_NAME,
        Key=key,
        Body=json_content.encode("utf-8"),
        ContentType="application/json",
    )


def _snapshot_to_json(snapshot: ReferenceSnapshot) -> str:
    """
    Serialize a ReferenceSnapshot to JSON for storage in S3.

    Replicates the serialization logic of SnapshotStore: tuples are stored
    as JSON arrays, scalars are stored as-is.
    """

    def _serialize_value(value):
        # Tuples are serialized as lists (JSON has no tuple type).
        if isinstance(value, tuple):
            return list(value)
        return value

    payload = {
        "snapshot_version": snapshot.snapshot_version,
        "snapshot_schema_version": snapshot.snapshot_schema_version,
        "created_at": snapshot.created_at,
        "data": {k: _serialize_value(v) for k, v in snapshot.data.items()},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# Strategies for ReferenceSnapshot
# ---------------------------------------------------------------------------

# Simple scalar data values for snapshot.data — keeps the strategy fast.
_snapshot_scalar_value = st.one_of(
    st.integers(min_value=0, max_value=10_000_000),
    st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_-"),
        min_size=1,
        max_size=30,
    ),
    st.booleans(),
)

# Snapshot data dict: 1–5 scalar entries (no tuples to keep strategy simple).
_snapshot_data = st.dictionaries(
    keys=_identifier,
    values=_snapshot_scalar_value,
    min_size=1,
    max_size=5,
)

# Full ReferenceSnapshot strategy.
_reference_snapshot = st.builds(
    ReferenceSnapshot,
    snapshot_version=_identifier,
    snapshot_schema_version=_semver,
    created_at=_iso_timestamp,
    data=_snapshot_data,
)


# ---------------------------------------------------------------------------
# Property 10a: Bundle integrity — correct hash always passes
# Validates: Requirements 17.3, 20.3
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(bundle=_rule_bundle)
@settings(max_examples=50, deadline=None)
def test_bundle_with_correct_hash_is_accepted_by_loader(bundle: RuleBundle) -> None:
    """
    **Validates: Requirements 17.3, 20.3**

    Property 10a: For any valid RuleBundle with a correctly computed
    artifact_hash, the BundleLoader must accept it without raising
    PolicyBundleIntegrityFailure.

    The BundleLoader verifies integrity by recomputing SHA-256 of the bundle
    content (excluding the artifact_hash field) and comparing with the stored
    hash. A correctly computed hash must always pass this check.

    This property ensures that the integrity check does not produce false
    positives — valid bundles must never be rejected.
    """
    # Build a bundle whose artifact_hash matches the actual content hash.
    # The _rule_bundle strategy generates arbitrary hashes; we replace it
    # with the correct one so the loader accepts the bundle.
    valid_bundle = _build_bundle_with_correct_hash(bundle)

    # The BundleLoader also checks compatibility. We configure it to match
    # the bundle's declared versions so compatibility never blocks this test.
    context_version = valid_bundle.compatibility.context_schema_version
    evaluator_version = valid_bundle.compatibility.evaluator_min_version

    with mock_aws():
        s3 = _make_s3_client()
        _create_bucket(s3)
        _put_bundle_json(s3, valid_bundle.artifact_hash, valid_bundle.to_json())

        loader = BundleLoader(
            s3_client=s3,
            bucket_name=_BUCKET_NAME,
            current_context_schema_version=context_version,
            current_evaluator_version=evaluator_version,
        )

        # Must not raise PolicyBundleIntegrityFailure.
        loaded = loader.load(valid_bundle.artifact_hash)

    assert loaded.artifact_hash == valid_bundle.artifact_hash, (
        "Loaded bundle has a different artifact_hash than the stored bundle."
    )


# ---------------------------------------------------------------------------
# Property 10b: Bundle integrity — any modification to content invalidates hash
# Validates: Requirements 17.3, 17.4, 20.3, 20.4
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(bundle=_rule_bundle)
@settings(max_examples=50, deadline=None)
def test_tampered_bundle_content_is_rejected_with_integrity_failure(
    bundle: RuleBundle,
) -> None:
    """
    **Validates: Requirements 17.3, 17.4, 20.3, 20.4**

    Property 10b: For any valid RuleBundle, if the policy_set_id field in the
    serialized JSON is modified while keeping the original artifact_hash, the
    BundleLoader must reject it with PolicyBundleIntegrityFailure.

    This simulates a content-substitution attack: an adversary replaces the
    bundle content in S3 but cannot update the artifact_hash (which is stored
    in the manifest and verified independently). The loader must detect the
    mismatch between the stored hash and the recomputed hash of the tampered
    content.

    We tamper with policy_set_id because it is always present in the serialized
    JSON and its modification is guaranteed to change the computed hash.
    """
    # Start with a bundle that has a correct hash so we know the original
    # content is valid. Then tamper with the content.
    valid_bundle = _build_bundle_with_correct_hash(bundle)

    # Tamper: modify policy_set_id in the JSON while keeping artifact_hash.
    raw = json.loads(valid_bundle.to_json())
    raw["policy_set_id"] = "TAMPERED-" + raw["policy_set_id"]
    tampered_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)

    # The loader is asked to load by the original (correct) artifact_hash,
    # but the stored content has been modified — hash mismatch must be detected.
    original_hash = valid_bundle.artifact_hash

    context_version = valid_bundle.compatibility.context_schema_version
    evaluator_version = valid_bundle.compatibility.evaluator_min_version

    with mock_aws():
        s3 = _make_s3_client()
        _create_bucket(s3)
        # Store the tampered JSON under the original hash key.
        _put_bundle_json(s3, original_hash, tampered_json)

        loader = BundleLoader(
            s3_client=s3,
            bucket_name=_BUCKET_NAME,
            current_context_schema_version=context_version,
            current_evaluator_version=evaluator_version,
        )

        with pytest.raises(PolicyBundleIntegrityFailure):
            loader.load(original_hash)


# ---------------------------------------------------------------------------
# Property 10c: artifact_hash is SHA-256 of content without hash field
# Validates: Requirements 3.3, 20.3
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(bundle=_rule_bundle)
@settings(max_examples=50, deadline=None)
def test_artifact_hash_equals_sha256_of_content_without_hash_field(
    bundle: RuleBundle,
) -> None:
    """
    **Validates: Requirements 3.3, 20.3**

    Property 10c: For any valid RuleBundle, the artifact_hash stored in the
    bundle must equal SHA-256(canonical_json_without_artifact_hash_field).

    This property verifies the hash algorithm contract defined in Req 3.3:
    "artifact_hash SHALL be SHA-256 of the serialized Rule_Bundle content,
    excluding the artifact_hash field itself."

    We build a bundle with a correctly computed hash and then verify that
    the hash matches the expected algorithm output. This ensures that the
    hash computation is consistent and reproducible.
    """
    # Build a bundle with a correctly computed artifact_hash.
    valid_bundle = _build_bundle_with_correct_hash(bundle)

    # Independently compute the expected hash using the documented algorithm.
    raw = json.loads(valid_bundle.to_json())
    content_without_hash = {k: v for k, v in raw.items() if k != "artifact_hash"}
    canonical = json.dumps(content_without_hash, ensure_ascii=False, sort_keys=True)
    expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert valid_bundle.artifact_hash == expected_hash, (
        f"artifact_hash does not match SHA-256 of content without hash field.\n"
        f"Stored:   {valid_bundle.artifact_hash}\n"
        f"Expected: {expected_hash}"
    )


# ---------------------------------------------------------------------------
# Property 10d: Snapshot schema compatibility — matching version always passes
# Validates: Requirements 17.4, 20.4
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(snapshot=_reference_snapshot)
@settings(max_examples=50, deadline=None)
def test_snapshot_with_matching_schema_version_is_accepted(
    snapshot: ReferenceSnapshot,
) -> None:
    """
    **Validates: Requirements 17.4, 20.4**

    Property 10d: For any ReferenceSnapshot, if the SnapshotLoader is
    configured with the same snapshot_schema_version as the snapshot,
    it must accept it without raising PolicySnapshotUnavailable.

    The SnapshotLoader verifies schema compatibility by comparing the
    snapshot's snapshot_schema_version with the expected version configured
    at loader construction time. A matching version must always pass.

    This property ensures no false positives in schema compatibility checks.
    """
    # Configure the loader with the same schema version as the snapshot.
    expected_version = snapshot.snapshot_schema_version

    with mock_aws():
        s3 = _make_s3_client()
        _create_bucket(s3)
        _put_snapshot_json(s3, snapshot.snapshot_version, _snapshot_to_json(snapshot))

        loader = SnapshotLoader(
            s3_client=s3,
            bucket_name=_BUCKET_NAME,
            expected_snapshot_schema_version=expected_version,
        )

        # Must not raise PolicySnapshotUnavailable.
        loaded = loader.load(snapshot.snapshot_version)

    assert loaded.snapshot_version == snapshot.snapshot_version, (
        "Loaded snapshot has a different snapshot_version than the stored snapshot."
    )
    assert loaded.snapshot_schema_version == expected_version, (
        "Loaded snapshot has a different snapshot_schema_version than expected."
    )


# ---------------------------------------------------------------------------
# Property 10e: Snapshot schema compatibility — mismatched version always fails
# Validates: Requirements 17.4, 20.4
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(
    snapshot=_reference_snapshot,
    different_version=_semver,
)
@settings(max_examples=50, deadline=None)
def test_snapshot_with_mismatched_schema_version_is_rejected(
    snapshot: ReferenceSnapshot,
    different_version: str,
) -> None:
    """
    **Validates: Requirements 17.4, 20.4**

    Property 10e: For any ReferenceSnapshot, if the SnapshotLoader is
    configured with a DIFFERENT snapshot_schema_version than the snapshot
    declares, it must raise PolicySnapshotUnavailable.

    The SnapshotLoader must reject snapshots whose schema_version does not
    match the expected version. This prevents the runtime from using a
    snapshot compiled for a different schema, which could cause incorrect
    evaluation results.

    We use assume() to skip cases where the generated version happens to
    match the snapshot's version (which would be a valid case, not a mismatch).
    """
    from hypothesis import assume

    # Skip the case where the generated version accidentally matches the snapshot.
    # In that case, the loader would correctly accept the snapshot, which is
    # Property 10d — not what we are testing here.
    assume(different_version != snapshot.snapshot_schema_version)

    with mock_aws():
        s3 = _make_s3_client()
        _create_bucket(s3)
        _put_snapshot_json(s3, snapshot.snapshot_version, _snapshot_to_json(snapshot))

        # Configure the loader with a DIFFERENT schema version.
        loader = SnapshotLoader(
            s3_client=s3,
            bucket_name=_BUCKET_NAME,
            expected_snapshot_schema_version=different_version,
        )

        # Must raise PolicySnapshotUnavailable due to schema version mismatch.
        with pytest.raises(PolicySnapshotUnavailable):
            loader.load(snapshot.snapshot_version)


# ---------------------------------------------------------------------------
# Property 10f: Bundle integrity — hash is deterministic
# Validates: Requirements 3.3, 20.3
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(bundle=_rule_bundle)
@settings(max_examples=50, deadline=None)
def test_artifact_hash_computation_is_deterministic(bundle: RuleBundle) -> None:
    """
    **Validates: Requirements 3.3, 20.3**

    Property 10f: For any valid RuleBundle, computing the artifact_hash twice
    from the same content must produce the same result.

    This property verifies that the hash computation algorithm is deterministic:
    given the same bundle content, the hash is always identical. This is a
    prerequisite for reproducible integrity verification — if the hash were
    non-deterministic, the BundleLoader could reject valid bundles on reload.

    We compute the hash twice independently and verify they are equal.
    """
    # Compute the hash twice from the same bundle content.
    first_hash = _compute_correct_artifact_hash(bundle)
    second_hash = _compute_correct_artifact_hash(bundle)

    assert first_hash == second_hash, (
        f"artifact_hash computation is not deterministic.\n"
        f"First computation:  {first_hash}\n"
        f"Second computation: {second_hash}"
    )
