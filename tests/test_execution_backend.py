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
