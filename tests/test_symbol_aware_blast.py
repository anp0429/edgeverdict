# EDGEVERDICT_SYMBOL_AWARE_BLAST_TESTS_V1
"""File-level import counts overstate blast: importing a file != exercising
the changed function. Symbol-aware blast narrows direct importers to those
that reference a changed symbol, so a logging change to one function in a
20-importer file no longer reads as system-wide (field critique, #542).
With no changed_symbols, behavior is exactly file-level (no regression)."""
from __future__ import annotations


from edgeverdict.blast import (
    _references_symbol,
    changed_symbols_from_diff,
    compute_blast_detail,
)


def _mk(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ── symbol extraction from diff ──────────────────────────────────────────
def test_edit_inside_function_body_attributes_to_that_function():
    diff = (
        "@@ -10,6 +10,8 @@ class C:\n"
        "     def load_flags(self):\n"
        "         x = 1\n"
        "-        return x\n"
        "+        if x == 401:\n"
        "+            return None\n"
    )
    assert "load_flags" in changed_symbols_from_diff(diff)


def test_new_function_declaration_is_a_changed_symbol():
    diff = "@@ -1,0 +1,2 @@\n+def new_helper(a):\n+    return a\n"
    assert "new_helper" in changed_symbols_from_diff(diff)


def test_js_function_and_arrow_const():
    diff = (
        "@@ -1,0 +1,1 @@\n+export function getLogQuery(opts) {\n"
        "@@ -5,0 +5,1 @@\n+const buildWhere = (f) =>\n"
    )
    got = changed_symbols_from_diff(diff)
    assert "getLogQuery" in got
    assert "buildWhere" in got


def test_empty_diff_no_symbols():
    assert changed_symbols_from_diff("") == set()


# ── symbol reference matching ────────────────────────────────────────────
def test_reference_is_word_boundary():
    assert _references_symbol("x = load_flags()", {"load_flags"}) is True
    # must not match a longer name
    assert _references_symbol("load_flags_v2()", {"load_flags"}) is False
    # must not match an attribute of something else
    assert _references_symbol("other.load_flags", {"load_flags"}) is False


# ── the narrowing, end to end on a synthetic repo ────────────────────────
def test_symbol_aware_narrows_widely_imported_file(tmp_path):
    # target file with two exported functions
    _mk(tmp_path, "pkg/store.py",
        "def read_rows():\n    return []\n\ndef write_row(r):\n    return r\n")
    # five importers, but only ONE actually calls the changed function
    _mk(tmp_path, "pkg/a.py", "from pkg.store import write_row\nwrite_row(1)\n")
    for name in ("b", "c", "d", "e"):
        _mk(tmp_path, f"pkg/{name}.py",
            "from pkg import store\n# uses store.read_rows elsewhere\nstore\n")

    # file-level: 5 importers -> wide-ish
    file_level = compute_blast_detail(str(tmp_path), "pkg/store.py")
    # symbol-aware on the changed write_row: only pkg/a.py references it
    sym = compute_blast_detail(str(tmp_path), "pkg/store.py",
                               changed_symbols={"write_row"})
    assert len(sym.direct) == file_level.direct.__len__()  # same import set
    assert "reference the changed symbol" in sym.note
    # the reaching count is smaller than the raw importer count
    assert "1 importer(s) reference" in sym.note
    assert "import the file but not the changed symbol" in sym.note


def test_unreferenced_symbol_is_narrow(tmp_path):
    _mk(tmp_path, "pkg/store.py", "def _private(): return 1\n")
    _mk(tmp_path, "pkg/a.py", "from pkg import store\nstore\n")
    sym = compute_blast_detail(str(tmp_path), "pkg/store.py",
                               changed_symbols={"_private"})
    assert sym.tier == "narrow"  # nothing references it -> local change


def test_no_symbols_is_file_level_unchanged(tmp_path):
    _mk(tmp_path, "pkg/store.py", "def f(): return 1\n")
    _mk(tmp_path, "pkg/a.py", "from pkg import store\nstore\n")
    a = compute_blast_detail(str(tmp_path), "pkg/store.py")
    b = compute_blast_detail(str(tmp_path), "pkg/store.py", changed_symbols=None)
    assert a.tier == b.tier
    assert a.note == b.note  # identical when no symbols given
    assert "direct importer(s)" in a.note  # the file-level phrasing
