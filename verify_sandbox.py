#!/usr/bin/env python3
# EDGEVERDICT_SANDBOX_VERIFY_V2
"""Prove the hardened Docker backend actually works, at runtime.

The bundle's own tests are unit-level: they assert the docker COMMAND STRING
contains hardening flags and that a dict of secrets gets filtered. Neither
proves a container ever starts, that code executes inside it, or that a real
secret in the real environment fails to reach the real process.

This runs the actual backend, in the actual container, and checks the actual
promises. Run it from the hardened checkout after building the image:

    docker build -f docker/Dockerfile.sandbox -t edgeverdict-sandbox:latest .
    python3 verify_sandbox.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

CANARY_NAME = "OPENAI_API_KEY"
CANARY_VALUE = "sk-canary-must-never-appear-in-a-sandbox-abc123"
ALSO_CANARY = {
    "AWS_SECRET_ACCESS_KEY": "aws-canary-xyz789",
    "GITHUB_TOKEN": "ghp-canary-def456",
}

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    if detail and not passed:
        print(f"        {detail}")


def main() -> int:
    if shutil.which("docker") is None:
        print("docker not found on PATH. Install Docker Desktop first.")
        return 2

    image = os.environ.get("EDGEVERDICT_SANDBOX_IMAGE", "edgeverdict-sandbox:latest")
    have = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True, text=True,
    )
    if have.returncode != 0:
        print(f"image {image} not built. Run:")
        print("  docker build -f docker/Dockerfile.sandbox "
              f"-t {image} .")
        return 2

    # Put the canaries in the REAL environment, the way a developer's shell has
    # them. Everything below inherits this.
    os.environ[CANARY_NAME] = CANARY_VALUE
    os.environ.update(ALSO_CANARY)

    os.environ["EDGEVERDICT_EXECUTION_BACKEND"] = "docker"
    os.environ.setdefault("EDGEVERDICT_SANDBOX_NETWORK", "none")

    from edgeverdict.execution import backend_from_env

    workdir = tempfile.mkdtemp(prefix="edgeverdict-verify-")
    backend = backend_from_env()

    print("\n1. POSITIVE CONTROL: does anything execute inside the container")
    proc = backend.run(
        ["python", "-c", "print('hello from the sandbox')"],
        cwd=workdir, env=dict(os.environ), timeout=120,
    )
    executed = proc.returncode == 0 and "hello from the sandbox" in proc.stdout
    check("container executes code", executed,
          f"rc={proc.returncode} out={proc.stdout[:200]!r} err={proc.stderr[:200]!r}")
    if not executed:
        # Every check below asserts that something BAD did not happen. If the
        # container never starts, all of them "pass" vacuously and the script
        # reports a secure sandbox that does not exist. This is the positive
        # control: without it, a broken docker invocation looks like success.
        print("\nABORTING: nothing executed, so no downstream check means "
              "anything.\nFix the error above and re-run. Do not read the "
              "remaining checks as passes.")
        backend.close()
        return 1

    print("\n2. secrets do not reach the executed process")
    proc = backend.run(
        ["python", "-c", "import os;print('\\n'.join(f'{k}={v}' for k,v in os.environ.items()))"],
        cwd=workdir, env=dict(os.environ), timeout=120,
    )
    seen = proc.stdout
    check("provider key absent", CANARY_VALUE not in seen,
          "THE CANARY LEAKED INTO THE SANDBOX")
    for key, value in ALSO_CANARY.items():
        check(f"{key} absent", value not in seen, f"{key} leaked")
    check("secret NAMES absent too", CANARY_NAME not in seen,
          f"{CANARY_NAME} name present in sandbox env")

    print("\n3. the network is actually off (not just requested)")
    proc = backend.run(
        ["python", "-c",
         "import socket,sys\n"
         "try:\n"
         "    socket.create_connection(('1.1.1.1', 53), timeout=5)\n"
         "    print('NETWORK REACHABLE')\n"
         "except Exception as e:\n"
         "    print('blocked:', type(e).__name__)\n"],
        cwd=workdir, env=dict(os.environ), timeout=120,
    )
    check("egress blocked", "NETWORK REACHABLE" not in proc.stdout,
          f"out={proc.stdout[:200]!r}")

    print("\n4. the root filesystem is read-only")
    proc = backend.run(
        ["python", "-c",
         "try:\n"
         "    open('/etc/edgeverdict-should-fail','w').write('x')\n"
         "    print('ROOT WRITABLE')\n"
         "except Exception as e:\n"
         "    print('blocked:', type(e).__name__)\n"],
        cwd=workdir, env=dict(os.environ), timeout=120,
    )
    check("root fs read-only", "ROOT WRITABLE" not in proc.stdout,
          f"out={proc.stdout[:200]!r}")

    print("\n5. the process is not root")
    proc = backend.run(
        ["python", "-c", "import os;print('uid', os.getuid())"],
        cwd=workdir, env=dict(os.environ), timeout=120,
    )
    check("runs as non-root", "uid 0" not in proc.stdout,
          f"out={proc.stdout[:200]!r}")

    print("\n6. the host filesystem outside the mount is not visible")
    proc = backend.run(
        ["python", "-c",
         "import os\n"
         "p='/Users' if os.path.isdir('/Users') else '/home'\n"
         "print('HOST VISIBLE', p) if os.path.isdir(p) and os.listdir(p) else print('not visible')\n"],
        cwd=workdir, env=dict(os.environ), timeout=120,
    )
    check("host home not mounted", "HOST VISIBLE" not in proc.stdout,
          f"out={proc.stdout[:200]!r}")

    backend.close()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFAILED:")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    print("\nThe hardened backend does what it claims, at runtime.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
