# EDGEVERDICT_UNTESTED_BOUNDARY_TESTS_V1
"""A data-layer change with no reaching test is a coverage gap. The detector
is CONSERVATIVE by design: it fires only when the diff's added lines show a
boundary AND blast found no test reaching the file. Any uncertainty -> silent.
The finding it emits is advisory ("untested_boundary"), never a confirmed_gap,
so it can never inflate the gap tally the tool's credibility rests on.
"""
from __future__ import annotations

from edgeverdict.untested_boundary import (
    boundary_touched,
    untested_boundary_finding,
)

_DB_DIFF = (
    "--- a/pkg/store.py\n"
    "+++ b/pkg/store.py\n"
    "@@ -1,3 +1,6 @@\n"
    " def save(row):\n"
    "+    cur = conn.cursor()\n"
    "+    cur.execute(\"INSERT INTO t VALUES (%s)\", (row,))\n"
    "+    conn.commit()\n"
)

_PURE_LOGIC_DIFF = (
    "--- a/pkg/calc.py\n"
    "+++ b/pkg/calc.py\n"
    "@@ -1,2 +1,4 @@\n"
    " def total(xs):\n"
    "+    if not xs:\n"
    "+        return 0\n"
    "+    return sum(xs)\n"
)


# ── boundary detection ───────────────────────────────────────────────────
def test_db_diff_is_a_boundary():
    hits = boundary_touched(_DB_DIFF)
    assert hits                        # execute/commit/INSERT matched
    assert any("execute" in h or "commit" in h or "INSERT" in h for h in hits)


def test_pure_logic_diff_is_not_a_boundary():
    assert boundary_touched(_PURE_LOGIC_DIFF) == []


def test_removed_lines_do_not_count():
    # a diff that REMOVES db code is not adding a boundary interaction
    diff = (
        "--- a/pkg/store.py\n"
        "+++ b/pkg/store.py\n"
        "@@ -1,3 +1,1 @@\n"
        "-    cur.execute(\"DELETE FROM t\")\n"
        " def noop(): pass\n"
    )
    assert boundary_touched(diff) == []


def test_redis_and_queue_signals():
    assert boundary_touched("+    redis.setex(k, 60, v)\n")
    assert boundary_touched("+    channel.publish(msg)\n")
    assert boundary_touched("+    producer.produce(topic, m)\n")


# ── the finding: when it fires ───────────────────────────────────────────
def test_fires_when_boundary_and_no_reaching_test():
    f = untested_boundary_finding(_DB_DIFF, "pkg/store.py", test_importers=[])
    assert f is not None
    assert f.status == "untested_boundary"
    assert f.axis == "coverage"
    assert f.confidence == "low"       # advisory, human adjudicates


def test_finding_is_never_a_confirmed_gap():
    # the credibility guarantee: this must NOT read as an execution gap
    f = untested_boundary_finding(_DB_DIFF, "pkg/store.py", test_importers=[])
    assert f.status != "confirmed_gap"


# ── the finding: when it stays SILENT (conservatism) ─────────────────────
def test_silent_when_no_boundary():
    # pure logic change, even with no tests, is not this detector's business
    f = untested_boundary_finding(_PURE_LOGIC_DIFF, "pkg/calc.py",
                                  test_importers=[])
    assert f is None


def test_silent_when_a_test_reaches_the_file():
    # indirect coverage exists -> under-claim, stay silent
    f = untested_boundary_finding(_DB_DIFF, "pkg/store.py",
                                  test_importers=["pkg/test/test_store.py"])
    assert f is None


def test_silent_when_test_importers_has_only_blank_entries():
    # defensive: whitespace-only importer names are not real coverage
    f = untested_boundary_finding(_DB_DIFF, "pkg/store.py",
                                  test_importers=["", "  "])
    assert f is not None               # blanks are not coverage -> still fires


def test_empty_diff_is_silent():
    assert untested_boundary_finding("", "pkg/store.py", []) is None


def test_finding_directs_toward_mocking_the_boundary():
    f = untested_boundary_finding(_DB_DIFF, "pkg/store.py", test_importers=[])
    # the observed text must steer toward the repo's own mocking idiom,
    # and must name itself a coverage observation, not a verdict
    assert "mock" in f.observed.lower()
    assert "coverage observation" in f.observed.lower()
    assert "not an executed verdict" in f.observed.lower()
