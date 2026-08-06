# EDGEVERDICT_TEST_SETUP_SURFACE_TESTS_V1
"""test_setup_surface shows the proposer the fixtures and boundary-doubles it
can REUSE, so it writes edge cases with the author's own mocks instead of
inventing one or reaching a real db/cache/queue. Facts only (availability,
not technique); pytest-only; "" on any trouble. Fed with blast's
test_importers so the whole reaching-test-file set is in scope, not just
--tests.
"""
from __future__ import annotations

from edgeverdict.test_setup import (
    _conftest_chain,
    _double_names,
    _fixtures_in,
    _patch_targets,
    setup_surface,
)


def _w(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ── conftest chain ───────────────────────────────────────────────────────
def test_conftest_chain_is_root_first(tmp_path):
    _w(tmp_path, "conftest.py", "")
    _w(tmp_path, "pkg/test/conftest.py", "")
    _w(tmp_path, "pkg/test/test_x.py", "")
    chain = _conftest_chain(str(tmp_path), "pkg/test/test_x.py")
    assert chain == ["conftest.py", "pkg/test/conftest.py"]


def test_conftest_chain_skips_absent_dirs(tmp_path):
    _w(tmp_path, "pkg/test/conftest.py", "")
    _w(tmp_path, "pkg/test/test_x.py", "")
    chain = _conftest_chain(str(tmp_path), "pkg/test/test_x.py")
    assert chain == ["pkg/test/conftest.py"]  # no root conftest


# ── fixtures ─────────────────────────────────────────────────────────────
def test_fixture_name_and_docstring_summary():
    src = ('import pytest\n'
           '@pytest.fixture\n'
           'def fake_db():\n'
           '    """A stand-in database."""\n'
           '    return {}\n')
    got = _fixtures_in(src)
    assert ("fake_db", "A stand-in database.") in got


def test_autouse_fixture_is_flagged():
    src = ('import pytest\n'
           '@pytest.fixture(autouse=True)\n'
           'def reset(monkeypatch):\n'
           '    pass\n')
    got = _fixtures_in(src)
    assert got and got[0][0] == "reset"
    assert "[autouse]" in got[0][1]
    assert "monkeypatch" in got[0][1]


def test_non_fixture_function_is_not_collected():
    src = "def helper():\n    return 1\n"
    assert _fixtures_in(src) == []


# ── patch targets ────────────────────────────────────────────────────────
def test_patch_target_extracted():
    src = ('from unittest.mock import patch\n'
           '@patch("pkg.db.query")\n'
           'def test_x(m):\n'
           '    pass\n')
    assert "pkg.db.query" in _patch_targets(src)


def test_patch_object_string_extracted():
    src = 'mock.patch("pkg.cache.get")\n'
    assert "pkg.cache.get" in _patch_targets(src)


def test_bare_name_not_a_patch_target():
    # a patch of a local (no dotted path) is not a useful lookup hint
    src = '@patch("query")\ndef test_x(m):\n    pass\n'
    assert _patch_targets(src) == []


# ── double names ─────────────────────────────────────────────────────────
def test_fake_class_name_collected():
    src = "class FakeClient:\n    pass\n"
    assert "FakeClient" in _double_names(src)


def test_mock_helper_collected():
    src = "def make_mock_span():\n    return object()\n"
    assert "make_mock_span" in _double_names(src)


def test_ordinary_name_not_collected():
    src = "def compute_total():\n    return 0\n"
    assert _double_names(src) == []


# ── the surface ──────────────────────────────────────────────────────────
def test_surface_unions_conftest_and_extra_files(tmp_path):
    _w(tmp_path, "conftest.py",
       "import pytest\n@pytest.fixture\ndef root_fix():\n    return 1\n")
    _w(tmp_path, "pkg/test/conftest.py",
       "import pytest\n@pytest.fixture\ndef db_fix():\n    return 2\n")
    _w(tmp_path, "pkg/test/test_a.py",
       'from unittest.mock import patch\n'
       '@patch("pkg.db.query")\ndef test_a():\n    pass\n')
    _w(tmp_path, "pkg/test/test_b.py",
       "class FakeConn:\n    pass\n")
    out = setup_surface(
        str(tmp_path), "pkg/db.py", "pkg/test/test_a.py",
        extra_test_files=["pkg/test/test_b.py"])
    assert "root_fix" in out          # root conftest fixture
    assert "db_fix" in out            # nested conftest fixture
    assert "pkg.db.query" in out      # patch target from test_a
    assert "FakeConn" in out          # fake from the extra test_b
    assert "TEST SETUP AVAILABLE" in out


def test_surface_empty_when_no_setup(tmp_path):
    _w(tmp_path, "pkg/test/test_x.py",
       "def test_x():\n    assert 1 == 1\n")
    out = setup_surface(str(tmp_path), "pkg/m.py", "pkg/test/test_x.py")
    assert out == ""


def test_non_py_tests_returns_empty(tmp_path):
    out = setup_surface(str(tmp_path), "src/m.ts", "src/m.test.ts")
    assert out == ""


def test_directive_tells_model_to_reuse_not_invent(tmp_path):
    _w(tmp_path, "conftest.py",
       "import pytest\n@pytest.fixture\ndef c():\n    return 1\n")
    _w(tmp_path, "test_x.py", "def test_x():\n    pass\n")
    out = setup_surface(str(tmp_path), "m.py", "test_x.py")
    # the directive must steer AWAY from real infra and from inventing
    assert "do NOT reach a real" in out
    assert "do NOT invent a new mock" in out
