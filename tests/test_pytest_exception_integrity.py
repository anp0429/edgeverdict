"""A custom exception wrapping an assertion must not manufacture a code gap."""

import os
import subprocess
import sys

import pytest

from edgeverdict.verifiers.pytest_harness import PytestHarness


@pytest.mark.parametrize("traceback_style", ["long", "short"])
@pytest.mark.parametrize("exception_name", ["InvalidInput", "RuntimeFault", "Oops"])
def test_custom_exception_after_assertion_is_broken_test(
    tmp_path, exception_name, traceback_style,
):
    test_file = tmp_path / "test_proposal.py"
    test_file.write_text(
        f"class {exception_name}(Exception):\n"
        "    pass\n\n"
        "def test_proposal():\n"
        "    try:\n"
        "        assert False, 'inner assertion'\n"
        "    except AssertionError as exc:\n"
        f"        raise {exception_name}('proposal crashed') from exc\n",
        encoding="utf-8",
    )
    report = tmp_path / "results.xml"
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q",
         f"--tb={traceback_style}",
         f"--junit-xml={report}"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    harness = PytestHarness()
    status, explanation = harness.read_verdict(str(report))
    assert status == "broken_test", (status, explanation, report.read_text())
    results = harness.read_batch(str(report))
    assert results is not None and len(results) == 1
    assert harness.classify_failure(results[0].failure)[0] == "load_error"
    assert exception_name in harness.failure_headline(results[0].failure)


@pytest.mark.parametrize("traceback_style", ["long", "short"])
def test_assertion_after_custom_exception_remains_a_gap(tmp_path, traceback_style):
    test_file = tmp_path / "test_proposal.py"
    test_file.write_text(
        "class InvalidInput(Exception):\n"
        "    pass\n\n"
        "def test_proposal():\n"
        "    try:\n"
        "        raise InvalidInput('inner crash')\n"
        "    except InvalidInput as exc:\n"
        "        raise AssertionError('outer assertion') from exc\n",
        encoding="utf-8",
    )
    report = tmp_path / "results.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q",
         f"--tb={traceback_style}", f"--junit-xml={report}"],
        cwd=tmp_path, env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    harness = PytestHarness()
    assert harness.read_verdict(str(report))[0] == "confirmed_gap"
    results = harness.read_batch(str(report))
    assert results is not None and len(results) == 1
    assert harness.classify_failure(results[0].failure)[0] == "assertion"


@pytest.mark.parametrize("final_line", [
    "E   application.InvalidInput: invalid request",
    "application.InvalidInput: invalid request",
    "test_proposal.py:8: InvalidInput",
    "/tmp/project with spaces/test_proposal.py:8: InvalidInput",
])
def test_final_custom_exception_overrides_inner_assertion(final_line):
    report = "E   AssertionError: inner assertion\n\n" + final_line
    harness = PytestHarness()
    assert harness.classify_failure(report)[0] == "load_error"
    assert "InvalidInput" in harness.failure_headline(report)
