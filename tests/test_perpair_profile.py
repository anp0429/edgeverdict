# EDGEVERDICT_PERPAIR_PROFILE_TESTS_V1
"""Multi-target runs must build the runner profile PER PAIR.

A workspace repo scopes its test runner per package (pnpm --filter <pkg> exec
vitest). A multi-target run can span packages, so reusing the PRIMARY target's
profile for a target in a different package runs vitest inside the wrong
package: the injected test file is never collected, nothing registers, and
every finding reports "name match failed". Found on supabase/mcp#316.
"""
import json
import os

from edgeverdict.config import Config, build_profile, detect_project_dir


def _w(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


def _workspace(tmp_path):
    root = str(tmp_path)
    _w(root, "pnpm-workspace.yaml", "packages:\n  - packages/*\n")
    _w(root, "pnpm-lock.yaml", "lockfileVersion: 9\n")
    _w(root, "package.json", json.dumps(
        {"devDependencies": {"@biomejs/biome": "^1"}}))
    for pkg in ("alpha", "beta"):
        _w(root, f"packages/{pkg}/package.json", json.dumps(
            {"name": f"@acme/{pkg}", "devDependencies": {"vitest": "^2"}}))
        _w(root, f"packages/{pkg}/src/thing.ts", "export const x = 1;\n")
        _w(root, f"packages/{pkg}/src/thing.test.ts",
           "import { x } from './thing.js';\ntest('a', () => {});\n")
    return root


def _filter_of(profile):
    base = list(profile.test_base)
    return base[base.index("--filter") + 1] if "--filter" in base else None


def test_each_package_gets_its_own_filter(tmp_path):
    root = _workspace(tmp_path)
    a = build_profile(root, Config(), "packages/alpha/src/thing.test.ts",
                      project_dir=detect_project_dir(
                          root, "packages/alpha/src/thing.ts"))
    b = build_profile(root, Config(), "packages/beta/src/thing.test.ts",
                      project_dir=detect_project_dir(
                          root, "packages/beta/src/thing.ts"))
    assert _filter_of(a) == "@acme/alpha"
    assert _filter_of(b) == "@acme/beta"
    assert _filter_of(a) != _filter_of(b)


def test_primary_profile_would_misscope_a_cross_package_target(tmp_path):
    root = _workspace(tmp_path)
    primary = build_profile(root, Config(), "packages/alpha/src/thing.test.ts",
                            project_dir=detect_project_dir(
                                root, "packages/alpha/src/thing.ts"))
    assert _filter_of(primary) == "@acme/alpha"
    assert "packages/beta" not in " ".join(primary.test_base)


def test_run_review_builds_a_profile_for_every_pair():
    import inspect

    from edgeverdict import api

    src = inspect.getsource(api._run_review_pipeline)
    loop = src.split("for tgt, tst_path in pairs:", 1)[1]
    assert "FindingVerifier(repo, pair_profile" in loop
    assert "project_dir=pair_project_dir" in loop
    assert "harness_for_profile(pair_profile)" in loop
