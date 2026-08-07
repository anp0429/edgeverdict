"""EDGEVERDICT_WARM_CACHE_V2_TESTS

The three fixes born from the studio full-repo run:
1. the runner bootstrap (npm-cache/pnpm-store) is cached and restored so
   a warm gate needs zero network,
2. smoke markers are per test project -- one dep tree serves many
   projects, each vouched individually,
3. _runner_tail treats container-init noise (tini) as empty and falls
   through to the stream holding the real cause.
"""

from __future__ import annotations

import subprocess

from edgeverdict.verifiers.finding_verifier import (
    _nm_complete,
    _project_smoke_marker,
    _runner_tail,
)
from edgeverdict.verifiers.vitest_verifier import scrubbed_env


def test_project_smoke_marker_distinguishes_projects():
    a = _project_smoke_marker("packages/pg-meta/test/functions.test.ts")
    b = _project_smoke_marker("apps/studio/state/tabs.test.ts")
    same_a = _project_smoke_marker("packages/pg-meta/test/tables.test.ts")
    assert a != b
    assert a == same_a  # same project dir, same marker
    assert a.startswith("smoke.") and a.endswith(".ok")


def test_nm_complete_accepts_modern_and_legacy_markers(tmp_path):
    cdir = tmp_path / "entry"
    cdir.mkdir()
    assert _nm_complete(str(cdir)) is False
    (cdir / "deps.ok").write_text("fp")
    assert _nm_complete(str(cdir)) is True
    (cdir / "deps.ok").unlink()
    (cdir / "smoke.ok").write_text("fp")  # legacy pre-split marker
    assert _nm_complete(str(cdir)) is True
    (cdir / "smoke.ok").unlink()
    (cdir / "smoke.abc123def456.ok").write_text("fp")
    assert _nm_complete(str(cdir)) is True


def test_scrubbed_env_prefers_offline_only_with_cache_root(tmp_path):
    with_cache = scrubbed_env({}, cache_root=str(tmp_path))
    assert with_cache["npm_config_prefer_offline"] == "true"
    assert with_cache["npm_config_cache"].endswith("npm-cache")
    without = scrubbed_env({})
    assert "npm_config_prefer_offline" not in without


def _proc(out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=1,
                                       stdout=out, stderr=err)


def test_runner_tail_skips_tini_noise_to_reach_stdout():
    err = ("[WARN  tini (7)] Tini is not running as PID 1 .\n"
           "To fix the problem, use the -s option or set the environment "
           "variable TINI_SUBREAPER to register Tini as a child subreaper, "
           "or run Tini as PID 1.\n")
    p = _proc(out="Error: Cannot find module 'vitest'\n", err=err)
    tail = _runner_tail(p)
    assert "Cannot find module 'vitest'" in tail
    assert "TINI_SUBREAPER" not in tail


def test_runner_tail_keeps_real_error_after_tini_noise():
    err = ("Tini is not running as PID 1 .\n"
           "npm error code EAI_AGAIN\n"
           "npm error request to https://registry.npmjs.org/pnpm failed\n")
    tail = _runner_tail(_proc(err=err))
    assert "EAI_AGAIN" in tail
    assert "Tini" not in tail


def test_invalidate_cache_entry_removes_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE_DIR", str(tmp_path))
    from edgeverdict.verifiers.finding_verifier import _invalidate_cache_entry
    entry = tmp_path / "abc123"
    (entry / "node_modules").mkdir(parents=True)
    (entry / "deps.ok").write_text("abc123")
    _invalidate_cache_entry("abc123")
    assert not entry.exists()
    _invalidate_cache_entry("never-existed")  # best-effort: no raise
