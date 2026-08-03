from __future__ import annotations


import pytest

from edgeverdict.execution import (
    DockerBackend,
    DockerLimits,
    ExecutionConfigurationError,
    LocalBackend,
    filtered_environment,
)


def test_secret_environment_is_never_forwarded(monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_SANDBOX_ENV_ALLOWLIST", "CUSTOM_FLAG,OPENAI_API_KEY")
    result = filtered_environment(
        {
            "CI": "true",
            "CUSTOM_FLAG": "ok",
            "OPENAI_API_KEY": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "GITHUB_TOKEN": "secret",
            "EDGEVERDICT_SAFE_MODE": "1",
            "RANDOM_HOST_VALUE": "drop",
        }
    )
    assert result["CUSTOM_FLAG"] == "ok"
    assert result["EDGEVERDICT_SAFE_MODE"] == "1"
    assert "OPENAI_API_KEY" not in result
    assert "AWS_SECRET_ACCESS_KEY" not in result
    assert "GITHUB_TOKEN" not in result
    assert "RANDOM_HOST_VALUE" not in result


def test_local_backend_requires_double_opt_in(monkeypatch):
    monkeypatch.delenv("EDGEVERDICT_ALLOW_UNSAFE_LOCAL", raising=False)
    with pytest.raises(ExecutionConfigurationError):
        LocalBackend()
    monkeypatch.setenv("EDGEVERDICT_ALLOW_UNSAFE_LOCAL", "1")
    LocalBackend()


def test_docker_command_contains_hardening_flags(monkeypatch, tmp_path):
    monkeypatch.setattr("edgeverdict.execution.shutil.which", lambda _: "/usr/bin/docker")

    class Probe:
        returncode = 0

    monkeypatch.setattr("edgeverdict.execution.subprocess.run", lambda *a, **k: Probe())
    root = tmp_path / "edgeverdict_warm_test"
    cwd = root / "repo" / "pkg"
    cwd.mkdir(parents=True)
    backend = DockerBackend(
        image="edgeverdict-sandbox:test",
        limits=DockerLimits(max_output_bytes=4096),
        log=lambda *a, **k: None,
    )
    command = backend._docker_command(
        ["python", "-m", "pytest"], cwd=str(cwd), env={"CI": "true"}, name="ev-test"
    )
    joined = " ".join(command)
    assert "--network none" in joined
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert "--pids-limit" in command
    assert "--memory" in command
    assert "--cpus" in command
    assert "--user" in command
    assert "type=bind" in joined
    assert str(root) in joined
    assert "/edgeverdict/repo/pkg" in command
    assert "OPENAI_API_KEY" not in joined


def test_install_network_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr("edgeverdict.execution.shutil.which", lambda _: "/usr/bin/docker")

    class Probe:
        returncode = 0

    monkeypatch.setattr("edgeverdict.execution.subprocess.run", lambda *a, **k: Probe())
    backend = DockerBackend(
        image="edgeverdict-sandbox:test", network_policy="install", log=lambda *a, **k: None
    )
    assert backend._network(["npm", "install"]) == "bridge"
    assert backend._network(["npm", "test"]) == "none"
    assert backend._network(["python", "-m", "pip", "install", "-e", "."]) == "bridge"


def test_bind_mount_uses_valid_field_syntax(monkeypatch, tmp_path):
    """`rw` is not a --mount field. Docker rejects the whole run with exit 125
    ("invalid field 'rw' must be a key=value pair"), so NOTHING executes. The
    existing flag test passes anyway because it only checks that the substring
    "type=bind" appears. Assert every field is key=value instead."""
    monkeypatch.setattr("edgeverdict.execution.shutil.which", lambda _: "/usr/bin/docker")

    class Probe:
        returncode = 0

    monkeypatch.setattr("edgeverdict.execution.subprocess.run", lambda *a, **k: Probe())
    root = tmp_path / "edgeverdict_warm_test"
    cwd = root / "repo"
    cwd.mkdir(parents=True)
    backend = DockerBackend(
        image="edgeverdict-sandbox:test",
        limits=DockerLimits(max_output_bytes=4096),
        log=lambda *a, **k: None,
    )
    command = backend._docker_command(
        ["python", "-c", "pass"], cwd=str(cwd), env={"CI": "true"}, name="ev-test"
    )
    mounts = [command[i + 1] for i, a in enumerate(command) if a == "--mount"]
    assert mounts, "no bind mount in the docker command"
    for mount in mounts:
        for field in mount.split(","):
            assert "=" in field, f"--mount field {field!r} is not key=value"


def test_symlinked_host_paths_are_rewritten(tmp_path):
    """On macOS /var is a symlink to /private/var, so a warm root under
    /var/folders resolves to a different string than the env value holding it.
    A raw prefix comparison forwards the HOST path into the container, where it
    does not exist and the root filesystem is read-only. npm reported
    `ENOENT: mkdir '/var/folders'`, which looks like a permissions failure and
    is actually an unrewritten path."""
    from edgeverdict.execution import _rewrite_mounted_paths

    real = tmp_path / "real"
    (real / "edgeverdict_warm_x" / "repo").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)

    resolved_root = (real / "edgeverdict_warm_x").resolve()
    via_symlink = str(link / "edgeverdict_warm_x" / "npm-cache")

    out = _rewrite_mounted_paths({"npm_config_cache": via_symlink}, resolved_root)
    assert out["npm_config_cache"] == "/edgeverdict/npm-cache", out

    # a path genuinely outside the mount is still left alone
    out = _rewrite_mounted_paths({"SOMETHING": "/usr/bin/node"}, resolved_root)
    assert out["SOMETHING"] == "/usr/bin/node"

    # and non-path values are untouched
    out = _rewrite_mounted_paths({"CI": "true"}, resolved_root)
    assert out["CI"] == "true"


def test_host_paths_in_arguments_are_rewritten(tmp_path):
    """A path crosses the boundary in three places: cwd, env and ARGUMENTS.
    vitest gets --outputFile=<warm root>/result.json; unrewritten it writes
    inside the container to a path that does not exist on that side, the
    harness reads nothing back, and every finding reports "test run produced
    no JSON output". The smoke probe misses it: smoke_cmd has no --outputFile.
    """
    from edgeverdict.execution import _rewrite_arg_paths

    root = (tmp_path / "edgeverdict_warm_x").resolve()
    (root / "repo").mkdir(parents=True)

    out = _rewrite_arg_paths(
        ["npx", "vitest", "run",
         f"--outputFile={root}/edgeverdict-finding-result.json",
         "-t", "some title",
         str(root / "repo")],
        root,
    )
    assert out[3] == "--outputFile=/edgeverdict/edgeverdict-finding-result.json"
    assert out[6] == "/edgeverdict/repo"
    # non-path arguments and titles are untouched
    assert out[:3] == ["npx", "vitest", "run"]
    assert out[4:6] == ["-t", "some title"]
    # a path outside the mount is left alone rather than silently relocated
    assert _rewrite_arg_paths(["--config=/usr/lib/x.json"], root) == \
        ["--config=/usr/lib/x.json"]


def test_install_detection_sees_through_the_npx_wrapper():
    """edgeverdict never invokes a package manager directly. It routes the
    pinned version through npx, so the real install command is
    `npx -y pnpm@9 install --frozen-lockfile`. A detector reading only args[0]
    misses EVERY pnpm repository -- most real targets -- and the install then
    runs with --network none even when the operator asked for `install`,
    failing in a way that looks like a network problem."""
    from edgeverdict.execution import _looks_like_install as is_install

    assert is_install(["npx", "-y", "pnpm@9", "install", "--frozen-lockfile"])
    assert is_install(["npx", "-y", "pnpm@9", "install"])
    assert is_install(["npm", "install", "--no-audit", "--no-fund"])
    assert is_install(["pnpm", "install"])
    assert is_install(["python", "-m", "pip", "install", "-e", "."])
    assert is_install(["uv", "pip", "install", "-e", "."])

    # everything that is NOT an install keeps the network off
    assert not is_install(["npx", "-y", "pnpm@10", "-r", "build"])
    assert not is_install(
        ["npx", "-y", "pnpm@9", "--filter", "@s/p", "exec", "vitest", "run"])
    assert not is_install(["npx", "vitest", "run"])
    assert not is_install(["npx", "-p", "cowsay", "cowsay", "hello"])
    assert not is_install([])


def test_pnpm_install_actually_gets_a_network(monkeypatch, tmp_path):
    """The detector feeding the wrong answer into --network is the whole point,
    so assert the flag on the built command, not just the predicate."""
    from edgeverdict.execution import DockerBackend, DockerLimits

    monkeypatch.setattr(
        "edgeverdict.execution.shutil.which", lambda _: "/usr/bin/docker")

    class Probe:
        returncode = 0

    monkeypatch.setattr(
        "edgeverdict.execution.subprocess.run", lambda *a, **k: Probe())
    root = tmp_path / "edgeverdict_warm_x"
    (root / "repo").mkdir(parents=True)
    backend = DockerBackend(
        image="edgeverdict-sandbox:test",
        limits=DockerLimits(max_output_bytes=4096),
        network_policy="install",
        log=lambda *a, **k: None,
    )

    def network_of(args):
        command = backend._docker_command(
            args, cwd=str(root / "repo"), env={"CI": "true"}, name="t")
        return command[command.index("--network") + 1]

    assert network_of(
        ["npx", "-y", "pnpm@9", "install", "--frozen-lockfile"]) == "bridge"
    assert network_of(
        ["npx", "-y", "pnpm@9", "--filter", "@s/p", "exec", "vitest",
         "run"]) == "none"


# --- demo trusted-fixture backend (sig EDGEVERDICT_DEMO_LOCAL_TESTS_V2) ---


def test_demo_backend_is_local_without_env(monkeypatch):
    """A stranger with no docker, no image, and no env vars gets a working
    demo: the chooser returns a real LocalBackend despite the unsafe
    opt-in being unset, because the demo fixture ships in this package."""
    from edgeverdict.cli import _demo_backend
    from edgeverdict.execution import LocalBackend

    monkeypatch.delenv("EDGEVERDICT_EXECUTION_BACKEND", raising=False)
    monkeypatch.delenv("EDGEVERDICT_ALLOW_UNSAFE_LOCAL", raising=False)
    backend = _demo_backend()
    assert isinstance(backend, LocalBackend)
    # Behaviour, not shape: the backend actually executes a command.
    # sys.executable is an absolute path, so this runs under an empty
    # env on any machine; a bare "node" here failed on macOS, where
    # homebrew's bin is not on the empty-env fallback PATH.
    import sys

    result = backend.run(
        [sys.executable, "-c", "print(6*7)"], cwd=".", env={}, timeout=30
    )
    assert result.returncode == 0
    assert "42" in result.stdout


def test_demo_backend_defers_to_explicit_env(monkeypatch):
    """An operator who explicitly chose a backend gets exactly that choice:
    the chooser steps aside so backend_from_env rules."""
    from edgeverdict.cli import _demo_backend

    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "docker")
    assert _demo_backend() is None


def test_local_backend_still_fails_closed_outside_the_demo(monkeypatch):
    """The trusted-fixture escape must not weaken the production guard:
    constructing LocalBackend the normal way without the opt-in still
    refuses to run."""
    import pytest

    from edgeverdict.execution import ExecutionConfigurationError, LocalBackend

    monkeypatch.delenv("EDGEVERDICT_ALLOW_UNSAFE_LOCAL", raising=False)
    with pytest.raises(ExecutionConfigurationError):
        LocalBackend()


# --- python lane under docker (sig EDGEVERDICT_PYLANE_DOCKER_TESTS_V1) ---


def _docker_backend_for_test(monkeypatch):
    monkeypatch.setattr(
        "edgeverdict.execution.shutil.which", lambda _: "/usr/bin/docker"
    )

    class Probe:
        returncode = 0

    monkeypatch.setattr(
        "edgeverdict.execution.subprocess.run", lambda *a, **k: Probe()
    )
    return DockerBackend(
        image="edgeverdict-sandbox:test",
        limits=DockerLimits(max_output_bytes=4096),
        log=lambda *a, **k: None,
    )


def test_host_interpreter_maps_to_container_python(monkeypatch, tmp_path):
    """The pytest profile's sys.executable is a host venv path outside the
    mount root; the built docker command must run the container's python
    instead, or the lane dies at exec."""
    import sys

    backend = _docker_backend_for_test(monkeypatch)
    root = tmp_path / "edgeverdict_warm_test"
    cwd = root / "repo"
    cwd.mkdir(parents=True)
    command = backend._docker_command(
        [sys.executable, "-m", "pytest", "--collect-only"],
        cwd=str(cwd), env={"CI": "true"}, name="ev-test",
    )
    image_at = command.index("edgeverdict-sandbox:test")
    argv = command[image_at + 1:]
    assert argv[0] == "python"
    # The user-site env rides along on the mount, where writes and
    # native-extension loading both work.
    assert "--env" in command
    assert "PYTHONUSERBASE=/edgeverdict/.edgeverdict-pyuser" in command
    assert sys.executable not in command
    assert argv[1:3] == ["-m", "pytest"]


def test_non_interpreter_argv_passes_through(monkeypatch, tmp_path):
    """npm stays npm: the mapping fires only on this process's interpreter."""
    backend = _docker_backend_for_test(monkeypatch)
    root = tmp_path / "edgeverdict_warm_test"
    cwd = root / "repo"
    cwd.mkdir(parents=True)
    command = backend._docker_command(
        ["npm", "install", "--no-audit"],
        cwd=str(cwd), env={}, name="ev-test",
    )
    image_at = command.index("edgeverdict-sandbox:test")
    assert command[image_at + 1] == "npm"


def test_pytest_profile_installs_under_docker(monkeypatch, tmp_path):
    """Docker selected -> the profile installs the project with its declared
    test extra; local selected -> no install, exactly the old behavior."""
    from edgeverdict.config import Config, build_profile

    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"fixture\"\nversion = \"0\"\n"
        "[project.optional-dependencies]\ntest = [\"pytest\"]\n"
    )
    (tmp_path / "test_x.py").write_text("def test_ok():\n    assert True\n")

    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "docker")
    prof = build_profile(str(tmp_path), Config(), "test_x.py")
    assert prof.install_cmd == [
        "python", "-m", "pip", "install", "--quiet", "--user",
        "--no-cache-dir", "-e", ".[test]"
    ]

    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "local")
    prof_local = build_profile(str(tmp_path), Config(), "test_x.py")
    assert prof_local.install_cmd == []


def test_python_target_project_dir_prefers_pyproject(tmp_path):
    """A python target in a mixed monorepo resolves to its own package dir
    even when a JS lockfile sits at the root."""
    import os

    from edgeverdict.config import detect_project_dir

    (tmp_path / "pnpm-lock.yaml").write_text("")
    pkg = tmp_path / "libs" / "sdk" / "src"
    pkg.mkdir(parents=True)
    (tmp_path / "libs" / "sdk" / "pyproject.toml").write_text(
        "[project]\nname = \"sdk\"\nversion = \"0\"\n"
    )
    (pkg / "mod.py").write_text("x = 1\n")
    assert detect_project_dir(str(tmp_path), "libs/sdk/src/mod.py") == os.path.join(
        "libs", "sdk"
    )
    # JS target in the same repo keeps lockfile-at-root semantics.
    js = tmp_path / "web"
    js.mkdir()
    (js / "index.ts").write_text("export {}\n")
    assert detect_project_dir(str(tmp_path), "web/index.ts") == "."


def test_pytest_profile_reads_dependency_groups(monkeypatch, tmp_path):
    """A PEP 735 [dependency-groups] test table (deepagents-style) lands in
    the install command as explicit requirements, includes resolved."""
    from edgeverdict.config import Config, build_profile

    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"fixture\"\nversion = \"0\"\n"
        "[dependency-groups]\n"
        "lint = [\"ruff\"]\n"
        "test = [\"pytest-socket\", {include-group = \"lint\"}]\n"
    )
    (tmp_path / "test_x.py").write_text("def test_ok():\n    assert True\n")
    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "docker")
    prof = build_profile(str(tmp_path), Config(), "test_x.py")
    assert prof.install_cmd == [
        "python", "-m", "pip", "install", "--quiet", "--user",
        "--no-cache-dir", "-e", ".", "pytest-socket", "ruff",
    ]


# --- pytest monorepo paths (sig EDGEVERDICT_PYMONO_PATH_TESTS_V1) ---


def _mono(tmp_path):
    import os

    pkg = tmp_path / "libs" / "pkg"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "pyproject.toml").write_text(
        "[project]\nname = \"pkg\"\nversion = \"0\"\n"
    )
    (pkg / "tests" / "test_x.py").write_text("def test_ok():\n    assert True\n")
    return str(tmp_path), os.path.join("libs", "pkg")


def test_pytest_smoke_path_is_project_relative_in_monorepo(monkeypatch, tmp_path):
    """cwd is the project dir, so a repo-relative smoke path doubles the
    prefix (libs/pkg/libs/pkg/...) and collects nothing — the deepagents
    STOPPED run. The command path must be project-relative."""
    from edgeverdict.config import Config, build_profile

    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "local")
    repo, pd = _mono(tmp_path)
    prof = build_profile(repo, Config(), "libs/pkg/tests/test_x.py",
                         project_dir=pd)
    assert prof.smoke_cmd[-1] == "tests/test_x.py"


def test_pytest_smoke_path_unchanged_for_root_projects(monkeypatch, tmp_path):
    """Root-project repos (the whole python gauntlet) keep the exact old
    path — relpath against '.' is the identity."""
    from edgeverdict.config import Config, build_profile

    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "local")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"x\"\nversion = \"0\"\n"
    )
    (tmp_path / "test_x.py").write_text("def test_ok():\n    assert True\n")
    prof = build_profile(str(tmp_path), Config(), "test_x.py")
    assert prof.smoke_cmd[-1] == "test_x.py"


def test_verifier_translates_pytest_command_path(monkeypatch, tmp_path):
    """The run-time commands (serial/batch) get the project-relative path;
    injection keeps writing at the repo-relative path."""
    from edgeverdict.config import Config, build_profile
    from edgeverdict.verifiers.finding_verifier import FindingVerifier
    from edgeverdict.verifiers.pytest_harness import PytestHarness

    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "local")
    monkeypatch.setenv("EDGEVERDICT_ALLOW_UNSAFE_LOCAL", "1")
    repo, pd = _mono(tmp_path)
    prof = build_profile(repo, Config(), "libs/pkg/tests/test_x.py",
                         project_dir=pd)
    v = FindingVerifier(repo, prof, tests_file="libs/pkg/tests/test_x.py",
                        project_dir=pd, harness=PytestHarness(),
                        log=lambda *a, **k: None)
    assert v._cmd_tests_file == "tests/test_x.py"
    assert v.tests_file == "libs/pkg/tests/test_x.py"
    cmd = v.harness.serial_command(prof, v._cmd_tests_file, "t", "/tmp/o.xml")
    assert any(a.startswith("tests/test_x.py") for a in cmd)


def test_resolver_prefers_unit_host_over_integration(tmp_path):
    """When unit and integration hosts both import the target, the
    integration one leaves the pool: a sandbox can't satisfy live-service
    suites (the deepagents integration twin imports ChatAnthropic)."""
    import os

    from edgeverdict.verifiers.pytest_harness import PytestHarness

    pkg = tmp_path / "libs" / "pkg"
    (pkg / "pkgmod").mkdir(parents=True)
    (pkg / "tests" / "unit_tests").mkdir(parents=True)
    (pkg / "tests" / "integration_tests").mkdir(parents=True)
    (pkg / "pkgmod" / "filesystem.py").write_text("def f():\n    return 1\n")
    (pkg / "tests" / "unit_tests" / "test_filesystem_init.py").write_text(
        "from pkgmod import filesystem\n\ndef test_a():\n    assert filesystem.f() == 1\n"
    )
    (pkg / "tests" / "integration_tests" / "test_filesystem_live.py").write_text(
        "from pkgmod import filesystem\n\ndef test_b():\n    assert filesystem.f() == 1\n"
    )
    picked = PytestHarness.default_tests_for(
        str(tmp_path), os.path.join("libs", "pkg", "pkgmod", "filesystem.py")
    )
    assert picked is not None
    assert "integration" not in picked
    assert picked.endswith("test_filesystem_init.py")


# --- host-file install supplement (sig EDGEVERDICT_HOSTFILE_INSTALL_TESTS_V1) ---


def test_host_file_imports_supplement_the_install(monkeypatch, tmp_path):
    """Undeclared external imports in the host tests file (and its
    conftest chain, which loads at collect) join the install command;
    stdlib, local modules, and relative imports stay out. The vitest
    lane gets this for free from package.json; python repos
    under-declare (posthog-python: one dev dep)."""
    from edgeverdict.config import Config, build_profile

    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"fixture\"\nversion = \"0\"\n"
    )
    (tmp_path / "localmod.py").write_text("x = 1\n")
    (tmp_path / "conftest.py").write_text(
        "import os\nimport pytest_socket\n"
    )
    (tmp_path / "test_x.py").write_text(
        "import json\n"
        "import localmod\n"
        "import yaml\n"
        "from responses import activate\n"
        "from . import nothing  # relative: excluded\n"
        "\ndef test_ok():\n    assert localmod.x == 1\n"
    )
    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "docker")
    prof = build_profile(str(tmp_path), Config(), "test_x.py")
    cmd = prof.install_cmd
    assert "PyYAML" in cmd            # mapped divergent name
    assert "responses" in cmd         # from-import external
    assert "pytest_socket" in cmd     # conftest chain included
    assert "json" not in cmd          # stdlib excluded
    assert "localmod" not in cmd      # repo-local excluded
    assert cmd[:8] == ["python", "-m", "pip", "install", "--quiet",
                       "--user", "--no-cache-dir", "-e"]


def test_supplement_uses_project_relative_host_in_monorepo(monkeypatch, tmp_path):
    """The supplement resolves the host file inside the project dir, so
    monorepo paths don't double the prefix here either."""
    from edgeverdict.config import Config, build_profile

    repo, pd = _mono(tmp_path)
    import os as _os

    (tmp_path / "libs" / "pkg" / "tests" / "test_x.py").write_text(
        "import yaml\n\ndef test_ok():\n    assert True\n"
    )
    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "docker")
    prof = build_profile(repo, Config(),
                         _os.path.join("libs", "pkg", "tests", "test_x.py"),
                         project_dir=pd)
    assert "PyYAML" in prof.install_cmd


# --- python free-imports v1 (sig EDGEVERDICT_PYFREE_IMPORTS_TESTS_V1) ---


def _freeimports_repo(tmp_path):

    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"pkg\"\nversion = \"0\"\n"
    )
    pkg = tmp_path / "pkgapi"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from pkgapi.core import make_widget\n")
    (pkg / "core.py").write_text(
        "from json import dumps\n\n\ndef make_widget():\n    return 1\n"
    )
    t = tmp_path / "tests"
    (t).mkdir()
    (t / "__init__.py").write_text("")
    (t / "test_helpers.py").write_text(
        "from pkgapi.core import make_widget\n\n"
        "class FakeModel:\n    def __init__(self):\n        self.x = 41\n\n"
        "def _bump(m):\n    return m.x + 1\n"
    )
    (t / "test_other.py").write_text(
        "from pkgapi import make_widget\n\n"
        "def helper_two():\n    return 2\n"
    )
    host = t / "test_host.py"
    host.write_text("import json\n\n\ndef test_existing():\n    assert True\n")
    return str(tmp_path), str(host)


def test_free_imports_bind_unique_definition_and_execute(tmp_path):
    """A helper class with exactly one definition site binds and the
    injected test EXECUTES to a real verdict — the deepagents
    FixedGenericFakeChatModel case in miniature."""
    import subprocess
    import sys

    from edgeverdict.verifiers.pytest_harness import PytestHarness

    repo, host = _freeimports_repo(tmp_path)
    pristine = open(host).read()
    code = ("def test_uses_helpers(self):\n"
            "    m = FakeModel()\n"
            "    assert _bump(m) == 42\n")
    injected, err = PytestHarness().inject(pristine, code, host_path=host)
    assert injected is not None
    assert "from tests.test_helpers import FakeModel, _bump" in injected
    assert "def test_uses_helpers():" in injected  # self stripped
    open(host, "w").write(injected)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", host + "::test_uses_helpers",
         "-x", "-q", "-p", "no:cacheprovider"],
        cwd=repo, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-800:]


def test_free_imports_refuse_ambiguous_definitions(tmp_path):
    """Two definition sites for one name = NO binding: the supabase
    `setup` collision minted false gaps under a looser rule."""
    from edgeverdict.verifiers.pytest_harness import PytestHarness

    repo, host = _freeimports_repo(tmp_path)
    (tmp_path / "tests" / "test_more.py").write_text(
        "class FakeModel:\n    pass\n"
    )
    pristine = open(host).read()
    code = "def test_x():\n    assert FakeModel is not None\n"
    injected, _ = PytestHarness().inject(pristine, code, host_path=host)
    assert "FakeModel" in injected
    assert "import FakeModel" not in injected  # stays free, fails honestly


def test_free_imports_adopt_repo_import_with_ancestor_collapse(tmp_path):
    """A name other test files import from pkgapi and pkgapi.core binds
    from the ancestor package (re-export pattern); unrelated modules
    would refuse."""
    from edgeverdict.verifiers.pytest_harness import PytestHarness

    repo, host = _freeimports_repo(tmp_path)
    pristine = open(host).read()
    code = "def test_w():\n    assert make_widget is not None\n"
    injected, _ = PytestHarness().inject(pristine, code, host_path=host)
    assert "from pkgapi import make_widget" in injected


def test_free_imports_refuse_unrelated_import_sources(tmp_path):
    """Same name imported from two UNRELATED modules refuses — that is
    genuine ambiguity, not a re-export."""
    from edgeverdict.verifiers.pytest_harness import PytestHarness

    repo, host = _freeimports_repo(tmp_path)
    (tmp_path / "tests" / "test_third.py").write_text(
        "from otherlib import make_widget\n"
    )
    pristine = open(host).read()
    code = "def test_w():\n    assert make_widget is not None\n"
    injected, _ = PytestHarness().inject(pristine, code, host_path=host)
    assert "import make_widget" not in injected


def test_free_imports_bind_from_target_module(tmp_path):
    """Rule T: the target module's own scope (including re-exports)
    vouches for a name."""

    from edgeverdict.verifiers.pytest_harness import PytestHarness

    repo, host = _freeimports_repo(tmp_path)
    pkg = tmp_path / "pkgapi"
    pristine = open(host).read()
    code = "def test_t():\n    assert dumps({}) == '{}'\n"
    injected, _ = PytestHarness().inject(
        pristine, code, host_path=host,
        target_path=str(pkg / "core.py"))
    assert "from pkgapi.core import dumps" in injected


# --- packaged sandbox dockerfile (sig EDGEVERDICT_SANDBOX_BUILD_TESTS_V1) ---


def test_packaged_dockerfile_matches_repo_copy():
    """The wheel ships the Dockerfile so pip users can build the image;
    this pin stops the packaged copy drifting from docker/Dockerfile.sandbox."""
    import os
    from importlib import resources

    packaged = resources.files("edgeverdict._sandbox").joinpath(
        "Dockerfile").read_bytes()
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo_copy = open(os.path.join(repo_root, "docker", "Dockerfile.sandbox"),
                     "rb").read()
    assert packaged == repo_copy


def test_sandbox_build_constructs_docker_command(monkeypatch, capsys):
    """sandbox-build materializes the packaged Dockerfile and runs docker
    build with the right tag and optional PYTHON_VERSION build arg."""
    import subprocess

    from edgeverdict.cli import sandbox_build

    seen = {}

    class P:
        returncode = 0

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return P()

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delenv("EDGEVERDICT_SANDBOX_IMAGE", raising=False)
    rc = sandbox_build(python_version="3.13.13")
    assert rc == 0
    cmd = seen["cmd"]
    assert cmd[:2] == ["docker", "build"]
    assert "edgeverdict-sandbox:latest" in cmd
    assert "PYTHON_VERSION=3.13.13" in " ".join(cmd)
    df = cmd[cmd.index("-f") + 1]
    import os
    assert os.path.basename(df) == "Dockerfile"


def test_fail_closed_error_names_sandbox_build(monkeypatch):
    """The pip-user dead end: the docker-required error must name a
    command that works without a repo checkout."""
    import pytest as _pytest

    from edgeverdict.execution import DockerBackend, ExecutionConfigurationError

    monkeypatch.setattr("edgeverdict.execution.shutil.which", lambda _: None)
    with _pytest.raises(ExecutionConfigurationError) as e:
        DockerBackend(log=lambda *a, **k: None)
    assert "sandbox-build" in str(e.value)


# --- supplement hardening (sig EDGEVERDICT_SUPPLEMENT_HARDENING_TESTS_V1) ---


def test_optional_py2_imports_stay_out_of_the_supplement(monkeypatch, tmp_path):
    """Imports inside try/except ImportError are optional by construction:
    posthog-python's py2 fallback `from Queue import Queue` must not become
    `pip install Queue` (which killed the whole install). Case differences
    from modern stdlib names are excluded too."""
    from edgeverdict.config import Config, build_profile

    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"fixture\"\nversion = \"0\"\n"
    )
    (tmp_path / "test_x.py").write_text(
        "try:\n"
        "    from queue import Queue\n"
        "except ImportError:\n"
        "    from Queue import Queue\n"
        "import parameterized\n"
        "\ndef test_ok():\n    assert Queue is not None\n"
    )
    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "docker")
    prof = build_profile(str(tmp_path), Config(), "test_x.py")
    assert "Queue" not in prof.install_cmd
    assert "parameterized" in prof.install_cmd


def test_profile_carries_declared_only_fallback(monkeypatch, tmp_path):
    """When the install carries supplements, the profile also carries a
    declared-deps-only fallback so a guessed name can never bench the run."""
    from edgeverdict.config import Config, build_profile

    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"fixture\"\nversion = \"0\"\n"
    )
    (tmp_path / "test_x.py").write_text(
        "import parameterized\n\ndef test_ok():\n    assert True\n"
    )
    monkeypatch.setenv("EDGEVERDICT_EXECUTION_BACKEND", "docker")
    prof = build_profile(str(tmp_path), Config(), "test_x.py")
    assert "parameterized" in prof.install_cmd
    assert prof.install_fallback_cmd is not None
    assert "parameterized" not in prof.install_fallback_cmd
    assert prof.install_fallback_cmd[:4] == ["python", "-m", "pip", "install"]


# --- phase-aware fsize (sig EDGEVERDICT_PHASE_FSIZE_TESTS_V1) ---


def test_install_gets_generous_fsize_execution_stays_tight(monkeypatch):
    """Install downloads legitimately large wheels (claude-agent-sdk ships
    ~85MB) and must not trip the per-file cap that guards runaway TEST
    writes. Install commands get the ceiling; test commands do not."""
    from edgeverdict.execution import DockerBackend, DockerLimits

    b = DockerBackend.__new__(DockerBackend)
    b.limits = DockerLimits()
    assert b._fsize(["python", "-m", "pip", "install", "-e", "."]) == \
        b.limits.install_file_size_bytes
    assert b._fsize(["npx", "-y", "pnpm@9", "install"]) == \
        b.limits.install_file_size_bytes
    assert b._fsize(["python", "-m", "pytest", "x.py"]) == \
        b.limits.file_size_bytes
    assert b._fsize(["npx", "vitest", "run"]) == b.limits.file_size_bytes
    assert b.limits.install_file_size_bytes >= 85 * 1024 * 1024


def test_docker_command_uses_phase_fsize(monkeypatch, tmp_path):
    """The built command carries the install ceiling for an install and the
    tight cap for a test run."""
    backend = _docker_backend_for_test(monkeypatch)
    root = tmp_path / "edgeverdict_warm_test"
    cwd = root / "repo"
    cwd.mkdir(parents=True)
    inst = backend._docker_command(
        ["python", "-m", "pip", "install", "-e", "."],
        cwd=str(cwd), env={}, name="ev-i")
    test = backend._docker_command(
        ["python", "-m", "pytest", "x.py"],
        cwd=str(cwd), env={}, name="ev-t")
    big = f"fsize={backend.limits.install_file_size_bytes}:{backend.limits.install_file_size_bytes}"
    small = f"fsize={backend.limits.file_size_bytes}:{backend.limits.file_size_bytes}"
    assert big in inst
    assert small in test
