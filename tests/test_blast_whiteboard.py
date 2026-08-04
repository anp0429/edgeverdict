# EDGEVERDICT_BLAST_WHITEBOARD_TESTS_V1
"""The blast pane renders per-target importer detail into the review board:
tier badge + three chip groups (direct / one-hop / test files). The one-hop
list COLLAPSES to a +N expander, never CAPS — hiding scope silently would
break the board's "never lies about scope" promise, so every importer stays
in the document (searchable), just folded past the inline count."""
from __future__ import annotations

import html as _html

from edgeverdict.blast import BlastDetail
from edgeverdict.review import (
    ReviewRun, ReviewFinding, render_review_html, _blast_pane, _BLAST_INLINE,
)


def _detail(direct=None, transitive=None, tests=None, tier="wide",
            target="pkg/mod.py", truncated=False):
    return BlastDetail(
        tier=tier, note=f"{len(direct or [])} direct importer(s)",
        target=target, direct=direct or [], transitive=transitive or [],
        test_importers=tests or [], truncated=truncated,
    )


def test_no_details_no_pane():
    # a run with no blast info renders exactly as before (back-compat).
    run = ReviewRun(intent="x", target="pkg/mod.py")
    assert _blast_pane(run) == ""


def test_pane_has_tier_badge_and_target():
    run = ReviewRun(intent="x", target="pkg/mod.py")
    run.blast_details = [_detail(direct=["a.py", "b.py"])]
    pane = _blast_pane(run)
    assert "wide blast" in pane
    assert "pkg/mod.py" in pane


def test_small_one_hop_renders_flat_no_fold():
    run = ReviewRun(intent="x", target="pkg/mod.py")
    run.blast_details = [_detail(transitive=[f"m{i}.py" for i in range(_BLAST_INLINE)])]
    pane = _blast_pane(run)
    # exactly _BLAST_INLINE items -> no "+N more"
    assert "more</summary>" not in pane
    assert f"one-hop importers ({_BLAST_INLINE})" in pane


def test_large_one_hop_folds_the_remainder_but_hides_nothing():
    n = _BLAST_INLINE + 32
    items = [f"m{i}.py" for i in range(n)]
    run = ReviewRun(intent="x", target="pkg/mod.py")
    run.blast_details = [_detail(transitive=items)]
    pane = _blast_pane(run)
    # the count is honest (all N), the fold advertises exactly the remainder,
    # and EVERY importer is present in the document (nothing capped away)
    assert f"one-hop importers ({n})" in pane
    assert "+32 more" in pane
    for it in items:
        assert it in pane


def test_empty_group_says_none():
    run = ReviewRun(intent="x", target="pkg/mod.py")
    run.blast_details = [_detail(direct=["a.py"])]  # no transitive, no tests
    pane = _blast_pane(run)
    assert "none" in pane


def test_truncation_is_declared():
    run = ReviewRun(intent="x", target="pkg/mod.py")
    run.blast_details = [_detail(direct=["a.py"], truncated=True)]
    pane = _blast_pane(run)
    assert "lower bound" in pane


def test_importer_names_are_escaped():
    run = ReviewRun(intent="x", target="pkg/mod.py")
    run.blast_details = [_detail(direct=["<script>evil.py"])]
    pane = _blast_pane(run)
    assert "<script>evil" not in pane
    assert _html.escape("<script>evil.py") in pane


def test_full_board_renders_pane_between_summary_and_cards(tmp_path):
    run = ReviewRun(intent="x", target="pkg/mod.py")
    run.findings = [ReviewFinding(behavior="b", status="handled")]
    run.blast_details = [_detail(direct=["a.py", "b.py"],
                                 transitive=[f"m{i}.py" for i in range(40)],
                                 tests=["t.py"])]
    out = str(tmp_path / "board.html")
    render_review_html(run, out)
    doc = open(out).read()
    s = doc.index('class="summary"')
    b = doc.index('class="blast"')
    w = doc.index('class="wrap"')
    assert s < b < w  # summary -> blast pane -> cards
