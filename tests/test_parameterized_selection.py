# EDGEVERDICT_PARAMETERIZED_SELECTION_TESTS_V1
"""Parameterized proposals (@parameterized.expand / @pytest.mark.parametrize)
must not be lost as "name match failed". One def fans into N collected node
ids (Class::test_x_0_case, test_x[case]); exact-node-id serial selection
matches ZERO of them and drops a GOOD proposal's verdict. The harness
detects parameterization and selects by -k on the unique gate mark instead,
which is a substring of every generated variant and cannot collide with a
host test — preserving the no-misattribution guarantee node-id gave the
ordinary case."""
from __future__ import annotations

from edgeverdict.verifiers.pytest_harness import PytestHarness

H = PytestHarness()


def _profile():
    return type("P", (), {"test_base": ["python", "-m", "pytest"]})()


_EXPAND = '''import unittest
from parameterized import parameterized
class TestC(unittest.TestCase):
    @parameterized.expand([("a",), ("b",)])
    def test_thing(self, x):
        self.assertTrue(True)
'''

_PARAMETRIZE = '''import pytest
@pytest.mark.parametrize("x", ["a", "b"])
def test_thing(x):
    assert True
'''

_PLAIN = '''def test_thing():
    assert 1 == 1
'''

_DOCSTRING_DECOY = '''def test_thing():
    """example: @pytest.mark.parametrize is documented here"""
    assert True
'''


def test_expand_is_detected():
    assert H._is_parameterized(_EXPAND) is True


def test_parametrize_is_detected():
    assert H._is_parameterized(_PARAMETRIZE) is True


def test_plain_is_not_parameterized():
    assert H._is_parameterized(_PLAIN) is False


def test_docstring_mention_does_not_trip_detection():
    # ast decorator list, not text scan: a decorator NAMED in a docstring
    # must not be read as a real decorator.
    assert H._is_parameterized(_DOCSTRING_DECOY) is False


def test_parameterized_serial_uses_dash_k_on_the_title():
    marked = H.mark_title(_EXPAND, "___evp0___")
    title = H.test_title(marked)
    cmd = H.serial_command(_profile(), "t.py", title, "/tmp/o.xml",
                           is_parameterized=True)
    assert "-k" in cmd
    # the -k value is the mark-bearing function name (a substring of every
    # generated variant), NOT a file::node id
    k = cmd[cmd.index("-k") + 1]
    assert "___evp0___" in k
    assert "::" not in k
    assert "t.py::" not in " ".join(cmd)


def test_plain_serial_still_uses_exact_node_id():
    # the safety guarantee for the common case is untouched.
    cmd = H.serial_command(_profile(), "t.py", "test_thing", "/tmp/o.xml",
                           is_parameterized=False)
    assert "t.py::test_thing" in cmd
    assert "-k" not in cmd


def test_default_is_node_id_when_flag_omitted():
    cmd = H.serial_command(_profile(), "t.py", "test_thing", "/tmp/o.xml")
    assert "t.py::test_thing" in cmd
