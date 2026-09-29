"""sig EDGEVERDICT_PROMISES_V2 (proposer failures stop cause-first)

`edgeverdict promises`: execute what the repo's own docs promise.

sig EDGEVERDICT_PROMISES_V1

    edgeverdict promises                     propose + execute (needs a model)
    edgeverdict promises --probes run.json   replay saved probes, no model

Every model run saves its probes next to the board, so any verdict can be
replayed later without a key and without resampling.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict

from .promises import (
    Promise, collect, load_probes_file, repo_size, verify, _MAX_REPO_BYTES,
)
from .prove import exit_code_for, gap_details, verdict_block
from .review import render_review_html
from .providers import uses_anthropic


def promises_board_path(now=None) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
    return os.path.join(tempfile.gettempdir(),
                        f"edgeverdict_promises_board_{stamp}.html")


def _model_ready(model: str) -> str:
    """'' when the model is reachable in principle, else the exit line."""
    if uses_anthropic(model):
        if os.environ.get("ANTHROPIC_API_KEY", "").strip():
            return ""
        return "export ANTHROPIC_API_KEY=... (needed by the model you chose)"
    if (os.environ.get("OPENAI_API_KEY", "").strip()
            or os.environ.get("OPENAI_BASE_URL", "").strip()):
        return ""
    return ("export OPENAI_API_KEY=sk-...  or  export "
            "OPENAI_BASE_URL=http://localhost:11434/v1")


def save_probes(promises: list[Promise], path: str) -> None:
    keep = ("claim", "quote", "source", "scenario", "probe")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"promises": [{k: asdict(p)[k] for k in keep}
                                for p in promises]}, fh, indent=2)


def promises(args, *, backend=None, agent=None) -> int:
    repo = os.path.abspath(os.path.expanduser(args.repo))
    if not os.path.isdir(repo):
        print(f"STOPPED: {repo} is not a directory")
        return 1
    size = repo_size(repo)
    if size > _MAX_REPO_BYTES:
        print(f"STOPPED: repo is {size // (1024 * 1024)}MB without build/dep "
              "dirs; each probe gets its own copy, so promises is capped at "
              f"{_MAX_REPO_BYTES // (1024 * 1024)}MB for now")
        return 1

    board = args.board or promises_board_path()
    if args.probes:
        proposed = load_probes_file(args.probes)
        print(f"promises: replaying {len(proposed)} saved probe(s) from "
              f"{args.probes} (no model)")
    else:
        missing = _model_ready(args.model)
        if missing and agent is None:
            print("No model configured. promises needs one to read the docs "
                  "and write probes\n(the verdict never uses one).\n  "
                  + missing + "\n  or replay saved probes: "
                  "edgeverdict promises --probes <file.json>")
            return 1
        text = collect(repo)
        if not text.docs:
            print("NOTHING TO PROVE: no documentation found, so the repo "
                  "makes no written promises to execute")
            return 0
        print(f"promises: reading {len(text.docs)} doc(s) and "
              f"{len(text.code)} script/source file(s) in {repo}")
        if agent is None:
            from .agents.promise_agent import PromiseAgent
            agent = PromiseAgent(model=args.model, max_promises=args.max)
        try:
            proposed = agent.propose(text)
        except Exception as exc:  # noqa: BLE001 - cause first, never a traceback
            first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            print(f"STOPPED: the proposer model call failed "
                  f"({type(exc).__name__}): {first[:300]}")
            if "401" in first or "invalid_api_key" in first:
                print("  the API key was rejected: check it is current, or "
                      "create a new one")
            elif "429" in first or "quota" in first.lower():
                print("  rate limit or out of credits on the provider account")
            return 1
        saved = os.path.splitext(board)[0] + ".probes.json"
        save_probes(proposed, saved)
        print(f"  {len(proposed)} promise(s) proposed; probes saved to {saved}")

    if not proposed:
        print("STOPPED: nothing was proposed, so nothing was tested")
        return 1

    if backend is None:
        from .execution import ExecutionConfigurationError, backend_from_env
        try:
            backend = backend_from_env(log=print)
        except ExecutionConfigurationError as exc:
            print(f"STOPPED: {exc}")
            return 1

    result = verify(proposed, repo, backend, timeout=args.timeout, log=print)
    run = result.run
    render_review_html(run, board)
    print()
    if not run.findings:
        print(f"STOPPED: all {len(result.dropped)} proposed promise(s) were "
              "dropped before running (see reasons above)")
        print(f"  board: {board}")
        return 1
    print(verdict_block(run).replace("attempts executed",
                                     "promises executed"))
    if result.dropped:
        print(f"  {len(result.dropped)} proposed promise(s) dropped before "
              "running (reasons above)")
    print(f"  board: {board}")
    print(f'         open "{board}"')
    for line in gap_details(run):
        print(line)
    return exit_code_for(run)
