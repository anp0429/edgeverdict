# EDGEVERDICT_DB_MODE_TESTS_V1
"""Mode 2 bring-your-own-database: EDGEVERDICT_DB_URL lets the sandbox reach a
host-run Postgres for integration tests (pg-meta's createTestDatabase). Test
commands get bridge network + host.docker.internal mapping + injected
DATABASE_URL (localhost rewritten to the host gateway). OPT-IN: with the env
unset, the sandbox is byte-identical to before (--network none, no DB)."""
from __future__ import annotations

from edgeverdict.execution import DockerBackend, DockerLimits


def _backend(policy: str = "none") -> DockerBackend:
    b = DockerBackend.__new__(DockerBackend)
    b.network_policy = policy
    b.docker = "docker"
    b.image = "img"
    b.limits = DockerLimits()
    return b


def _cmd(b: DockerBackend, args: list[str]) -> str:
    return " ".join(
        b._docker_command(args, cwd="/tmp/x/repo", env={}, name="t"))


def test_db_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("EDGEVERDICT_DB_URL", raising=False)
    assert _backend()._db_url() == ""


def test_default_test_command_stays_network_none(monkeypatch):
    monkeypatch.delenv("EDGEVERDICT_DB_URL", raising=False)
    cmd = _cmd(_backend("none"), ["vitest", "run"])
    assert "--network none" in cmd
    assert "add-host" not in cmd
    assert "DATABASE_URL" not in cmd


def test_db_mode_injects_url_and_host_for_test(monkeypatch):
    monkeypatch.setenv(
        "EDGEVERDICT_DB_URL", "postgresql://postgres:postgres@localhost:5432")
    cmd = _cmd(_backend("install"), ["vitest", "run"])
    assert "host.docker.internal:host-gateway" in cmd
    # localhost rewritten to the host gateway so it resolves inside the container
    assert ("DATABASE_URL=postgresql://postgres:postgres@"
            "host.docker.internal:5432") in cmd
    assert "--network bridge" in cmd


def test_db_url_localhost_rewritten(monkeypatch):
    monkeypatch.setenv(
        "EDGEVERDICT_DB_URL", "postgresql://u:p@localhost:5432")
    assert _backend()._container_db_url() == (
        "postgresql://u:p@host.docker.internal:5432")


def test_db_url_127_rewritten(monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_DB_URL", "postgresql://u:p@127.0.0.1:5432")
    assert "host.docker.internal" in _backend()._container_db_url()


def test_install_command_does_not_get_database_url(monkeypatch):
    # install doesn't need the DB; only the test phase reaches it
    monkeypatch.setenv(
        "EDGEVERDICT_DB_URL", "postgresql://postgres:postgres@localhost:5432")
    cmd = _cmd(_backend("install"), ["npx", "-y", "pnpm", "install"])
    assert "DATABASE_URL=" not in cmd


def test_db_mode_test_network_is_bridge(monkeypatch):
    monkeypatch.setenv(
        "EDGEVERDICT_DB_URL", "postgresql://postgres:postgres@localhost:5432")
    assert _backend("none")._network(["vitest", "run"]) == "bridge"


def test_db_mode_off_test_network_is_none(monkeypatch):
    monkeypatch.delenv("EDGEVERDICT_DB_URL", raising=False)
    assert _backend("none")._network(["vitest", "run"]) == "none"
