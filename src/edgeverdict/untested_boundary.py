"""Untested-boundary detection: a PR that changes a data-layer boundary
(database / cache / queue / external client) and ships NO test reaching that
change is itself a gap — the absence of coverage IS the finding, and it needs
no infrastructure to observe.

This is the complement of setup_surface. setup_surface helps the proposer
WRITE a boundary test using the author's doubles; this notices when the
author wrote NONE. Both operate purely on static facts — no db, no run.

CONSERVATISM IS THE WHOLE DESIGN. A false "you didn't test this" is a new
false positive, exactly what edgeverdict exists NOT to produce. So this fires
only when BOTH halves are confidently true:

  1. the DIFF touched a boundary — the changed hunks import or call a
     data-layer symbol (a curated, conservative signal set), AND
  2. NO test reaches the changed file — blast's test_importers is empty for
     the target.

If either is uncertain, it stays silent. Indirect coverage (a test that
exercises the boundary one layer up) shows up as a non-empty test_importers,
so this will NOT fire when such a test exists — it under-claims by design.
The finding it emits is advisory and clearly a COVERAGE observation, never a
counterfeit execution verdict; a human confirms whether the untested change
is real or intentional (a trivial passthrough, a typing-only change).
"""

from __future__ import annotations

import re

# Conservative boundary signals: dotted/callable names that strongly imply a
# data-layer interaction. Deliberately NOT exhaustive — a miss (staying
# silent) is cheap; a false fire is not. Matched against the ADDED lines of
# the diff only.
_BOUNDARY_SIGNALS = (
    # sql / db
    r"\.execute\(", r"\.executemany\(", r"\.fetchone\(", r"\.fetchall\(",
    r"\.commit\(", r"\.cursor\(", r"\bsession\.query\b", r"\.begin\(",
    r"\bSELECT\b", r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b",
    r"\bpsycopg2\b", r"\basyncpg\b", r"\bsqlalchemy\b", r"\bclickhouse\b",
    # cache
    r"\bredis\b", r"\bmemcache", r"\.setex\(", r"\.hset\(", r"\.expire\(",
    # queue
    r"\bkafka\b", r"\bcelery\b", r"\.publish\(", r"\.enqueue\(",
    r"\.produce\(", r"\.send_message\b",
    # external http client (a boundary a unit test should mock)
    r"requests\.(get|post|put|delete)\(", r"httpx\.(get|post|Client)\b",
    r"\.request\(",
)
_BOUNDARY_RE = re.compile("|".join(_BOUNDARY_SIGNALS))


def _added_lines(change: str) -> str:
    """The added ('+') lines of a unified diff, joined. Removed and context
    lines are ignored — we care what the PR now DOES, not what it removed.
    The '+++' file header is excluded."""
    out = []
    for line in change.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return "\n".join(out)


def boundary_touched(change: str) -> list[str]:
    """The distinct boundary signals the diff's added lines match. Empty ->
    the diff shows no data-layer interaction (stay silent)."""
    added = _added_lines(change)
    hits: list[str] = []
    seen: set[str] = set()
    for m in _BOUNDARY_RE.finditer(added):
        tok = m.group(0)
        if tok not in seen:
            seen.add(tok)
            hits.append(tok)
    return hits


def untested_boundary_finding(change: str, target_rel: str,
                              test_importers: list[str]):
    """A ReviewFinding when a data-layer change ships no reaching test, else
    None. CONSERVATIVE: requires a boundary signal in the added lines AND an
    empty test_importers set. Returns a finding whose status is a coverage
    observation the human adjudicates — it is NOT an execution-earned
    confirmed_gap and never counts as one in the run tally.

    Imported lazily-friendly: returns a plain ReviewFinding so callers keep
    their own import of the type."""
    from .review import ReviewFinding

    signals = boundary_touched(change or "")
    if not signals:
        return None                     # no boundary interaction — silent
    # a non-test file that IS reached by at least one test is (indirectly)
    # covered — under-claim, stay silent.
    reaching = [t for t in (test_importers or []) if t.strip()]
    if reaching:
        return None

    shown = ", ".join(signals[:6])
    f = ReviewFinding(
        behavior=(f"The change to {target_rel} touches a data-layer boundary "
                  f"({shown}) but no test in the repo reaches this file."),
        axis="coverage",
    )
    f.status = "untested_boundary"
    f.observed = (
        "No test file imports or reaches this target, yet the diff adds a "
        "boundary interaction. This is a COVERAGE observation, not an "
        "executed verdict: confirm whether the change warrants a test (real "
        "gap) or is an intentional passthrough / typing-only change (not a "
        "gap). If it warrants one, the test should mock the boundary the way "
        "the repo's other tests do."
    )
    f.confidence = "low"
    f.confidence_reason = (
        "static coverage signal (boundary in diff + no reaching test), not an "
        "execution result — human adjudicates."
    )
    return f
