#!/usr/bin/env python3
# EDGEVERDICT_SANDBOX_NPM_DIAG_V2
"""Which sandbox resource cap kills the demo's install?

A one-package install succeeds inside the sandbox, but the demo's install
(vitest 3.2.6) dies in ~1.9s with no stdout at all. Silent death plus a fast
exit points at a limit, not at npm. The three candidates, all of which a tiny
package would never reach:

  tmpfs_size 512m  -- HOME is /tmp/edgeverdict-home, so npm's CACHE lives on
                      the tmpfs. A real dependency tree can exhaust it.
  nofile 1024      -- npm extracts concurrently; EMFILE is plausible.
  file_size_bytes  -- 64MB; exceeding it raises SIGXFSZ, which kills the
                      process with no message whatsoever.

Run:
    cd ~/Documents/agentboard
    python3 sandbox_npm_diag_v2.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

os.environ["EDGEVERDICT_EXECUTION_BACKEND"] = "docker"
os.environ["EDGEVERDICT_SANDBOX_NETWORK"] = "install"

REAL_DEP = {"name": "probe", "version": "1.0.0", "private": True,
            "devDependencies": {"vitest": "3.2.6"}}


def show(title: str, proc) -> None:
    print(f"\n--- {title}   rc={proc.returncode}")
    for stream, label in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
        text = (stream or "").strip()
        if not text:
            continue
        lines = [ln for ln in text.splitlines() if "tini" not in ln.lower()]
        if not lines:
            continue
        print(f"    {label}:")
        for line in lines[-20:]:
            print(f"      {line}")
    if not (proc.stdout or "").strip() and not (proc.stderr or "").strip():
        print("    (no output on either stream)")


def main() -> int:
    if shutil.which("docker") is None:
        print("docker not on PATH")
        return 2

    from edgeverdict.execution import backend_from_env

    backend = backend_from_env()
    work = tempfile.mkdtemp(prefix="edgeverdict-npm2-")
    env = dict(os.environ)

    def run(args, cwd, extra=None, timeout=300):
        e = dict(env)
        if extra:
            e.update(extra)
        return backend.run(args, cwd=cwd, env=e, timeout=timeout)

    print("== the limits actually in force ==")
    show("ulimits + tmpfs size", run(
        ["sh", "-c",
         "ulimit -n; echo '^ nofile'; ulimit -f; echo '^ fsize (blocks)'; "
         "df -h /tmp | tail -1; echo '^ tmpfs'"],
        work))

    def fresh_pkg(name):
        d = os.path.join(work, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "package.json"), "w") as fh:
            json.dump(REAL_DEP, fh)
        return d

    print("\n== A. the real demo install, exactly as the gate runs it ==")
    a = fresh_pkg("a")
    show("npm install (cache on tmpfs, default)",
         run(["npm", "install", "--no-audit", "--no-fund"], a))
    show("tmpfs after", run(["sh", "-c", "df -h /tmp | tail -1"], a))

    print("\n== B. same install, cache moved OFF the tmpfs into the mount ==")
    b = fresh_pkg("b")
    show("npm install (cache in bind mount)",
         run(["npm", "install", "--no-audit", "--no-fund"], b,
             extra={"npm_config_cache": "/edgeverdict/.npm-cache"}))

    print("\n== C. same install, cache in mount AND fd limit raised ==")
    c = fresh_pkg("c")
    show("npm install (cache in mount, ulimit -n 8192)",
         run(["sh", "-c",
              "ulimit -n 8192 2>/dev/null; "
              "npm install --no-audit --no-fund"], c,
             extra={"npm_config_cache": "/edgeverdict/.npm-cache"}))

    backend.close()
    print("""
How to read this:
  A fails, B works        -> the tmpfs is the cause. Fix: point npm's cache
                             into the bind mount (one env var in the backend).
  A and B fail, C works   -> the fd limit is the cause. Fix: raise nofile.
  all three fail          -> paste the output; the fsize cap (SIGXFSZ) or
                             something else is killing it silently.
  all three succeed       -> the limits are innocent and the difference is in
                             how the gate builds the warm copy, not the sandbox.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
