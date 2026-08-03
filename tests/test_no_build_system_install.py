# EDGEVERDICT_NO_BUILD_SYSTEM_INSTALL_TESTS_V1
"""A repo with no [build-system] table cannot be editable-installed. The
python sandbox lane must install its declared dependencies and skip the
`-e .` build, or pip's legacy setuptools fallback dies on a flat
multi-package tree ("Failed to build ... when getting requirements to
build editable"). Vote: posthog/posthog ([project] + uv.lock, no
build-system), which runs in-place with the repo root on the path."""
from __future__ import annotations

import textwrap

from edgeverdict.config import (
    _declared_dependencies,
    _has_build_system,
    _python_sandbox_install,
)


def _write(d, body):
    (d / "pyproject.toml").write_text(textwrap.dedent(body))
    return str(d)


def test_uv_managed_repo_installs_deps_only(tmp_path):
    root = _write(tmp_path, '''
        [project]
        name = "posthog"
        dependencies = ["django>=4.2", "clickhouse-driver"]
        [tool.uv]
        dev-dependencies = ["pytest-django"]
    ''')
    assert _has_build_system(root) is False
    cmd = _python_sandbox_install(root)
    assert "-e" not in cmd
    assert "django>=4.2" in cmd and "clickhouse-driver" in cmd


def test_buildable_repo_keeps_editable(tmp_path):
    root = _write(tmp_path, '''
        [build-system]
        requires = ["setuptools"]
        build-backend = "setuptools.build_meta"
        [project]
        name = "normal"
        dependencies = ["requests"]
    ''')
    assert _has_build_system(root) is True
    cmd = _python_sandbox_install(root)
    assert "-e" in cmd and "." in cmd


def test_setup_py_repo_is_buildable(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup; setup()")
    assert _has_build_system(str(tmp_path)) is True


def test_declared_deps_survive_a_malformed_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project\nbroken")
    assert _declared_dependencies(str(tmp_path)) == []
    # no build-system readable => deps-only lane, which is the safe default
    assert "-e" not in _python_sandbox_install(str(tmp_path))
