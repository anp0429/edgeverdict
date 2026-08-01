#!/usr/bin/env python3
# EDGEVERDICT_SANDBOX_NPM_DIAG_V1
"""Find out why `npm install` fails inside the hardened sandbox.

The gate reports `install failed` with a tini warning attached, which is a
benign message that merely happened to be last on stderr. This runs the same
backend and prints the FULL output of each step, so the actual error is visible.

    cd ~/Documents/agentboard
    python3 sandbox_npm_diag_v1.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

os.environ["EDGEVERDICT_EXECUTION_BACKEND"] = "docker"
os.environ["EDGEVERDICT_SANDBOX_NETWORK"] = "install"


def show(title: str, proc) -> None:
    print(f"\n--- {title}")
    print(f"    rc={proc.returncode}")
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        print("    stdout:")
        for line in out.splitlines()[-15:]:
            print(f"      {line}")
    if err:
        print("    stderr:")
        for line in err.splitlines()[-15:]:
            print(f"      {line}")
    if not out and not err:
        print("    (no output)")


def main() -> int:
    if shutil.which("docker") is None:
        print("docker not on PATH")
        return 2

    from edgeverdict.execution import backend_from_env

    backend = backend_from_env()
    work = tempfile.mkdtemp(prefix="edgeverdict-npmdiag-")
    env = dict(os.environ)

    def run(args, cwd=work):
        return backend.run(args, cwd=cwd, env=env, timeout=180)

    print("== is the toolchain even reachable ==")
    show("node --version", run(["node", "--version"]))
    show("npm --version", run(["npm", "--version"]))

    print("\n== identity and HOME ==")
    show("id + HOME", run([
        "sh", "-c",
        "id; echo HOME=$HOME; echo PATH=$PATH",
    ]))

    print("\n== can npm write where it needs to ==")
    show("HOME writable", run([
        "sh", "-c",
        'mkdir -p "$HOME" 2>&1 && touch "$HOME/.probe" 2>&1 '
        '&& echo HOME_WRITABLE || echo HOME_NOT_WRITABLE',
    ]))
    show("workdir writable", run([
        "sh", "-c",
        "touch ./.probe 2>&1 && echo WORKDIR_WRITABLE || echo WORKDIR_NOT_WRITABLE",
    ]))

    print("\n== does noexec on /tmp block execution ==")
    show("exec from /tmp", run([
        "sh", "-c",
        'printf "#!/bin/sh\\necho RAN_FROM_TMP\\n" > /tmp/p.sh '
        "&& chmod +x /tmp/p.sh && /tmp/p.sh 2>&1 || echo EXEC_BLOCKED",
    ]))

    print("\n== the real thing: npm install in a tiny package ==")
    pkg = os.path.join(work, "pkg")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "package.json"), "w") as fh:
        fh.write('{"name":"probe","version":"1.0.0","private":true,'
                 '"dependencies":{"is-number":"7.0.0"}}\n')
    show("npm install", run(["npm", "install", "--no-audit", "--no-fund"],
                            cwd=pkg))

    print("\n== same install with a writable npm cache under the mount ==")
    env["npm_config_cache"] = "/edgeverdict/.npm-cache"
    show("npm install (cache in mount)",
         run(["npm", "install", "--no-audit", "--no-fund"], cwd=pkg))

    backend.close()
    print("\nRead the first step above that fails. That is the cause;")
    print("everything after it is downstream noise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
