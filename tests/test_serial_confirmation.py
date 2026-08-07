"""EDGEVERDICT_SERIAL_CONFIRM_TESTS_V1

Batch-found confirmed_gaps must survive isolation before keeping the label.
Born from the supabase studio run: 12 batch gaps, 10 of them shared-state
artifacts of the one-file batch design, 2 real. The confirmation pass
re-gates batch gaps through the serial path; serial-fallback gaps are
already isolated and are never re-run.
"""

from __future__ import annotations

from edgeverdict.review import ReviewFinding, ReviewRun
from edgeverdict.verifiers.finding_verifier import FindingVerifier


def _fv(script, logged):
    """A FindingVerifier shell: only what _confirm_batch_gaps touches."""
    fv = object.__new__(FindingVerifier)
    fv.log = logged.append
    calls = []

    def fake_classify(f):
        calls.append(f.behavior)
        status, observed = script[f.behavior]
        f.status = status
        f.observed = observed
        return f

    fv.classify = fake_classify
    fv._calls = calls
    return fv


def _run_with(findings):
    r = object.__new__(ReviewRun)
    r.findings = findings
    return r


def _gap(behavior, observed="batch says broken"):
    f = ReviewFinding(behavior=behavior)
    f.status = "confirmed_gap"
    f.observed = observed
    return f


def test_surviving_gap_keeps_label_and_gains_confirmation_note(monkeypatch):
    monkeypatch.delenv("EDGEVERDICT_SERIAL_CONFIRM", raising=False)
    logged = []
    fv = _fv({"real gap": ("confirmed_gap", "isolated failure msg")}, logged)
    review = _run_with([_gap("real gap")])
    fv._confirm_batch_gaps(review, leftover=set())
    f = review.findings[0]
    assert f.status == "confirmed_gap"
    assert f.observed.endswith("[serially confirmed in isolation]")
    assert "isolated failure msg" in f.observed
    assert any("1 confirmed, 0 batch artifact(s)" in ln for ln in logged)


def test_artifact_gap_is_relabeled_with_honest_note(monkeypatch):
    monkeypatch.delenv("EDGEVERDICT_SERIAL_CONFIRM", raising=False)
    logged = []
    fv = _fv({"pollution gap": ("handled", "")}, logged)
    review = _run_with([_gap("pollution gap", observed="expected 2 got 1")])
    fv._confirm_batch_gaps(review, leftover=set())
    f = review.findings[0]
    assert f.status == "handled"
    assert "shared-state artifact" in f.observed
    assert "expected 2 got 1" in f.observed  # batch evidence preserved
    assert any("0 confirmed, 1 batch artifact(s)" in ln for ln in logged)


def test_serial_fallback_gaps_are_not_rerun(monkeypatch):
    monkeypatch.delenv("EDGEVERDICT_SERIAL_CONFIRM", raising=False)
    logged = []
    fv = _fv({"batch gap": ("confirmed_gap", "x")}, logged)
    serial_gap = _gap("already isolated")
    batch_gap = _gap("batch gap")
    review = _run_with([serial_gap, batch_gap])
    fv._confirm_batch_gaps(review, leftover={0})
    assert fv._calls == ["batch gap"]
    assert serial_gap.observed == "batch says broken"  # untouched


def test_non_gap_statuses_are_untouched(monkeypatch):
    monkeypatch.delenv("EDGEVERDICT_SERIAL_CONFIRM", raising=False)
    logged = []
    fv = _fv({}, logged)
    handled = ReviewFinding(behavior="fine")
    handled.status = "handled"
    review = _run_with([handled])
    fv._confirm_batch_gaps(review, leftover=set())
    assert fv._calls == []
    assert logged == []  # nothing to confirm, nothing logged


def test_opt_out_env_skips_everything(monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_SERIAL_CONFIRM", "0")
    logged = []
    fv = _fv({"gap": ("handled", "")}, logged)
    review = _run_with([_gap("gap")])
    fv._confirm_batch_gaps(review, leftover=set())
    assert fv._calls == []
    assert review.findings[0].status == "confirmed_gap"
