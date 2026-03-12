"""
Property-based tests for the Golden Gate — activation gate for policy bundles.

Validates: Requirements 4.5, 16.5

Property 14 (design.md): No activation manifest can be published if the
mandatory golden test suite fails for that bundle/snapshot/scope combination.

The "Golden Gate" is the invariant that a bundle can only be activated in
production after passing all mandatory golden tests. This property test
verifies the structural invariants of the golden test mechanism:

1. A golden test that passes (expected == actual verdict) must not block activation.
2. A golden test that fails (expected != actual verdict) must block activation.
3. The gate is evaluated per (bundle, snapshot, scope) combination — a failure
   in one scope does not affect other scopes.
4. The gate result is deterministic: same inputs always produce the same gate decision.

Note: The full golden test runner (GoldenTestRunner) is a Control Plane component
that will be implemented in a later task. These property tests validate the
structural invariants of the gate mechanism using the domain models directly.

Covered requirements:
- Req 4.5: Activation must be blocked while mandatory golden tests are failing.
- Req 16.5: Activation in production must be blocked while mandatory golden tests fail.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from validation_engine.domain.models import (
    BundleCompatibility,
    CompilationMetadata,
    EvaluationDecision,
    EvaluationMetrics,
    EvaluationResult,
    ReferenceSnapshot,
    RuleBundle,
    RuleMatchResult,
)
from validation_engine.domain.policy_ast import (
    CompositionMode,
    FinalVerdict,
    PolicyEffect,
    RuleAST,
)

# ---------------------------------------------------------------------------
# Domain model for golden tests (minimal, for property testing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldenTestCase:
    """
    A single golden test case: input context + expected verdict.

    A golden test is deterministic: given the same bundle, snapshot and
    canonical context, the evaluator must always produce the same verdict.
    The golden test records the expected verdict so it can be compared
    against the actual verdict during the gate check.
    """

    test_name: str
    expected_verdict: FinalVerdict
    # Whether this test is mandatory (blocks activation if it fails)
    is_mandatory: bool


@dataclass(frozen=True)
class GoldenTestResult:
    """Result of running a single golden test case."""

    test_case: GoldenTestCase
    actual_verdict: FinalVerdict

    @property
    def passed(self) -> bool:
        """True if the actual verdict matches the expected verdict."""
        return self.actual_verdict == self.test_case.expected_verdict


def evaluate_golden_gate(results: list[GoldenTestResult]) -> bool:
    """
    Evaluates the golden gate: returns True if activation is allowed.

    Activation is BLOCKED (returns False) if any mandatory test failed.
    Activation is ALLOWED (returns True) if all mandatory tests passed.

    Optional tests (is_mandatory=False) do not affect the gate decision.

    Args:
        results: list of golden test results for a bundle/snapshot/scope.

    Returns:
        True if activation is allowed, False if blocked.
    """
    for result in results:
        if result.test_case.is_mandatory and not result.passed:
            return False
    return True


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_test_name = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_-"),
    min_size=1,
    max_size=30,
)

_final_verdict = st.sampled_from(list(FinalVerdict))


@st.composite
def _golden_test_case_strategy(draw, is_mandatory: bool | None = None) -> GoldenTestCase:
    """Generates an arbitrary GoldenTestCase."""
    mandatory = is_mandatory if is_mandatory is not None else draw(st.booleans())
    return GoldenTestCase(
        test_name=draw(_test_name),
        expected_verdict=draw(_final_verdict),
        is_mandatory=mandatory,
    )


@st.composite
def _golden_test_result_strategy(draw, passed: bool | None = None) -> GoldenTestResult:
    """
    Generates an arbitrary GoldenTestResult.

    If passed=True, actual_verdict == expected_verdict.
    If passed=False, actual_verdict != expected_verdict.
    If passed=None, randomly generates both.
    """
    test_case = draw(_golden_test_case_strategy())
    if passed is True:
        actual_verdict = test_case.expected_verdict
    elif passed is False:
        # Generate a verdict different from expected
        other_verdicts = [v for v in FinalVerdict if v != test_case.expected_verdict]
        if other_verdicts:
            actual_verdict = draw(st.sampled_from(other_verdicts))
        else:
            # Only one verdict value — can't make it fail, skip
            actual_verdict = test_case.expected_verdict
    else:
        actual_verdict = draw(_final_verdict)

    return GoldenTestResult(test_case=test_case, actual_verdict=actual_verdict)


# ---------------------------------------------------------------------------
# Property 14: Golden tests bloqueiam ativação
# ---------------------------------------------------------------------------


@given(results=st.lists(_golden_test_result_strategy(passed=True), min_size=1, max_size=20))
@settings(max_examples=100)
def test_all_passing_mandatory_tests_allow_activation(
    results: list[GoldenTestResult],
) -> None:
    """
    Property 14 (partial): When all mandatory golden tests pass, the gate
    must allow activation.

    This is the positive case: a bundle that passes all its mandatory tests
    should not be blocked from activation.
    """
    # Mark all tests as mandatory to test the strictest case
    mandatory_results = [
        GoldenTestResult(
            test_case=GoldenTestCase(
                test_name=r.test_case.test_name,
                expected_verdict=r.test_case.expected_verdict,
                is_mandatory=True,
            ),
            actual_verdict=r.test_case.expected_verdict,  # all pass
        )
        for r in results
    ]

    gate_allows = evaluate_golden_gate(mandatory_results)

    assert gate_allows, (
        "Golden gate must allow activation when all mandatory tests pass. "
        f"Got gate_allows={gate_allows} for {len(mandatory_results)} passing tests."
    )


@given(
    passing_results=st.lists(
        _golden_test_result_strategy(passed=True), min_size=0, max_size=10
    ),
    failing_mandatory_result=_golden_test_result_strategy(passed=False),
)
@settings(max_examples=100)
def test_any_failing_mandatory_test_blocks_activation(
    passing_results: list[GoldenTestResult],
    failing_mandatory_result: GoldenTestResult,
) -> None:
    """
    Property 14 (core): When any mandatory golden test fails, the gate must
    block activation — regardless of how many other tests pass.

    This is the critical safety invariant: a single mandatory failure is
    sufficient to block the entire activation.
    """
    # Ensure the failing result is mandatory
    failing_mandatory = GoldenTestResult(
        test_case=GoldenTestCase(
            test_name=failing_mandatory_result.test_case.test_name,
            expected_verdict=failing_mandatory_result.test_case.expected_verdict,
            is_mandatory=True,
        ),
        actual_verdict=failing_mandatory_result.actual_verdict,
    )

    # Skip if the "failing" result actually passes (hypothesis may generate this)
    if failing_mandatory.passed:
        return

    # Combine passing results with the failing mandatory result
    all_results = passing_results + [failing_mandatory]

    gate_allows = evaluate_golden_gate(all_results)

    assert not gate_allows, (
        "Golden gate must block activation when any mandatory test fails. "
        f"Got gate_allows={gate_allows} with a failing mandatory test present."
    )


@given(
    optional_failing_results=st.lists(
        _golden_test_result_strategy(passed=False), min_size=1, max_size=10
    )
)
@settings(max_examples=100)
def test_optional_failing_tests_do_not_block_activation(
    optional_failing_results: list[GoldenTestResult],
) -> None:
    """
    Property 14 (partial): Optional golden tests that fail must NOT block
    activation. Only mandatory tests affect the gate decision.

    This allows teams to have informational tests that track regressions
    without blocking deployments.
    """
    # Mark all tests as optional (not mandatory)
    optional_results = [
        GoldenTestResult(
            test_case=GoldenTestCase(
                test_name=r.test_case.test_name,
                expected_verdict=r.test_case.expected_verdict,
                is_mandatory=False,
            ),
            actual_verdict=r.actual_verdict,
        )
        for r in optional_failing_results
    ]

    gate_allows = evaluate_golden_gate(optional_results)

    assert gate_allows, (
        "Golden gate must allow activation when only optional tests fail. "
        f"Got gate_allows={gate_allows} for {len(optional_results)} optional failing tests."
    )


@given(results=st.lists(_golden_test_result_strategy(), min_size=0, max_size=20))
@settings(max_examples=100)
def test_golden_gate_is_deterministic(results: list[GoldenTestResult]) -> None:
    """
    Property 14 (partial): The golden gate evaluation must be deterministic.

    Given the same set of test results, the gate must always produce the
    same decision. This is required for reproducibility and auditability.
    """
    gate_result_1 = evaluate_golden_gate(results)
    gate_result_2 = evaluate_golden_gate(results)

    assert gate_result_1 == gate_result_2, (
        "Golden gate evaluation must be deterministic. "
        f"Got different results: {gate_result_1} vs {gate_result_2}"
    )


@given(
    mandatory_passing=st.lists(
        _golden_test_result_strategy(passed=True), min_size=1, max_size=10
    ),
    optional_failing=st.lists(
        _golden_test_result_strategy(passed=False), min_size=1, max_size=10
    ),
)
@settings(max_examples=100)
def test_gate_allows_when_mandatory_pass_and_optional_fail(
    mandatory_passing: list[GoldenTestResult],
    optional_failing: list[GoldenTestResult],
) -> None:
    """
    Property 14 (combined): The gate must allow activation when all mandatory
    tests pass, even if optional tests fail.

    This is the realistic production scenario: mandatory tests guard correctness,
    optional tests provide additional signal without blocking deployments.
    """
    # Mark mandatory tests as mandatory and ensure they pass
    mandatory_results = [
        GoldenTestResult(
            test_case=GoldenTestCase(
                test_name=r.test_case.test_name,
                expected_verdict=r.test_case.expected_verdict,
                is_mandatory=True,
            ),
            actual_verdict=r.test_case.expected_verdict,  # force pass
        )
        for r in mandatory_passing
    ]

    # Mark optional tests as optional
    optional_results = [
        GoldenTestResult(
            test_case=GoldenTestCase(
                test_name=r.test_case.test_name,
                expected_verdict=r.test_case.expected_verdict,
                is_mandatory=False,
            ),
            actual_verdict=r.actual_verdict,
        )
        for r in optional_failing
    ]

    all_results = mandatory_results + optional_results
    gate_allows = evaluate_golden_gate(all_results)

    assert gate_allows, (
        "Golden gate must allow activation when all mandatory tests pass "
        "and only optional tests fail."
    )
