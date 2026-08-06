# EDGEVERDICT_WARM_CACHE_TESTS_V1
"""Cross-run warm-base cache: node_modules + smoke-pass keyed on lockfile
hash, reused across separate CLI invocations of the same repo so install
(~90s) and smoke (~75s) become one-time per dependency state. Opt-in
(EDGEVERDICT_WARM_CACHE=1), fails SAFE (any doubt -> normal install)."""
from __future__ import annotations

import os
import tempfile

from edgeverdict.verifiers.finding_verifier import (
    _dep_fingerprint,
    _warm_cache_enabled,
)


def _repo_with_lock(text: str) -> str:
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "pnpm-lock.yaml"), "w") as f:
        f.write(text)
    return d


def test_cache_off_by_default(monkeypatch):
    monkeypatch.delenv("EDGEVERDICT_WARM_CACHE", raising=False)
    assert _warm_cache_enabled() is False


def test_cache_on_with_env(monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_WARM_CACHE", "1")
    assert _warm_cache_enabled() is True


def test_fingerprint_stable_for_same_lockfile():
    d = _repo_with_lock("lock v1")
    a = _dep_fingerprint(d, ".", ["pnpm", "install"])
    b = _dep_fingerprint(d, ".", ["pnpm", "install"])
    assert a is not None and a == b


def test_fingerprint_changes_when_lockfile_changes():
    d = _repo_with_lock("lock v1")
    a = _dep_fingerprint(d, ".", ["pnpm", "install"])
    with open(os.path.join(d, "pnpm-lock.yaml"), "w") as f:
        f.write("lock v2 different")
    b = _dep_fingerprint(d, ".", ["pnpm", "install"])
    assert a != b


def test_fingerprint_isolates_monorepo_packages():
    # same root lockfile, different package dir -> different key, so two
    # packages don't share a node_modules cache entry.
    d = _repo_with_lock("lock v1")
    root = _dep_fingerprint(d, ".", ["pnpm", "install"])
    pkg = _dep_fingerprint(d, "packages/pg-meta", ["pnpm", "install"])
    assert root != pkg


def test_fingerprint_changes_with_install_cmd():
    d = _repo_with_lock("lock v1")
    a = _dep_fingerprint(d, ".", ["pnpm", "install"])
    b = _dep_fingerprint(d, ".", ["pnpm", "install", "--frozen-lockfile"])
    assert a != b


def test_no_lockfile_returns_none():
    # no lockfile -> deps not reproducible enough to cache -> normal install
    d = tempfile.mkdtemp()
    assert _dep_fingerprint(d, ".", ["pnpm", "install"]) is None
