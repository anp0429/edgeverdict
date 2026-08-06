"""The review record and its board.

A reviewer agent doesn't just accept/reject — it produces an audit trail:
for each behavior the intent implies, was it already covered? if not, a test
was written and run; what did it show? This module holds that record and renders
it as a single self-contained HTML page so a human can read exactly what the
loop did and why — the documented review IS the product, not just the fix.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Literal

Axis = Literal["correctness", "consistency", "coverage"]
Status = Literal[
    "skipped_covered", "handled", "confirmed_gap", "broken_test", "timed_out",
    "untested_boundary", "pending"
]


@dataclass
class ReviewFinding:
    behavior: str                      # the intended behavior, derived from the intent
    axis: Axis = "correctness"
    covered_by_existing: bool = False  # agent's read: already tested?
    coverage_note: str = ""            # why it thinks so (which test, or "none found")
    test_path: str | None = None       # the test it wrote (if uncovered)
    test_code: str | None = None
    source_file: str = ""              # which target file this finding is about (multi-target)
    status: Status = "pending"         # set by the FindingVerifier after running
    observed: str = ""                 # what the run showed (assertion msg / output / error)
    # advisory precision layer (GapAuditor) — NEVER changes status, triage only
    audit: str = ""                    # "likely_real" | "likely_false_positive" | "uncertain"
    audit_reason: str = ""
    audit_evidence: str = ""
    # deterministic precision layer: set when this gap's failure message is
    # shared verbatim by several other gaps in the same run (see
    # flag_systematic_artifacts). Advisory — NEVER changes status.
    artifact_note: str = ""
    # deterministic brittleness lint (assertion_lint) — flags gap-deciding
    # assertions with shapes that historically produced false positives
    # (short-needle substrings, format opinions). Advisory — NEVER changes
    # status.
    lint_note: str = ""
    # deterministic confidence tier (classify_gap_confidence) — "high" for a
    # gap whose failure is objective wrongness (an exception RAISED, a crash,
    # a type error: no design opinion can make it correct), "low" for a value
    # mismatch that may be the proposer disagreeing with an intended design
    # choice. Advisory — NEVER changes status; the gate does not lie about
    # whether the test failed, only annotates how much a human should trust
    # that the failure represents a real bug.
    confidence: str = ""               # "" | "high" | "low"
    confidence_reason: str = ""
    # static blast radius (edgeverdict.blast) — module-level reach of the
    # target file, computed from the repo's own import edges, no model, no
    # tokens. Impact axis of the triage matrix. Advisory — NEVER changes
    # status.
    blast: str = ""                    # "" | "wide" | "moderate" | "narrow"
    blast_note: str = ""
    # set when a broken proposal was repaired (one bounded round) and
    # re-executed; its status is whatever the SECOND run earned
    repaired: bool = False
    # fix stage (TransitionVerifier is the judge — red->green->no-regression)
    fix_status: str = ""               # "" | "fix_verified" | "fix_rejected" | "fix_not_attempted"
    fix_note: str = ""                 # verifier's reason (or agent's failure to propose)
    fix_change: str = ""               # human-readable summary of the applied edit


@dataclass
class ReviewRun:
    intent: str
    target: str
    findings: list[ReviewFinding] = field(default_factory=list)
    # set when the environment itself failed (install/build/smoke/fidelity):
    # a run-level fact, rendered ONCE as a banner — never as per-finding noise
    env_error: str = ""
    # per-target blast detail (edgeverdict.blast.BlastDetail), populated by the
    # api layer; drives the blast pane. Empty list -> no pane (back-compat).
    blast_details: list = field(default_factory=list)

    @property
    def gaps(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.status == "confirmed_gap"]


def flag_systematic_artifacts(findings: list[ReviewFinding],
                              threshold: int = 3) -> list[str]:
    """Deterministic FP heuristic: N confirmed gaps failing with the VERBATIM
    same message are one shared cause, not N independent bugs.

    Found on supabase/mcp#324: nine "confirmed gaps" all read "Target cannot
    be null or undefined." — every generated test unwrapped the tool response
    with the wrong shape, so nine well-formed assertions hit the same null.
    Real gaps fail in their own words; artifacts fail in unison. The status
    is untouched (the tests DID execute and fail — the gate does not lie);
    each member gets an artifact_note, and the returned warnings are for the
    run-level banner. Grouping is by the first line of `observed`, exact —
    normalizing further (stripping numbers etc.) would merge genuinely
    distinct assertion failures. Purely mechanical: no model in the loop."""
    groups: dict[str, list[ReviewFinding]] = {}
    for f in findings:
        if f.status != "confirmed_gap" or not f.observed.strip():
            continue
        key = f.observed.strip().splitlines()[0]
        groups.setdefault(key, []).append(f)
    warnings: list[str] = []
    for key, members in groups.items():
        if len(members) < threshold:
            continue
        for f in members:
            f.artifact_note = (
                f"identical failure shared by {len(members)} gaps — "
                "suspected setup/unwrapping artifact, one cause"
            )
        warnings.append(
            f"{len(members)} confirmed gaps share one verbatim failure "
            f"({key[:90]!r}) — that is one shared cause (test setup, response "
            "unwrapping, or scope), not independent bugs. Verify the shared "
            "cause before treating any of them as real."
        )
    return warnings


# Failure signatures that mean the test RAISED rather than asserted a value.
# An exception reaching the top is objective wrongness: no design opinion
# renders a crash correct. Substring match on the first observed line is
# deliberate — pytest prints "SomeError" / "raised" / a traceback header for
# these, and a plain "AssertionError: x != y" for a value mismatch.
_OBJECTIVE_FAILURE_MARKERS = (
    "Error:",          # TypeError:, ValueError:, KeyError:, custom *Error:
    "Exception",
    "Traceback",
    "not raised",      # "InconclusiveMatchError not raised" — expected to
                       # raise and didn't: a contract break, not a value opinion
    "DID NOT RAISE",   # pytest.raises' own wording for the same thing
    "unexpectedly raised",
    "segmentation",
    "RecursionError",
    "hang",
)

# "AssertionError:" ends in "Error:" and would wrongly match the objective set;
# a bare assertion failure is the CANONICAL judgment signal. Exclude it first.
_ASSERTION_PREFIXES = ("AssertionError", "assert ", "Failed: assert")

# vitest/jest phrase value mismatches as MATCHER failures, not "x != y", and
# they frequently lead with "Error:" or "AssertionError:" — so the objective
# substring set would wrongly claim them as crashes. These markers, ANYWHERE
# in the message, mean "a matcher compared two values and they differed" =
# judgment, same class as pytest's "AssertionError: a != b". A genuine thrown
# error in vitest has a stack/type but none of these matcher tokens.
_JS_MATCHER_MARKERS = (
    "expect(",          # expect(received).toBe(...)
    " to be ",          # "expected 3 to be 4"
    " to equal ",
    " to deeply equal ",
    " to match ",
    " to contain ",
    ".toBe", ".toEqual", ".toMatch", ".toContain", ".toThrow",
    "expected ", "received ",
)

# A bare value-mismatch assertion: "AssertionError: a != b", "assert x == y".
# This is where design-intent disagreements live (dateparser YDM 05->2005,
# posthog None->False): the code did something coherent, the proposer wanted
# something else. Not wrong on its face — needs a human or the code graph.
_JUDGMENT_FAILURE_MARKERS = (
    "AssertionError",
    "assert ",
    "!=",
    "==",
    " is not ",
    " is None",
)


_WORDING_ASSERTION_MARKERS = (
    "assertnotin", "assertin",
    "not.tocontain", "tocontain",
    "unexpectedly found in", "not found in",
    "to contain", "not to contain",
)
_MESSAGE_HINT = ("message", "error", "log", "warning", "text", "please",
                 "invalid", "verify")
# SQL / query-language markers: an assertion on a string containing these is
# checking QUERY CORRECTNESS (clause shape, escaping), not message wording. A
# bug there is a real defect, so these strings are never tiered down as
# wording preference. Learned on supabase/mcp #284: an ILIKE wildcard-escaping
# gap was mis-tagged because `event_message`/`storage_logs` contain hint words.
_QUERY_MARKERS = (
    "select ", "insert ", "update ", "delete ", "where ", "from ",
    "ilike", " like ", "join ", "order by", "group by", "values ",
)


def _is_wording_preference(observed: str) -> bool:
    """True when the failing assertion is about the WORDING of a message
    string, not a computed value — a specification-disagreement tell.
    Negative string assertions (assertNotIn / not.toContain) on a message
    frequently forbid a phrase the code legitimately includes.

    Guards against two mis-tags:
      - a value-membership check (assertIn on a list of ids) is not wording;
      - a SQL / query string is a CORRECTNESS check, not message wording —
        an escaping or clause bug there is a real defect (the #284 ILIKE
        wildcard-escaping case), so query strings are excluded even though
        column names like `event_message` contain message-hint words."""
    low = (observed or "").lower()
    body = low.split(":", 1)[1] if ":" in low else low
    if not any(m in body for m in _WORDING_ASSERTION_MARKERS):
        return False
    # SQL / query context -> correctness, not wording. Exclude.
    if any(kw in body for kw in _QUERY_MARKERS):
        return False
    has_prose = bool(re.search(r"['\"][^'\"]*\s+[^'\"]*['\"]", body))
    return has_prose and any(h in body for h in _MESSAGE_HINT)


def classify_gap_confidence(findings: list[ReviewFinding]) -> None:
    """Deterministic confidence tiering for confirmed gaps. Sets each gap's
    `confidence` to "high" or "low" and a one-line reason. NEVER changes
    status — the test executed and failed; this only annotates how much a
    human should trust that the failure is a real bug versus a disagreement
    with an intended design choice.

    Learned across seven hand-verified false positives (marshmallow #3005,
    humanize #356, deepagents #5241, two posthog-python, two dateparser):
    EVERY one was a value-mismatch assertion where the proposer invented a
    spec the library never promised — None should raise, YDM's leading number
    is the day, arrays evaluate element-wise. And on the one clean pure-logic
    run (tenacity cyclic __cause__), the proposer produced ZERO gaps: given a
    single correct answer, it does not manufacture disagreement. Meanwhile the
    two REAL finds (supabase #317 cartesian product, zod #6212 __proto__
    crash) were objective wrongness — a wrong value produced without opinion,
    and a crash. So the failure SHAPE is the signal: raised/crashed = high
    (objective), value-mismatch = low (possible design disagreement).

    Three inputs, all already on the finding, no model in the loop:
      - the failure text (`observed`) — raised vs asserted;
      - the advisory auditor (`audit`) — likely_false_positive pulls down;
      - the brittleness lint and artifact note — either pulls down.
    High requires objective failure AND no countervailing advisory flag; every
    other confirmed gap is low, because the default posture toward a value
    mismatch is distrust until a human (or, later, the code graph) clears it.
    """
    for f in findings:
        if f.status != "confirmed_gap":
            continue
        first = (f.observed or "").strip().splitlines()
        line = first[0] if first else ""

        # A plain assertion failure is judgment even though "AssertionError"
        # ends in "Error:" — check it FIRST so the objective substring set
        # can't capture it. "DID NOT RAISE" / "not raised" are the exception:
        # a missing-raise is an objective contract break, not a value opinion.
        is_assertion = line.startswith(_ASSERTION_PREFIXES)
        if _is_wording_preference(f.observed):
            f.confidence = "low"
            f.confidence_reason = (
                "specification disagreement (message wording), not a defect: "
                "the test asserts a phrase should appear in or be absent from "
                "a human-readable message. Ask whether a user would observe "
                "breakage if the wording stayed as the code has it — usually "
                "not. Needs a contract (PR text, issue criteria, adjacent "
                "test, or docs) to be a gap; otherwise a hypothesis"
            )
            if f.audit == "likely_false_positive":
                f.confidence_reason += "; auditor: likely false positive"
            continue
        # a vitest/jest matcher failure is a value mismatch even though it may
        # say "Error:" — check the whole first line, not just the prefix.
        is_js_matcher = any(m in line for m in _JS_MATCHER_MARKERS)
        # MISSING-RAISE ("X not raised" / "DID NOT RAISE" / failed .toThrow)
        # is NOT unconditionally objective. Learned the hard way on the
        # posthog None case: the classifier tiered "InconclusiveMatchError
        # not raised" HIGH/file-ready, and hand-verification had already
        # proven it a false positive — the raise expectation was itself the
        # proposer's invented contract (the code returns False for None BY
        # DESIGN, NONE_VALUES_ALLOWED_OPERATORS = ["is_not"]). A missing
        # raise is only objective when the change's INTENT promised a raise,
        # which a text heuristic cannot see. Default posture = distrust:
        # tier LOW with its own reason; the behavior_surface (Part C) or a
        # human clears it. Genuinely RAISED exceptions stay high.
        js_missing_throw = ("toThrow" in line or " to throw " in line) and (
            "not" in line.lower() or "did not" in line.lower()
            or "no error" in line.lower()
        )
        missing_raise = (
            "not raised" in line or "DID NOT RAISE" in line or js_missing_throw
        )
        raised = (
            (not missing_raise)
            and (not is_assertion) and (not is_js_matcher)
            and any(m in line for m in _OBJECTIVE_FAILURE_MARKERS)
        )

        flagged = (
            f.audit == "likely_false_positive"
            or bool(f.artifact_note)
            or bool(f.lint_note)
        )

        if missing_raise:
            f.confidence = "low"
            f.confidence_reason = (
                "missing-raise: the raise expectation may itself be the "
                "proposer's invented contract — objective only if the "
                "change's intent promises a raise; verify against intent "
                "and source"
            )
            if f.audit == "likely_false_positive":
                f.confidence_reason += "; auditor: likely false positive"
            continue
        if raised and not flagged:
            f.confidence = "high"
            f.confidence_reason = (
                "objective failure (exception/crash) with no false-positive "
                "flag — no design choice makes this correct"
            )
        elif raised and flagged:
            f.confidence = "low"
            f.confidence_reason = (
                "objective failure, but an advisory layer flagged it "
                "(auditor/artifact/lint) — verify before trusting"
            )
        else:
            f.confidence = "low"
            bits = ["value-mismatch assertion — may be a design-intent "
                    "disagreement, not a bug"]
            if f.audit == "likely_false_positive":
                bits.append("auditor: likely false positive")
            if f.artifact_note:
                bits.append("shared-cause artifact")
            if f.lint_note:
                bits.append("brittle assertion shape")
            f.confidence_reason = "; ".join(bits)


_STATUS_LABEL = {
    "confirmed_gap": ("gap", "#a32d2d", "#f09595"),
    "handled": ("handled", "#1f7a4d", "#5dcaa5"),
    "skipped_covered": ("already covered", "#76756e", "#c9c8c2"),
    "broken_test": ("test didn't run", "#8a6d1b", "#e0c060"),
    "untested_boundary": ("untested boundary — human call", "#8a6d1b", "#e0c060"),
    "timed_out": ("timed out — human call", "#5a5a8f", "#a9a9d6"),
    "pending": ("not yet run", "#76756e", "#c9c8c2"),
}

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
       background:#f6f6f4; color:#26251f; }
@media (prefers-color-scheme: dark){ body{ background:#1c1c1a; color:#e8e6df; } }
header { padding:20px 24px; border-bottom:1px solid #d9d8d2; }
h1 { font-size:18px; font-weight:600; margin:0; }
.sub { color:#76756e; font-size:13px; margin-top:6px; white-space:pre-wrap; max-width:900px; }
.summary { padding:14px 24px; font-size:14px; border-bottom:1px solid #d9d8d2; }
.blast { padding:14px 24px; border-bottom:1px solid #d9d8d2; }
.btitle { font-size:13px; font-weight:600; color:#44443f; margin-bottom:10px; }
.bcard { border:1px solid #e0dfd8; border-radius:10px; padding:12px 14px; margin-bottom:10px; }
.bhead { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
.btgt { font-family:ui-monospace,SFMono-Regular,monospace; font-size:13px; color:#33332f; }
.btier { color:#fff; font-size:11px; font-weight:600; padding:2px 9px; border-radius:20px; }
.btrunc { font-size:12px; color:#a32d2d; margin-bottom:8px; }
.bg { display:flex; flex-wrap:wrap; align-items:baseline; gap:6px; margin:6px 0; }
.bl { font-size:12px; color:#76756e; min-width:120px; }
.bn { font-size:12px; color:#a3a29c; font-style:italic; }
.chips { display:inline-flex; flex-wrap:wrap; gap:5px; }
.chip { font-family:ui-monospace,SFMono-Regular,monospace; font-size:11px; background:#f0efe9; color:#44443f; padding:2px 7px; border-radius:5px; }
.bmore { display:inline; }
.bmore summary { display:inline; font-size:11px; color:#5a76a8; cursor:pointer; margin-left:4px; }
.bmore .chips { margin-top:6px; }
@media (prefers-color-scheme: dark){ .chip{ background:#26261f; color:#c9c8be; } .btrunc{ color:#f09595; } }
.envfail { padding:14px 24px; font-size:14px; font-weight:600; color:#fff;
           background:#a32d2d; }
.envfail .detail { font-weight:400; font-size:13px; margin-top:4px;
                   font-family:ui-monospace,monospace; }
.summary b { font-weight:600; }
.wrap { padding:20px 24px; max-width:920px; }
.card { border-radius:12px; padding:14px 16px; margin-bottom:12px; border:1px solid #d9d8d2;
        border-left-width:4px; background:#fff; }
@media (prefers-color-scheme: dark){ .card{ background:#26261f; border-color:#3a3a36; } }
.behavior { font-weight:600; font-size:15px; }
.meta { font-size:12px; color:#76756e; margin-top:3px; }
.badge { display:inline-block; font-size:11px; font-weight:600; padding:2px 9px;
         border-radius:20px; color:#fff; margin-left:8px; vertical-align:middle; }
.observed { font-size:13px; margin-top:8px; }
details { margin-top:8px; }
summary { font-size:12px; color:#5a76a8; cursor:pointer; }
pre { background:#f0efe9; border-radius:8px; padding:8px 10px; margin:6px 0 0;
      font:12px/1.45 ui-monospace,SFMono-Regular,monospace; white-space:pre-wrap;
      word-break:break-word; }
@media (prefers-color-scheme: dark){ pre{ background:#1f1f1b; } }
.verify { margin-top:10px; font-size:12px; font-weight:600; color:#a32d2d; }
@media (prefers-color-scheme: dark){ .verify{ color:#f09595; } }
pre.gap-test { border:1px solid #e0b4b4; max-height:340px; overflow:auto; }
@media (prefers-color-scheme: dark){ pre.gap-test{ border-color:#5a2d2d; } }
.audit { margin-top:10px; padding:8px 10px; border-radius:8px; border:1px solid #ccc;
         border-left-width:4px; font-size:12px; background:#faf8f2; }
@media (prefers-color-scheme: dark){ .audit{ background:#232019; } }
.audit .ev { color:#76756e; margin-top:4px; font-family:ui-monospace,monospace; }
"""


_BLAST_INLINE = 8  # importers shown before the rest fold behind "+N more"


def _blast_chip_group(label: str, items: list[str]) -> str:
    """One labeled group of importer chips. Up to _BLAST_INLINE render
    inline; the remainder fold behind a <details>+N more</details> so the
    board NEVER hides scope (collapse, never cap) while staying readable.
    Every path stays in the document — searchable and complete."""
    if not items:
        return (f'<div class="bg"><span class="bl">{html.escape(label)}</span>'
                f'<span class="bn">none</span></div>')
    inline = items[:_BLAST_INLINE]
    rest = items[_BLAST_INLINE:]
    chips = "".join(f'<span class="chip">{html.escape(i)}</span>' for i in inline)
    more = ""
    if rest:
        rest_chips = "".join(
            f'<span class="chip">{html.escape(i)}</span>' for i in rest)
        more = (f'<details class="bmore"><summary>+{len(rest)} more</summary>'
                f'<div class="chips">{rest_chips}</div></details>')
    return (f'<div class="bg"><span class="bl">{html.escape(label)} '
            f'({len(items)})</span>'
            f'<span class="chips">{chips}</span>{more}</div>')


def _blast_pane(run: ReviewRun) -> str:
    """The blast-radius pane: one card per target with a tier badge and
    three importer groups (direct / one-hop / test files). Sits between the
    summary and the finding cards. No details -> empty string (a run with no
    blast info renders exactly as before)."""
    details = getattr(run, "blast_details", None) or []
    if not details:
        return ""
    _tier_color = {"wide": "#b9770e", "moderate": "#5a76a8", "narrow": "#76756e"}
    cards = []
    for d in details:
        tier = getattr(d, "tier", "") or ""
        color = _tier_color.get(tier, "#76756e")
        target = getattr(d, "target", "") or ""
        trunc = ""
        if getattr(d, "truncated", False):
            trunc = ('<div class="btrunc">scan truncated at cap — importer '
                     'lists are a lower bound</div>')
        groups = (
            _blast_chip_group("direct importers", list(getattr(d, "direct", [])))
            + _blast_chip_group("one-hop importers",
                                list(getattr(d, "transitive", [])))
            + _blast_chip_group("test files",
                                list(getattr(d, "test_importers", [])))
        )
        cards.append(
            f'<div class="bcard">'
            f'<div class="bhead"><span class="btgt">{html.escape(target)}</span>'
            f'<span class="btier" style="background:{color}">{html.escape(tier)}'
            f' blast</span></div>{trunc}{groups}</div>'
        )
    return f'<div class="blast"><div class="btitle">Blast radius</div>' \
           f'{"".join(cards)}</div>'


def render_review_html(run: ReviewRun, path: str) -> str:
    gaps = sum(1 for f in run.findings if f.status == "confirmed_gap")
    covered = sum(1 for f in run.findings if f.status in ("skipped_covered", "handled"))
    broken = sum(1 for f in run.findings if f.status == "broken_test")
    timed = sum(1 for f in run.findings if f.status == "timed_out")

    cards = []
    for f in run.findings:
        label, text_c, border_c = _STATUS_LABEL.get(f.status, _STATUS_LABEL["pending"])
        badge = f'<span class="badge" style="background:{text_c}">{label}</span>'
        axis = f'<span class="meta">axis: {f.axis}</span>'
        cov = (f"already covered — {html.escape(f.coverage_note)}"
               if f.covered_by_existing else
               "not covered by existing tests" + (f" — {html.escape(f.coverage_note)}" if f.coverage_note else ""))
        extra = ""
        if f.status == "confirmed_gap" and f.blast:
            _bc = {"wide": "#e0c060", "moderate": "#c9c8c2",
                   "narrow": "#76756e"}.get(f.blast, "#76756e")
            extra += (
                f'<div class="audit" style="border-color:{_bc}">'
                f'<b style="color:{_bc}">blast: {f.blast}</b>'
                + (f' — {html.escape(f.blast_note)}' if f.blast_note else "")
                + '</div>'
            )
        if f.status == "confirmed_gap" and f.confidence:
            _cc = "#f09595" if f.confidence == "low" else "#5dcaa5"
            _clabel = ("LOW confidence — possible design-intent disagreement, "
                       "verify against source"
                       if f.confidence == "low"
                       else "HIGH confidence — objective failure (exception/crash)")
            extra += (
                f'<div class="audit" style="border-color:{_cc}">'
                f'<b style="color:{_cc}">{_clabel}</b>'
                + (f' — {html.escape(f.confidence_reason)}' if f.confidence_reason else "")
                + '</div>'
            )
        if f.observed:
            extra += f'<div class="observed"><b>Observed:</b> {html.escape(f.observed)}</div>'
        if f.fix_status:
            fx_label, fx_color = {
                "fix_verified": ("FIX VERIFIED (red->green, no regression)", "#1f7a4d"),
                "fix_rejected": ("fix rejected by gate", "#8a6d1b"),
                "fix_not_attempted": ("no fix proposed", "#76756e"),
            }.get(f.fix_status, (f.fix_status, "#76756e"))
            extra += (
                f'<div class="audit" style="border-color:{fx_color}">'
                f'<b style="color:{fx_color}">{fx_label}</b>'
                + (f' — {html.escape(f.fix_note)}' if f.fix_note else '')
                + (f'<div class="ev">{html.escape(f.fix_change)}</div>' if f.fix_change else '')
                + '</div>'
            )
        if f.test_code:
            if f.status == "confirmed_gap":
                # advisory precision flag from the GapAuditor (does NOT change status)
                if f.audit:
                    a_label = {"likely_real": ("likely REAL", "#a32d2d"),
                               "likely_false_positive": ("likely FALSE POSITIVE", "#8a6d1b"),
                               "uncertain": ("uncertain", "#76756e")}.get(f.audit, ("", "#76756e"))
                    extra += (
                        f'<div class="audit" style="border-color:{a_label[1]}">'
                        f'<b style="color:{a_label[1]}">Auditor: {a_label[0]}</b>'
                        + (f' — {html.escape(f.audit_reason)}' if f.audit_reason else '')
                        + (f'<div class="ev">evidence: {html.escape(f.audit_evidence)}</div>' if f.audit_evidence else '')
                        + '</div>'
                    )
                extra += (
                    '<div class="verify">Verify this before trusting it: is the '
                    'assertion correct, or did the agent assert the wrong thing?</div>'
                    f'<pre class="gap-test">{html.escape(f.test_code)}</pre>'
                )
            else:
                extra += (f'<details><summary>test the agent wrote</summary>'
                          f'<pre>{html.escape(f.test_code)}</pre></details>')
        cards.append(
            f'<div class="card" style="border-left-color:{border_c}">'
            f'<div class="behavior">{html.escape(f.behavior)}{badge}</div>'
            f'<div class="meta">{cov}</div>{axis}{extra}</div>'
        )

    banner = ""
    if run.env_error:
        banner = (
            '<div class="envfail">Environment preparation failed — '
            'no verdicts were issued.'
            f'<div class="detail">{html.escape(run.env_error)}</div></div>'
        )
    doc = (
        f"<!doctype html><meta charset=utf-8><title>edgeverdict review</title>"
        f"<style>{_CSS}</style>"
        f'<header><h1>edgeverdict — review</h1>'
        f'<div class="sub">Target: {html.escape(run.target)}\n\nIntent: {html.escape(run.intent[:400])}</div></header>'
        f'{banner}'
        f'<div class="summary"><b>{gaps}</b> confirmed gap(s) · '
        f'<b>{covered}</b> covered/handled · <b>{broken}</b> test didn\'t run · '
        f'<b>{timed}</b> timed out · '
        f'<b>{len(run.findings)}</b> behaviors reviewed</div>'
        f'{_blast_pane(run)}'
        f'<div class="wrap">{"".join(cards)}</div>'
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path