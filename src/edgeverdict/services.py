"""Auto-provisioned test services (opt-in): EDGEVERDICT_AUTO_SERVICES=1.

Some packages' test suites need a live service -- supabase's pg-meta runs
hundreds of integration tests against a real Postgres. The package already
knows how to provide it: a test-service compose file (pg-meta ships
test/db/docker-compose.yml, and its own run-tests.sh brings it up). This
module rides that declaration instead of inventing infrastructure:

    detect the package's test compose
      -> bring it up with the package's OWN compose (their build, so the
         exact right image runs, never a stock stand-in)
      -> pick a free host port and inject it the way the compose expects
      -> construct the database URL and set EDGEVERDICT_DB_URL for the run
      -> tear down after (or keep warm: EDGEVERDICT_KEEP_SERVICES=1)

The variable this sets is the same one a human sets by hand for the
bring-your-own-environment mode, so everything downstream (the test
phase's host-gateway network, the localhost rewrite) is unchanged: this
module's whole job is to make the manual step automatic.

Scope guard: PER-PACKAGE test services only. A repository's full platform
compose (the one that boots the product) is never a test dependency, and
provisioning it would mean booting the world to test one file. Detection
therefore looks only under the target package's own test directories.

Honesty rules, same as everywhere else in this codebase:
- A service we cannot classify is never guessed at. We say what we found,
  say why we are not provisioning it, and run without it; if the tests
  needed it, the environment-failure layer reports that truthfully.
- A provisioning failure stops the run BEFORE any tokens are spent, with
  the compose's own stderr as the cause. No fake verdicts on a dead env.
- With the flag unset, or with EDGEVERDICT_DB_URL already set by a human,
  this module does nothing at all.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Iterator

__all__ = ["ServiceError", "ServicePlan", "auto_services", "detect_test_compose"]

# Compose accepts four spellings; all are looked for.
_COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml",
                  "compose.yml", "compose.yaml")

# Host-port side of a ports mapping: "${PG_TEST_PORT:-5432}:5432",
# "${PORT}:5432", or a bare "5433:5432".
_PORT_ENV_RE = re.compile(r"^\$\{(\w+)(?::-(\d+))?\}$")

_PORT_SCAN_SPAN = 100  # matches pg-meta's own 5432-5531 convention


class ServiceError(RuntimeError):
    """Provisioning was required and failed; the run must not proceed."""


@dataclass
class ServicePlan:
    """One provisionable service, parsed from a package's test compose."""

    compose_path: str            # absolute path to the compose file
    service: str                 # compose service key, e.g. "db"
    container_port: int          # the port inside the container
    port_env: str | None         # env var the compose reads the host port from
    default_port: int            # where the free-port scan starts
    user: str
    password: str
    kind: str = "postgres"       # the only classified kind today
    unrecognized: list[str] = field(default_factory=list)

    @property
    def compose_dir(self) -> str:
        return os.path.dirname(self.compose_path)

    @property
    def project_name(self) -> str:
        """Unique per compose location, stable across runs: collisions
        between checkouts are impossible and a kept-warm stack from a
        previous run is findable (pg-meta's run-tests.sh does the same
        with a hash of its package dir)."""
        digest = hashlib.sha1(
            os.path.abspath(self.compose_dir).encode()).hexdigest()[:8]
        return f"edgeverdict-{digest}"

    def url(self, host_port: int) -> str:
        return f"postgresql://{self.user}:{self.password}@localhost:{host_port}"


def _package_dir(repo_root: str, target: str) -> str:
    """The target's OWN package directory: nearest ancestor with a package
    manifest. This is deliberately NOT config.detect_project_dir, which
    answers where the INSTALL runs -- in a pnpm workspace that is the repo
    root (the lockfile rule), while the test-service compose lives in the
    package (supabase's is packages/pg-meta/test/db/). Two different
    questions, two different walks."""
    root = os.path.abspath(repo_root)
    d = os.path.dirname(os.path.abspath(os.path.join(root, target)))
    markers = ("package.json", "pyproject.toml", "setup.cfg", "setup.py")
    while True:
        if any(os.path.isfile(os.path.join(d, m)) for m in markers):
            rel = os.path.relpath(d, root)
            return "." if rel == os.curdir else rel
        if os.path.normpath(d) == os.path.normpath(root) or len(d) <= len(root):
            return "."
        parent = os.path.dirname(d)
        if parent == d:
            return "."
        d = parent


def _load_yaml(path: str) -> dict | None:
    try:
        import yaml  # lazy: only the opt-in path needs it
    except ImportError as e:  # pragma: no cover - dependency is declared
        raise ServiceError(
            "EDGEVERDICT_AUTO_SERVICES needs PyYAML to read the compose "
            "file (pip install pyyaml)") from e
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:  # noqa: BLE001 - unparseable compose = not a plan
        return None
    return data if isinstance(data, dict) else None


def _parse_ports_entry(entry: object) -> tuple[str | None, int, int] | None:
    """(port_env, default_host_port, container_port) from one short-form
    ports mapping, or None when the shape is not one we support."""
    if not isinstance(entry, str) or ":" not in entry:
        return None
    host, _, container = entry.rpartition(":")
    if not container.isdigit():
        return None
    m = _PORT_ENV_RE.match(host)
    if m:
        env_name, default = m.group(1), m.group(2)
        return env_name, int(default or container), int(container)
    if host.isdigit():
        return None, int(host), int(container)
    return None


def _plan_from_service(compose_path: str, name: str,
                       svc: dict) -> ServicePlan | None:
    """Classify one compose service. Only shapes we can construct a
    truthful URL for become a plan; anything else returns None."""
    env = svc.get("environment") or {}
    if isinstance(env, list):  # compose also allows KEY=VALUE list form
        env = dict(item.split("=", 1) for item in env
                   if isinstance(item, str) and "=" in item)
    if not isinstance(env, dict):
        return None
    password = env.get("POSTGRES_PASSWORD")
    if not isinstance(password, str) or "${" in password:
        return None  # absent or interpolated: we never guess credentials
    user = env.get("POSTGRES_USER", "postgres")
    if not isinstance(user, str) or "${" in user:
        return None
    ports = svc.get("ports") or []
    parsed = None
    for entry in ports:
        parsed = _parse_ports_entry(entry)
        if parsed:
            break
    if not parsed:
        return None
    port_env, default_port, container_port = parsed
    return ServicePlan(compose_path=compose_path, service=name,
                       container_port=container_port, port_env=port_env,
                       default_port=default_port, user=user,
                       password=password)


def detect_test_compose(repo_root: str, project_dir: str) -> ServicePlan | None:
    """Find the target package's test-service compose, if it has one.

    Looks only under directories named test/tests inside the package
    (up to two levels deep below them) -- the per-package declaration is
    the point, and the repository's platform stack must never match.
    """
    base = os.path.join(os.path.abspath(repo_root), project_dir)
    candidates: list[str] = []
    for test_dir in ("test", "tests"):
        td = os.path.join(base, test_dir)
        if not os.path.isdir(td):
            continue
        for dirpath, dirnames, filenames in os.walk(td):
            depth = os.path.relpath(dirpath, td).count(os.sep)
            if depth >= 2:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d != "node_modules"]
            for fname in filenames:
                if fname in _COMPOSE_NAMES:
                    candidates.append(os.path.join(dirpath, fname))
    if not candidates:
        return None
    # Shortest path wins: the compose closest to the test root is the
    # suite-level declaration, not a fixture's.
    candidates.sort(key=lambda p: (p.count(os.sep), p))
    compose_path = candidates[0]
    data = _load_yaml(compose_path)
    services = (data or {}).get("services")
    if not isinstance(services, dict) or not services:
        return None
    skipped: list[str] = []
    for name, svc in services.items():
        if not isinstance(svc, dict):
            skipped.append(str(name))
            continue
        plan = _plan_from_service(compose_path, str(name), svc)
        if plan is not None:
            plan.unrecognized = skipped + [
                str(n) for n in services if str(n) != plan.service
                and str(n) not in skipped]
            return plan
        skipped.append(str(name))
    # A compose exists but nothing in it is a shape we provision: report
    # honestly via a plan-less marker so the caller can say what it found.
    marker = ServicePlan(compose_path=compose_path, service="",
                         container_port=0, port_env=None, default_port=0,
                         user="", password="", kind="unrecognized",
                         unrecognized=skipped)
    return marker


def _free_port(start: int, span: int = _PORT_SCAN_SPAN) -> int:
    for port in range(start, start + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise ServiceError(
        f"no free port in {start}-{start + span - 1} for the test service")


def _compose_cmd(plan: ServicePlan, *args: str) -> list[str]:
    return ["docker", "compose", "--project-name", plan.project_name,
            "-f", plan.compose_path, *args]


def _run(cmd: list[str], cwd: str, env: dict[str, str],
         timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=timeout)


def _reuse_port(plan: ServicePlan, env: dict[str, str]) -> int | None:
    """A kept-warm stack from a previous run: ask compose which host port
    the service is published on. Any failure just means no warm stack."""
    try:
        r = _run(_compose_cmd(plan, "port", plan.service,
                              str(plan.container_port)),
                 cwd=plan.compose_dir, env=env, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip().splitlines()
    if not out or ":" not in out[-1]:
        return None
    port = out[-1].rsplit(":", 1)[-1]
    return int(port) if port.isdigit() else None


def provision(plan: ServicePlan, log=print) -> tuple[int, bool]:
    """Bring the service up (or find it already warm). Returns
    (host_port, reused). Raises ServiceError when required and failed."""
    timeout = int(os.environ.get("EDGEVERDICT_SERVICES_TIMEOUT", "600"))
    env = dict(os.environ)
    warm = _reuse_port(plan, env)
    if warm is not None:
        return warm, True
    port = _free_port(plan.default_port)
    if plan.port_env:
        env[plan.port_env] = str(port)
    elif port != plan.default_port:
        # Fixed-port compose and the port is taken: injecting is impossible
        # without editing their file, which we never do.
        raise ServiceError(
            f"test service wants fixed port {plan.default_port} and it is "
            "busy; free it or set EDGEVERDICT_DB_URL to a database you run")
    # Their own bootstrap does down-then-up so stale state never lingers.
    with contextlib.suppress(Exception):
        _run(_compose_cmd(plan, "down"), cwd=plan.compose_dir, env=env,
             timeout=timeout)
    try:
        r = _run(_compose_cmd(plan, "up", "--detach", "--wait"),
                 cwd=plan.compose_dir, env=env, timeout=timeout)
    except FileNotFoundError as e:
        raise ServiceError(
            "docker is required to provision the test service "
            "(EDGEVERDICT_AUTO_SERVICES=1) and was not found") from e
    except subprocess.TimeoutExpired as e:
        raise ServiceError(
            f"test service did not become healthy within {timeout}s "
            "(EDGEVERDICT_SERVICES_TIMEOUT raises it); first run builds "
            "the package's own image, which can be slow") from e
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-8:]
        raise ServiceError("test service failed to start:\n  "
                           + "\n  ".join(tail))
    return port, False


def teardown(plan: ServicePlan, log=print) -> None:
    env = dict(os.environ)
    with contextlib.suppress(Exception):
        _run(_compose_cmd(plan, "down"), cwd=plan.compose_dir, env=env,
             timeout=120)


@contextlib.contextmanager
def auto_services(repo_root: str, target: str, log=print) -> Iterator[None]:
    """The whole opt-in lifecycle around one run. Does nothing unless
    EDGEVERDICT_AUTO_SERVICES=1; defers to a human-set EDGEVERDICT_DB_URL;
    otherwise detect -> provision -> set the URL -> yield -> restore the
    environment -> tear down (unless EDGEVERDICT_KEEP_SERVICES=1)."""
    if os.environ.get("EDGEVERDICT_AUTO_SERVICES", "").strip() != "1":
        yield
        return
    if os.environ.get("EDGEVERDICT_DB_URL", "").strip():
        log("auto-services: EDGEVERDICT_DB_URL is already set; "
            "using it as-is (nothing provisioned)")
        yield
        return
    project_dir = _package_dir(os.path.abspath(repo_root), target)
    plan = detect_test_compose(repo_root, project_dir)
    if plan is None:
        log("auto-services: no test-service compose under "
            f"{project_dir if project_dir != '.' else 'the package'}; "
            "running without services")
        yield
        return
    rel = os.path.relpath(plan.compose_path, os.path.abspath(repo_root))
    if plan.kind == "unrecognized":
        names = ", ".join(plan.unrecognized) or "none"
        log(f"auto-services: found {rel} but could not classify a "
            f"provisionable service in it (services: {names}); running "
            "without it. Set EDGEVERDICT_DB_URL manually if the suite "
            "needs a database.")
        yield
        return
    port, reused = provision(plan, log=log)
    url = plan.url(port)
    verb = "reusing warm" if reused else "started"
    log(f"auto-services: {verb} '{plan.service}' from {rel} on port {port} "
        "(the package's own compose); EDGEVERDICT_DB_URL set for this run")
    if plan.unrecognized:
        log("auto-services: note: additional services not provisioned: "
            + ", ".join(plan.unrecognized))
    prior = os.environ.get("EDGEVERDICT_DB_URL")
    os.environ["EDGEVERDICT_DB_URL"] = url
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("EDGEVERDICT_DB_URL", None)
        else:  # pragma: no cover - defensive: we only enter when unset
            os.environ["EDGEVERDICT_DB_URL"] = prior
        if os.environ.get("EDGEVERDICT_KEEP_SERVICES", "").strip() == "1":
            log(f"auto-services: kept '{plan.service}' running "
                f"(EDGEVERDICT_KEEP_SERVICES=1); stop it with: "
                f"docker compose --project-name {plan.project_name} "
                f"-f {rel} down")
        else:
            teardown(plan, log=log)
