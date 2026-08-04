# EDGEVERDICT_BUILD_ISOLATION_RETRY_TESTS_V1
"""When a pip editable install fails while fetching its [build-system]
build dependencies into pip's isolated build env, the run must degrade to
a seeded --no-build-isolation retry, not report "environment failure".

Found on posthog/posthog-python: [build-system] requires setuptools>=83,
which pip fetches into a throwaway build env before building the editable
install. That fetch succeeds on a warm-cached base and fails on a fresh
resample — so the SAME target that ran clean one run reports 0 gaps /
environment failure the next. Reproducibility across runs is the product;
a fragile build-env fetch must never silently zero a run."""
from __future__ import annotations

from edgeverdict.verifiers.vitest_verifier import (
    no_build_isolation_install,
    build_tool_seed_cmd,
    unfrozen_install,
)

_POSTHOG_INSTALL = ["python", "-m", "pip", "install", "--quiet", "--user",
                    "--no-cache-dir", "-e", "."]


def test_no_build_isolation_inserts_flag_after_install():
    out = no_build_isolation_install(_POSTHOG_INSTALL)
    assert out is not None
    assert "--no-build-isolation" in out
    assert out[out.index("install") + 1] == "--no-build-isolation"
    # the rest of the command is preserved
    assert out[-2:] == ["-e", "."]


def test_no_build_isolation_is_idempotent():
    once = no_build_isolation_install(_POSTHOG_INSTALL)
    assert no_build_isolation_install(once) is None


def test_no_build_isolation_none_for_non_pip():
    assert no_build_isolation_install(["npm", "ci"]) is None
    assert no_build_isolation_install(["yarn", "install"]) is None


def test_seed_cmd_installs_build_tools_with_same_env_flags():
    seed = build_tool_seed_cmd(_POSTHOG_INSTALL)
    assert seed is not None
    assert "setuptools>=61.0" in seed and "wheel" in seed
    # environment-shaping flags carried over so the seed lands in the same
    # place the real install will look
    assert "--user" in seed and "--no-cache-dir" in seed
    # but NOT the project spec — the seed installs build tools, not the repo
    assert "-e" not in seed and "." not in seed[seed.index("install") + 1:]


def test_seed_cmd_none_for_non_pip():
    assert build_tool_seed_cmd(["pnpm", "install"]) is None


def test_seed_and_nbi_compose_into_a_working_pair():
    # the two transforms are designed to run in sequence: seed first, then
    # the no-isolation install builds against the seeded tools.
    seed = build_tool_seed_cmd(_POSTHOG_INSTALL)
    nbi = no_build_isolation_install(_POSTHOG_INSTALL)
    assert seed[:4] == ["python", "-m", "pip", "install"]
    assert nbi[:4] == ["python", "-m", "pip", "install"]
    # same interpreter + user site, so the seed is visible to the build
    assert ("--user" in seed) == ("--user" in nbi)


def test_sibling_unfrozen_transform_unaffected():
    # regression guard: adding the new transforms must not disturb the
    # frozen-lockfile fallback that shares the module.
    assert unfrozen_install(["yarn", "--frozen-lockfile"]) == [
        "yarn", "--no-frozen-lockfile"]
    assert unfrozen_install(["npm", "ci"]) is None
