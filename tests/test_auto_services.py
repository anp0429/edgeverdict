"""EDGEVERDICT_AUTO_SERVICES_TESTS_V1

The opt-in test-service provisioner (services.py). Docker itself is mocked
at the subprocess boundary (this suite must run keyless and daemonless);
the detection and parsing half runs against the REAL shape of supabase
pg-meta's test/db/docker-compose.yml, copied verbatim below, because that
file is the case this feature exists for.
"""

from __future__ import annotations

import os
import socket
import subprocess
from unittest import mock

import pytest

from edgeverdict import services
from edgeverdict.services import (
    ServiceError,
    auto_services,
    detect_test_compose,
    provision,
)

# supabase/supabase packages/pg-meta/test/db/docker-compose.yml, verbatim.
PG_META_COMPOSE = """\
services:
  db:
    build: .
    ports:
      - ${PG_TEST_PORT:-5432}:5432
    volumes:
      - .:/docker-entrypoint-initdb.d
    environment:
      POSTGRES_PASSWORD: postgres
    command: postgres -c config_file=/etc/postgresql/postgresql.conf
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 1s
      timeout: 2s
      retries: 10
      start_period: 2s
"""


def _write(tmp_path, rel, content):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return str(p)


@pytest.fixture()
def pg_meta_repo(tmp_path):
    """A repo shaped like supabase: the package's test compose two levels
    down, and a platform compose at the repo root that must NOT match."""
    _write(tmp_path, "packages/pg-meta/test/db/docker-compose.yml",
           PG_META_COMPOSE)
    _write(tmp_path, "packages/pg-meta/package.json", "{}")
    _write(tmp_path, "pnpm-lock.yaml", "lockfileVersion: 9\n")
    _write(tmp_path, "docker/docker-compose.yml",
           "services:\n  kong:\n    image: kong\n")
    return str(tmp_path)


def test_detects_pg_meta_shape_and_parses_it(pg_meta_repo):
    plan = detect_test_compose(pg_meta_repo, "packages/pg-meta")
    assert plan is not None and plan.kind == "postgres"
    assert plan.service == "db"
    assert plan.port_env == "PG_TEST_PORT"
    assert plan.default_port == 5432
    assert plan.container_port == 5432
    assert plan.user == "postgres" and plan.password == "postgres"
    assert plan.url(5437) == "postgresql://postgres:postgres@localhost:5437"
    assert plan.project_name.startswith("edgeverdict-")


def test_platform_stack_at_repo_root_never_matches(pg_meta_repo):
    # Scope guard: from the repo root's perspective there is no test/
    # compose, and docker/docker-compose.yml (the product stack) must not
    # be treated as a test dependency.
    assert detect_test_compose(pg_meta_repo, ".") is None


def test_no_compose_means_none(tmp_path):
    _write(tmp_path, "packages/ui/test/basic.test.ts", "test('x',()=>{})")
    assert detect_test_compose(str(tmp_path), "packages/ui") is None


def test_compose_yaml_spelling_found(tmp_path):
    _write(tmp_path, "pkg/tests/compose.yaml", PG_META_COMPOSE)
    plan = detect_test_compose(str(tmp_path), "pkg")
    assert plan is not None and plan.kind == "postgres"


def test_fixed_port_mapping_parses(tmp_path):
    fixed = PG_META_COMPOSE.replace("${PG_TEST_PORT:-5432}:5432",
                                    "5433:5432")
    _write(tmp_path, "pkg/test/docker-compose.yml", fixed)
    plan = detect_test_compose(str(tmp_path), "pkg")
    assert plan is not None and plan.port_env is None
    assert plan.default_port == 5433 and plan.container_port == 5432


def test_unclassifiable_service_becomes_honest_marker(tmp_path):
    _write(tmp_path, "pkg/test/docker-compose.yml",
           "services:\n  redis:\n    image: redis\n    ports:\n"
           "      - 6379:6379\n")
    plan = detect_test_compose(str(tmp_path), "pkg")
    assert plan is not None and plan.kind == "unrecognized"
    assert plan.unrecognized == ["redis"]


def test_interpolated_password_is_never_guessed(tmp_path):
    tricky = PG_META_COMPOSE.replace("POSTGRES_PASSWORD: postgres",
                                     "POSTGRES_PASSWORD: ${PW:-secret}")
    _write(tmp_path, "pkg/test/docker-compose.yml", tricky)
    plan = detect_test_compose(str(tmp_path), "pkg")
    assert plan is not None and plan.kind == "unrecognized"


def test_free_port_skips_a_bound_port(pg_meta_repo):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        busy = s.getsockname()[1]
        got = services._free_port(busy)
        assert got != busy and got == busy + 1


def _completed(rc=0, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=rc,
                                       stdout=out, stderr=err)


def test_provision_cold_runs_down_then_up_with_injected_port(pg_meta_repo):
    plan = detect_test_compose(pg_meta_repo, "packages/pg-meta")
    calls = []

    def fake_run(cmd, cwd=None, env=None, **kw):
        calls.append((cmd, cwd, env))
        if "port" in cmd:
            return _completed(rc=1)  # no warm stack
        return _completed()

    with mock.patch.object(services.subprocess, "run",
                           side_effect=fake_run):
        port, reused = provision(plan, log=lambda *_: None)
    assert reused is False and 5432 <= port <= 5531
    verbs = [c[0] for c in calls]
    assert any("down" in v for v in verbs)
    up = next(c for c in calls if "up" in c[0])
    assert "--detach" in up[0] and "--wait" in up[0]
    assert up[1] == plan.compose_dir
    assert up[2]["PG_TEST_PORT"] == str(port)
    assert "--project-name" in up[0] and plan.project_name in up[0]


def test_provision_reuses_a_warm_stack(pg_meta_repo):
    plan = detect_test_compose(pg_meta_repo, "packages/pg-meta")
    calls = []

    def fake_run(cmd, cwd=None, env=None, **kw):
        calls.append(cmd)
        if "port" in cmd:
            return _completed(out="0.0.0.0:5437\n")
        raise AssertionError("must not start anything when warm")

    with mock.patch.object(services.subprocess, "run",
                           side_effect=fake_run):
        port, reused = provision(plan, log=lambda *_: None)
    assert (port, reused) == (5437, True)
    assert all("up" not in c for c in calls)


def test_provision_failure_raises_with_compose_stderr(pg_meta_repo):
    plan = detect_test_compose(pg_meta_repo, "packages/pg-meta")

    def fake_run(cmd, cwd=None, env=None, **kw):
        if "port" in cmd:
            return _completed(rc=1)
        if "up" in cmd:
            return _completed(rc=1, err="no space left on device")
        return _completed()

    with mock.patch.object(services.subprocess, "run",
                           side_effect=fake_run):
        with pytest.raises(ServiceError, match="no space left"):
            provision(plan, log=lambda *_: None)


def test_lifecycle_sets_url_then_restores_and_tears_down(
        pg_meta_repo, monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_AUTO_SERVICES", "1")
    monkeypatch.delenv("EDGEVERDICT_DB_URL", raising=False)
    monkeypatch.delenv("EDGEVERDICT_KEEP_SERVICES", raising=False)
    downs = []

    def fake_run(cmd, cwd=None, env=None, **kw):
        if "port" in cmd:
            return _completed(rc=1)
        if "down" in cmd:
            downs.append(cmd)
        return _completed()

    seen = {}
    with mock.patch.object(services.subprocess, "run",
                           side_effect=fake_run):
        with auto_services(pg_meta_repo,
                           "packages/pg-meta/src/sql/tables.ts",
                           log=lambda *_: None):
            seen["url"] = os.environ.get("EDGEVERDICT_DB_URL", "")
    assert seen["url"].startswith("postgresql://postgres:postgres@localhost:")
    assert "EDGEVERDICT_DB_URL" not in os.environ
    # one pre-up down (stale clear) + one teardown down
    assert len(downs) == 2


def test_keep_services_skips_teardown(pg_meta_repo, monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_AUTO_SERVICES", "1")
    monkeypatch.setenv("EDGEVERDICT_KEEP_SERVICES", "1")
    monkeypatch.delenv("EDGEVERDICT_DB_URL", raising=False)
    downs = []

    def fake_run(cmd, cwd=None, env=None, **kw):
        if "port" in cmd:
            return _completed(rc=1)
        if "down" in cmd:
            downs.append(cmd)
        return _completed()

    with mock.patch.object(services.subprocess, "run",
                           side_effect=fake_run):
        with auto_services(pg_meta_repo,
                           "packages/pg-meta/src/sql/tables.ts",
                           log=lambda *_: None):
            pass
    assert len(downs) == 1  # only the pre-up stale clear, no teardown


def test_flag_off_touches_nothing(pg_meta_repo, monkeypatch):
    monkeypatch.delenv("EDGEVERDICT_AUTO_SERVICES", raising=False)
    with mock.patch.object(services.subprocess, "run") as run:
        with auto_services(pg_meta_repo,
                           "packages/pg-meta/src/sql/tables.ts"):
            pass
    run.assert_not_called()


def test_human_set_db_url_wins(pg_meta_repo, monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_AUTO_SERVICES", "1")
    monkeypatch.setenv("EDGEVERDICT_DB_URL", "postgresql://me@localhost:9")
    with mock.patch.object(services.subprocess, "run") as run:
        with auto_services(pg_meta_repo,
                           "packages/pg-meta/src/sql/tables.ts",
                           log=lambda *_: None):
            assert os.environ["EDGEVERDICT_DB_URL"].endswith(":9")
    run.assert_not_called()


def test_unrecognized_service_yields_without_provisioning(
        tmp_path, monkeypatch):
    monkeypatch.setenv("EDGEVERDICT_AUTO_SERVICES", "1")
    monkeypatch.delenv("EDGEVERDICT_DB_URL", raising=False)
    _write(tmp_path, "pkg/test/docker-compose.yml",
           "services:\n  redis:\n    image: redis\n")
    _write(tmp_path, "pkg/package.json", "{}")
    notes = []
    with mock.patch.object(services.subprocess, "run") as run:
        with auto_services(str(tmp_path), "pkg/src/x.ts",
                           log=notes.append):
            pass
    run.assert_not_called()
    assert any("could not classify" in n for n in notes)


def test_run_review_converts_service_error_to_exit_1(monkeypatch, tmp_path):
    from edgeverdict import api

    def boom(repo, target, log=print):
        raise ServiceError("test service failed to start")

    monkeypatch.setattr(api, "auto_services",
                        lambda *a, **kw: _raising_cm(boom))
    req = api.ReviewRequest(repo=str(tmp_path), target="x.py")
    lines = []
    result = api.run_review(req, log=lines.append)
    assert result.exit_code == 1
    assert any("failed to start" in ln for ln in lines)


class _raising_cm:
    def __init__(self, fn):
        self.fn = fn

    def __enter__(self):
        self.fn(None, None)

    def __exit__(self, *a):
        return False
