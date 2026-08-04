# EDGEVERDICT_CONFIDENCE_TIERING_TESTS_V1
"""Confidence tiering: objective failures (raised/crashed) are high-confidence,
value-mismatch assertions are low (possible design-intent disagreement).
Advisory only — status is never touched. Fixtures replay the real Aug-3
hand-verified cases so the classifier is pinned to actual outcomes, not a
toy model of them."""
from __future__ import annotations

from edgeverdict.review import ReviewFinding, classify_gap_confidence


def _gap(observed, audit="", artifact_note="", lint_note=""):
    return ReviewFinding(
        behavior="b", status="confirmed_gap", observed=observed,
        audit=audit, artifact_note=artifact_note, lint_note=lint_note,
    )


def test_dateparser_ydm_value_mismatch_is_low():
    # real: "AssertionError: datetime(2005,3,20) != datetime(2018,3,5)"
    f = _gap("AssertionError: datetime.datetime(2005, 3, 20, 0, 0) != "
             "datetime.datetime(2018, 3, 5, 0, 0)", audit="likely_false_positive")
    classify_gap_confidence([f])
    assert f.confidence == "low"
    assert f.status == "confirmed_gap"  # never changed


def test_posthog_none_missing_raise_is_low_even_unflagged():
    # THE validation-2 regression, pinned: "InconclusiveMatchError not
    # raised" tiered HIGH/file-ready on a hand-verified false positive
    # (the auditor said only "uncertain" — truncated source — so no flag
    # pulled it down). Missing-raise is low BY DEFAULT now: the raise
    # expectation may be the proposer's own invented contract.
    f = _gap("AssertionError: InconclusiveMatchError not raised",
             audit="uncertain")
    classify_gap_confidence([f])
    assert f.confidence == "low"
    assert "invented contract" in f.confidence_reason


def test_missing_raise_flagged_keeps_auditor_note():
    f = _gap("AssertionError: InconclusiveMatchError not raised",
             audit="likely_false_positive")
    classify_gap_confidence([f])
    assert f.confidence == "low"
    assert "likely false positive" in f.confidence_reason


def test_raised_exception_unflagged_is_high():
    # supabase-shape objective wrongness: a real crash, no advisory flag.
    f = _gap("TypeError: unhashable type: 'dict'")
    classify_gap_confidence([f])
    assert f.confidence == "high"
    assert "objective" in f.confidence_reason


def test_proto_crash_is_high():
    # zod #6212 shape: a raised error from a bad path element.
    f = _gap("RangeError: Maximum call stack exceeded")
    classify_gap_confidence([f])
    assert f.confidence == "high"


def test_bare_value_mismatch_no_flag_is_still_low():
    # default posture toward a value mismatch is distrust, even with no
    # auditor flag — a human (or the code graph) must clear it.
    f = _gap("AssertionError: 3 != 4")
    classify_gap_confidence([f])
    assert f.confidence == "low"
    assert "design-intent" in f.confidence_reason


def test_pytest_did_not_raise_is_low():
    # conservative by evidence: the only real-world missing-raise seen so
    # far was a false positive. A text heuristic cannot tell a promised
    # raise from an invented one — Part C or a human clears it.
    f = _gap("Failed: DID NOT RAISE <class 'ValueError'>")
    classify_gap_confidence([f])
    assert f.confidence == "low"


def test_lint_flag_pulls_objective_down():
    f = _gap("KeyError: 'x'", lint_note="brittle 3-char needle")
    classify_gap_confidence([f])
    assert f.confidence == "low"


def test_artifact_note_pulls_down():
    f = _gap("ValueError: bad", artifact_note="shared by 5 gaps")
    classify_gap_confidence([f])
    assert f.confidence == "low"


def test_non_gap_findings_untouched():
    h = ReviewFinding(behavior="b", status="handled", observed="")
    s = ReviewFinding(behavior="b", status="skipped_covered", observed="")
    classify_gap_confidence([h, s])
    assert h.confidence == "" and s.confidence == ""


def test_vitest_matcher_mismatch_is_low():
    # vitest value mismatch — leads with "AssertionError:" but is a matcher
    # comparison, must be LOW not misread as objective via the "Error:" tail.
    f = _gap("AssertionError: expected 3 to be 4 // Object.is equality")
    classify_gap_confidence([f])
    assert f.confidence == "low"


def test_vitest_expect_received_form_is_low():
    f = _gap("Error: expect(received).toEqual(expected)\n\nExpected: 5\nReceived: 6")
    classify_gap_confidence([f])
    assert f.confidence == "low"


def test_vitest_toBe_mismatch_is_low():
    f = _gap("AssertionError: expected undefined to be 'x'")
    classify_gap_confidence([f])
    assert f.confidence == "low"


def test_vitest_real_thrown_error_is_high():
    # a genuine runtime throw in JS — no matcher tokens, objective.
    f = _gap("TypeError: Cannot read properties of undefined (reading 'id')")
    classify_gap_confidence([f])
    assert f.confidence == "high"


def test_vitest_missing_throw_is_low():
    # JS mirror of the missing-raise rule: low by default, same reasoning.
    f = _gap("AssertionError: expected function to throw an error but it did not")
    classify_gap_confidence([f])
    assert f.confidence == "low"
    assert "invented contract" in f.confidence_reason


def test_status_is_never_mutated():
    f = _gap("AssertionError: a != b")
    before = f.status
    classify_gap_confidence([f])
    assert f.status == before == "confirmed_gap"
