# EDGEVERDICT_WORKSPACE_FILTER_TESTS_V1
"""Workspace-aware pnpm filter: engage --filter ONLY when the workspace
root cannot resolve vitest but the target's package can (supabase/mcp
shape). Must be a no-op on root-vitest workspaces (zod shape) and on
non-workspace repos, so gauntlet behavior cannot drift."""
import json
import os

from edgeverdict.config import Config, build_profile, detect_workspace_filter


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))


def _supabase_shape(tmp_path):
    root = str(tmp_path)
    _write(root, "pnpm-workspace.yaml", "packages:\n  - packages/*\n")
    _write(root, "package.json",
           {"scripts": {"test": "pnpm --filter x test"},
            "devDependencies": {"@biomejs/biome": "^1"}})
    _write(root, "pnpm-lock.yaml", "lockfileVersion: 9\n")
    _write(root, "packages/utils/package.json",
           {"name": "@acme/utils", "devDependencies": {"vitest": "^2"}})
    _write(root, "packages/utils/src/server.test.ts", "")
    return root


def test_filter_engaged_when_root_lacks_vitest(tmp_path):
    root = _supabase_shape(tmp_path)
    name, pkg_dir = detect_workspace_filter(root, "packages/utils/src/server.test.ts")
    assert name == "@acme/utils"
    assert pkg_dir == "packages/utils"
    prof = build_profile(root, Config(), "packages/utils/src/server.test.ts")
    assert "--filter" in prof.test_base
    assert "@acme/utils" in prof.test_base
    # probe path handed to vitest is package-relative (exec cwd = package)
    assert prof.smoke_cmd[-1].startswith("src/")
    # probe FILE keeps its repo-relative path (write base = repo root)
    assert prof.smoke_probe[0].startswith("packages/utils/src/")


def test_noop_when_root_has_vitest_zod_shape(tmp_path):
    root = _supabase_shape(tmp_path)
    _write(root, "package.json",
           {"devDependencies": {"vitest": "^2"}})
    name, pkg_dir = detect_workspace_filter(root, "packages/utils/src/server.test.ts")
    assert (name, pkg_dir) == (None, None)
    prof = build_profile(root, Config(), "packages/utils/src/server.test.ts")
    assert "--filter" not in prof.test_base
    # probe path stays repo-relative, exactly the pre-fix behavior
    assert prof.smoke_cmd[-1].startswith("packages/utils/src/")


def test_noop_on_non_workspace_repo(tmp_path):
    root = str(tmp_path)
    _write(root, "package.json", {"devDependencies": {"vitest": "^2"}})
    _write(root, "src/x.test.ts", "")
    name, pkg_dir = detect_workspace_filter(root, "src/x.test.ts")
    assert (name, pkg_dir) == (None, None)
    prof = build_profile(root, Config(), "src/x.test.ts")
    assert "--filter" not in prof.test_base


def test_explicit_config_filter_wins(tmp_path):
    root = _supabase_shape(tmp_path)
    prof = build_profile(root, Config(filter="@acme/other"),
                         "packages/utils/src/server.test.ts")
    assert "@acme/other" in prof.test_base
    assert "@acme/utils" not in prof.test_base


def test_noop_when_package_also_lacks_vitest(tmp_path):
    root = _supabase_shape(tmp_path)
    _write(root, "packages/utils/package.json", {"name": "@acme/utils"})
    name, pkg_dir = detect_workspace_filter(root, "packages/utils/src/server.test.ts")
    assert (name, pkg_dir) == (None, None)
