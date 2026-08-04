# EDGEVERDICT_BEHAVIOR_SURFACE_TESTS_V1
"""behavior_surface: the code's own visible decisions (constants, guard
clauses, one-hop helper bodies) fed to the proposer so it stops inventing
specs the shown lines contradict. Facts from the ast; advisory data; never
changes a verdict. Fixtures replay the real posthog shapes."""
from __future__ import annotations

import os

from edgeverdict.agents.reviewer_agent import (
    behavior_surface,
    _changed_ranges,
    prompt_fingerprint,
)


def _w(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(text)


def test_module_constant_appears_verbatim(tmp_path):
    d = str(tmp_path)
    _w(d, "m.py",
       'NONE_VALUES_ALLOWED_OPERATORS = ["is_not"]\n\n'
       'def match(op, val):\n'
       '    if op not in NONE_VALUES_ALLOWED_OPERATORS and val is None:\n'
       '        return False\n'
       '    return True\n')
    out = behavior_surface(d, "m.py")
    assert 'NONE_VALUES_ALLOWED_OPERATORS = ["is_not"]' in out
    assert "VISIBLE BEHAVIOR" in out


def test_none_guard_after_setup_assignments_is_captured(tmp_path):
    # the real match_property shape: assignments THEN guards. The guard
    # must survive even though it isn't the first statement.
    d = str(tmp_path)
    _w(d, "m.py",
       'ALLOWED = ["is_not"]\n'
       'def match(property, values):\n'
       '    key = property.get("key")\n'
       '    operator = property.get("operator")\n'
       '    value = property.get("value")\n'
       '    if operator not in ALLOWED and value is None:\n'
       '        return False\n'
       '    return True\n')
    out = behavior_surface(d, "m.py")
    assert "value is None" in out


def test_same_file_helper_body_included(tmp_path):
    d = str(tmp_path)
    _w(d, "m.py",
       'def helper(s, v):\n'
       '    return str(s).casefold().startswith(v)\n\n'
       'def match(s, v):\n'
       '    return helper(s, v)\n')
    out = behavior_surface(d, "m.py")
    assert "casefold().startswith" in out


def test_module_attribute_helper_one_hop(tmp_path):
    # match calls utils.str_istartswith(...); utils imported as a module.
    d = str(tmp_path)
    _w(d, "pkg/__init__.py", "")
    _w(d, "pkg/utils.py",
       "def str_istartswith(s, v):\n"
       "    return str(s).casefold().startswith(str(v).casefold())\n")
    _w(d, "pkg/flags.py",
       "from pkg import utils\n"
       "def match(a, b):\n"
       "    return utils.str_istartswith(a, b)\n")
    out = behavior_surface(d, "pkg/flags.py")
    assert "str_istartswith" in out
    assert "casefold" in out


def test_syntax_error_returns_empty_no_raise(tmp_path):
    d = str(tmp_path)
    _w(d, "bad.py", "def broken(:\n")
    assert behavior_surface(d, "bad.py") == ""


def test_non_python_returns_empty(tmp_path):
    d = str(tmp_path)
    _w(d, "a.ts", "const x = 1\n")
    assert behavior_surface(d, "a.ts") == ""


def test_empty_when_nothing_to_show(tmp_path):
    d = str(tmp_path)
    _w(d, "m.py", "def f():\n    return sum([1, 2])\n")
    # no top-level constants, no guards, no intra-repo helpers
    assert behavior_surface(d, "m.py") == ""


def test_changed_ranges_parse_hunk_headers():
    diff = ("@@ -10,3 +12,5 @@ def f():\n"
            " ctx\n+added\n"
            "@@ -40 +50,2 @@\n+x\n")
    assert _changed_ranges(diff) == [(12, 16), (50, 51)]
    assert _changed_ranges("") is None
    assert _changed_ranges("no hunks here") is None


def test_fingerprint_includes_visible_behavior_marker():
    # the shape marker must be in the fingerprint so the cache invalidates
    # when this surface is added (sig-surface precedent).
    from edgeverdict.agents.reviewer_agent import _USER_SHAPE
    assert "VISIBLE BEHAVIOR" in _USER_SHAPE
    assert isinstance(prompt_fingerprint(), str)


def test_ranges_scope_to_touched_function(tmp_path):
    d = str(tmp_path)
    _w(d, "m.py",
       'A = ["x"]\n'
       'def touched(v):\n'
       '    if v is None:\n'
       '        return False\n'
       '    return True\n'
       'def other(v):\n'
       '    if v == 0:\n'
       '        raise ValueError\n'
       '    return v\n')
    # range covering only `touched` (lines ~2-5)
    out = behavior_surface(d, "m.py", changed_line_ranges=[(2, 5)])
    assert "v is None" in out
    assert "raise ValueError" not in out
