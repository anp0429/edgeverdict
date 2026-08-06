# EDGEVERDICT_WORDING_PREFERENCE_TESTS_V1
"""A value-mismatch that asserts on MESSAGE WORDING (assertNotIn / assertIn on
prose, toContain) is a specification disagreement about phrasing, not a
correctness defect. The #542 run proved the failure mode: five "gaps" were one
mutually-contradictory disagreement about what a 401 message should say. The
deterministic classifier now tiers these low with a category-naming reason —
no LLM needed to see that a negative string assertion on an error message is
wording preference. It must NOT catch real value bugs, list equality, or
id-membership checks."""
from __future__ import annotations

from edgeverdict.review import (
    ReviewFinding,
    _is_wording_preference,
    classify_gap_confidence,
)


# ── the discriminator ────────────────────────────────────────────────────
def test_negative_message_assertion_is_wording():
    obs = ("AssertionError: 'verify both' unexpectedly found in "
           "'error loading feature flags: invalid personal api key'")
    assert _is_wording_preference(obs) is True


def test_positive_message_assertion_is_wording():
    obs = ("AssertionError: 'project_api_key' not found in "
           "'Error loading feature flags: please check your token'")
    assert _is_wording_preference(obs) is True


def test_value_inequality_is_not_wording():
    assert _is_wording_preference("AssertionError: 42 != 43") is False


def test_list_equality_is_not_wording():
    assert _is_wording_preference(
        "AssertionError: assert result == [1, 2, 3]") is False


def test_id_membership_is_not_wording():
    # a membership check on identifiers is a value check, not message wording
    assert _is_wording_preference(
        "AssertionError: 'user_5' not found in ['user_1', 'user_2']") is False


def test_assertionerror_name_alone_does_not_trigger():
    # the word "Error" inside "AssertionError" must not supply message-context
    assert _is_wording_preference(
        "AssertionError: 5 not found in [1, 2, 3]") is False


def test_vitest_tocontain_on_message_is_wording():
    obs = ("Error: expected 'invalid token message' to contain "
           "'project_api_key'")
    assert _is_wording_preference(obs) is True


# ── the classifier integration ───────────────────────────────────────────
def _gap(observed: str) -> ReviewFinding:
    f = ReviewFinding(behavior="b")
    f.status = "confirmed_gap"
    f.observed = observed
    return f


def test_wording_gap_tiered_low_with_spec_disagreement_reason():
    f = _gap("AssertionError: 'verify both' unexpectedly found in "
             "'error loading feature flags: invalid personal api key'")
    classify_gap_confidence([f])
    assert f.confidence == "low"
    assert "specification disagreement" in f.confidence_reason
    assert "wording" in f.confidence_reason


def test_wording_reason_names_the_user_breakage_test():
    f = _gap("AssertionError: 'personal_api_key' unexpectedly found in "
             "'Error: Invalid API key. Please verify both keys'")
    classify_gap_confidence([f])
    # the reason must carry the "would a user observe breakage?" framing and
    # the demand for a contract
    assert "user would observe breakage" in f.confidence_reason.lower()
    assert ("contract" in f.confidence_reason.lower()
            or "hypothesis" in f.confidence_reason.lower())


def test_real_value_bug_stays_out_of_wording_category():
    # an objective mismatch must NOT be relabeled as wording preference
    f = _gap("AssertionError: 42 != 43")
    classify_gap_confidence([f])
    assert "specification disagreement (message wording)" not in f.confidence_reason
