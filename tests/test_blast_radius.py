# EDGEVERDICT_BLAST_RADIUS_TESTS_V1
"""Static blast radius: module-level reach from the repo's own import edges.
Deterministic, no model. Fixtures build tiny repos on disk so the walk, the
python AST import matching, the JS suffix matching, the test-file exclusion,
and the tier thresholds are all pinned to behavior, not implementation."""
from __future__ import annotations

import os

from edgeverdict.blast import compute_blast, triage


def _w(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(text)


def test_leaf_module_is_narrow(tmp_path):
    d = str(tmp_path)
    _w(d, "pkg/leaf.py", "X = 1\n")
    _w(d, "pkg/other.py", "import os\n")
    tier, note = compute_blast(d, "pkg/leaf.py")
    assert tier == "narrow"
    assert "nothing in-repo imports it" in note


def test_single_importer_is_moderate(tmp_path):
    d = str(tmp_path)
    _w(d, "pkg/target.py", "def f(): pass\n")
    _w(d, "pkg/user.py", "from pkg.target import f\n")
    tier, note = compute_blast(d, "pkg/target.py")
    assert tier == "moderate"
    assert "1 direct importer(s)" in note
    assert "pkg/user.py" in note


def test_many_importers_is_wide(tmp_path):
    d = str(tmp_path)
    _w(d, "pkg/core.py", "def f(): pass\n")
    for i in range(6):
        _w(d, f"pkg/u{i}.py", "from pkg import core\n")
    tier, note = compute_blast(d, "pkg/core.py")
    assert tier == "wide"
    assert "6 direct importer(s)" in note


def test_test_files_counted_separately_not_as_blast(tmp_path):
    # tests importing a module is coverage, not blast: a module imported
    # ONLY by tests stays narrow.
    d = str(tmp_path)
    _w(d, "pkg/target.py", "def f(): pass\n")
    _w(d, "tests/test_target.py", "from pkg.target import f\n")
    tier, note = compute_blast(d, "pkg/target.py")
    assert tier == "narrow"
    assert "1 test file(s)" in note


def test_one_hop_transitive_counts_python(tmp_path):
    d = str(tmp_path)
    _w(d, "pkg/target.py", "def f(): pass\n")
    _w(d, "pkg/mid.py", "from pkg.target import f\n")
    for i in range(9):
        _w(d, f"pkg/top{i}.py", "from pkg.mid import f\n")
    tier, note = compute_blast(d, "pkg/target.py")
    # 1 direct + 9 one-hop = reach 10 -> wide
    assert tier == "wide"
    assert "9 one-hop" in note


def test_js_suffix_import_detected(tmp_path):
    d = str(tmp_path)
    _w(d, "src/utils/thing.ts", "export const x = 1\n")
    _w(d, "src/app.ts", "import { x } from './utils/thing'\n")
    tier, note = compute_blast(d, "src/utils/thing.ts")
    assert tier == "moderate"
    assert "src/app.ts" in note


def test_triage_matrix():
    assert triage("high", "wide") == "file-ready"
    assert triage("high", "narrow") == "verify-then-file"
    assert triage("low", "wide") == "verify-hard"
    assert triage("low", "narrow") == "low-priority"
