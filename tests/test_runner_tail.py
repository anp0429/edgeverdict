"""EDGEVERDICT_RUNNER_TAIL_TESTS_V1

When the runner dies before writing JSON, the observed cause must carry the
runner's own last words (stderr preferred, stdout fallback, ANSI stripped)
instead of the bare "no JSON output" shrug that hid studio's boot failure.
"""

from __future__ import annotations

import subprocess

from edgeverdict.verifiers.finding_verifier import _runner_tail


def _proc(out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=1,
                                       stdout=out, stderr=err)


def test_prefers_stderr_and_joins_last_lines():
    p = _proc(out="ignored stdout", err="line1\n\nError: cannot find module 'x'\n  at boot\n")
    tail = _runner_tail(p)
    assert "cannot find module 'x'" in tail
    assert "at boot" in tail
    assert "ignored stdout" not in tail


def test_falls_back_to_stdout_when_stderr_empty():
    p = _proc(out="vitest died: config error\n", err="   \n")
    assert "config error" in _runner_tail(p)


def test_strips_ansi_and_caps_length():
    p = _proc(err="\x1b[31mred error\x1b[0m\n" + ("x" * 900))
    tail = _runner_tail(p)
    assert "\x1b" not in tail
    assert len(tail) <= 500


def test_empty_streams_yield_empty():
    assert _runner_tail(_proc()) == ""
