#!/usr/bin/env python3
# EDGEVERDICT_SANDBOX_DEMO_DIAG_V3
"""Trace the demo's own install, command by command.

A plain `npm install` of vitest 3.2.6 succeeds inside the sandbox (52 packages,
12s), so the resource caps are innocent. The demo's install still dies in ~1.9s
with nothing on either stream. That points at the demo's setup path rather than
the container, so this replicates the demo exactly and logs every command the
backend runs, with full output and the resolved paths.

    cd ~/Documents/agentboard
    python3 sandbox_demo_diag_v3.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ["EDGEVERDICT_EXECUTION_BACKEND"] = "docker"
os.environ["EDGEVERDICT_SANDBOX_NETWORK"] = "install"


def main() -> int:
    if shutil.which("docker") is None:
        print("docker not on PATH")
        return 2

    from edgeverdict.cli import TARGET_DIR, _demo_profile
    from edgeverdict.execution import _warm_root
    from edgeverdict.verifiers.finding_verifier import FindingVerifier

    work = tempfile.mkdtemp(prefix="edgeverdict_demo_")
    target = os.path.join(work, "target")
    shutil.copytree(TARGET_DIR, target)

    print("== what the demo copies ==")
    print(f"  source : {TARGET_DIR}")
    print(f"  target : {target}")
    for entry in sorted(os.listdir(target)):
        path = os.path.join(target, entry)
        kind = "dir " if os.path.isdir(path) else "file"
        print(f"    {kind} {entry}")
    if os.path.isdir(os.path.join(TARGET_DIR, "node_modules")):
        print("  NOTE: the SOURCE demo target has node_modules; it was copied "
              "into the sandbox. A macOS-built tree inside a Linux container "
              "is a strong suspect.")

    profile = _demo_profile()
    print("\n== the demo profile ==")
    print(f"  install_cmd : {profile.install_cmd}")
    print(f"  test_base   : {profile.test_base}")
    print(f"  smoke_cmd   : {profile.smoke_cmd}")
    print(f"  env         : {profile.env}")

    verifier = FindingVerifier(target, profile, tests_file="demo.test.js",
                               timeout=300)

    backend = verifier._execution_backend
    original = backend.run

    def traced(args, *, cwd, env, timeout):
        print(f"\n  >>> {' '.join(args)}")
        print(f"      cwd        : {cwd}")
        print(f"      mount root : {_warm_root(cwd)}")
        proc = original(args, cwd=cwd, env=env, timeout=timeout)
        print(f"      rc         : {proc.returncode}")
        for stream, label in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            text = (stream or "").strip()
            lines = [ln for ln in text.splitlines() if "tini" not in ln.lower()]
            if lines:
                print(f"      {label}:")
                for line in lines[-25:]:
                    print(f"        {line}")
        if not (proc.stdout or "").strip() and not (proc.stderr or "").strip():
            print("      (no output on either stream -- killed, not failed)")
        return proc

    backend.run = traced

    print("\n== preparing the sandbox, traced ==")
    verifier._ensure_warm()
    print(f"\n  warm repo  : {getattr(verifier, '_warm_repo', None)}")
    print(f"  prep_error : {getattr(verifier, '_prep_error', '') or '(none)'}")

    print("""
The first command above with a non-zero rc is the failure. If it printed
nothing at all, the process was killed rather than exiting on its own.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
