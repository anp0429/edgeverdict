"""Promises: execute what a repository says about itself.

sig EDGEVERDICT_PROMISES_V1
sig EDGEVERDICT_PROMISES_V2 (reference-driven collection, must/attempt)
sig EDGEVERDICT_PROMISES_V3 (verdict message capped at 500 chars)

A README, a hooks guide, a CONTRIBUTING file all make behavioral claims:
"safe to run twice", "never overwrites", "every draft gets the next
number". Those claims are code-adjacent, rarely tested, and nobody reviews
them against the scripts they describe. This module turns each claim into
an executed check, with the same split as the rest of edgeverdict: a model
proposes (the promise and a probe script that tries to break it), a
deterministic runner decides. No model in pass/fail.

The probe contract is language-agnostic because the probe is a shell
script: it drives the repo the way a user would (run its CLI, its install
script, its hooks) inside a scratch copy of the repo.

Verdict rules (each one learned from a false green somewhere else):

  * A promise is only tested if its quoted text is actually found in the
    file it cites. A model paraphrasing a promise the repo never made is
    dropped before anything runs, and the count of drops is reported.
  * A probe must have a failure path (a call to `broken`). A probe that can
    only succeed is not evidence and never runs.
  * exit 1 alone is NOT a broken promise. `set -e`, a typo, a missing file
    all exit 1. A promise is broken only when the probe exits 1 AND its last
    marker line is EDGEVERDICT_PROMISE_BROKEN. Held likewise needs exit 0
    AND the HELD marker. Everything else is broken_test (the probe failed,
    which is evidence about the probe, not the repo).
  * A missing tool in the sandbox (jq, make, node) is reported by name as a
    probe that did not run, never as either verdict.
  * Commands under test run through `must` (expected to succeed) or
    `attempt` (failure may be the observed behavior). If a `must` command
    errors anywhere, in a capture or a background job included, the probe
    is not evidence: a crash is not a contradicted value. (sig V2)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable

from .review import ReviewFinding, ReviewRun

SIG = "EDGEVERDICT_PROMISES_V2"

MARK_BROKEN = "EDGEVERDICT_PROMISE_BROKEN:"
MARK_HELD = "EDGEVERDICT_PROMISE_HELD:"
MARK_MISSING = "EDGEVERDICT_MISSING_TOOL:"
MARK_ERRORED = "EDGEVERDICT_UNDER_TEST_ERRORED:"
MARKERS_FILE = ".edgeverdict_markers"

# Directories that are never part of what a repo promises and are expensive
# to copy. .git is deliberately NOT here: promises about branches, commits
# and PRs need the history.
_SKIP_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", ".turbo",
    ".tox", "target", ".cache",
}
_DOC_EXTS = (".md", ".markdown", ".rst", ".txt")
_CODE_EXTS = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts",
              ".rb", ".pl", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".java",
              ".kt", ".php", ".cs", ".swift", ".lua", ".ex", ".toml", ".json",
              ".yaml", ".yml")
_MAX_REPO_BYTES = 200 * 1024 * 1024


# ---------------------------------------------------------------- collection
#
# Generic by construction: nothing here knows any repo. Files are chosen by
# what the docs and scripts REFER to (one hop through scripts), plus the
# repo's own sample data and tests, labeled as such, so the model builds
# scenarios from real formats instead of inventing them.

_DATA_EXTS = (".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".csv", ".tsv",
              ".txt", ".xml", ".toml", ".ini", ".env.example", ".log", ".sql",
              ".html", ".diff", ".patch")
_FIXTURE_DIRS = {"examples", "example", "fixtures", "fixture", "testdata",
                 "test-data", "test_data", "samples", "sample", "__fixtures__",
                 "__snapshots__", "golden", "data"}
_TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs", "e2e"}
_TEST_NAME = re.compile(
    r"(^test_.*\.py$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$|^test.*\.(sh|bats)$|\.bats$)",
    re.IGNORECASE)
_REF_TOKEN = re.compile(r"[A-Za-z0-9_$.{}/-]*[A-Za-z0-9_-]\.[A-Za-z0-9]{1,8}\b")


@dataclass
class RepoText:
    docs: dict[str, str] = field(default_factory=dict)      # rel -> text
    code: dict[str, str] = field(default_factory=dict)      # scripts/source
    fixtures: dict[str, str] = field(default_factory=dict)  # repo's own data
    tests: dict[str, str] = field(default_factory=dict)     # repo's own tests
    tree: list[str] = field(default_factory=list)


def _git_files(repo: str) -> list[str] | None:
    """Tracked plus untracked-but-not-ignored files, when the repo is a git
    checkout: the repo's own .gitignore is the most generic definition of
    what is not part of the project (venvs, build output, caches)."""
    if not os.path.exists(os.path.join(repo, ".git")):
        return None
    try:
        r = subprocess.run(
            ["git", "-C", repo, "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return sorted(f for f in r.stdout.split("\0") if f)


def _skip_path(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in _SKIP_DIRS or p == ".git" or p == "site-packages"
               for p in parts[:-1])


def _walk(repo: str):
    listed = _git_files(repo)
    if listed is not None:
        for rel in listed:
            full = os.path.join(repo, rel)
            if not _skip_path(rel) and os.path.isfile(full) \
                    and not os.path.islink(full):
                yield rel, full
        return
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(
            d for d in dirs if d not in _SKIP_DIRS and d != ".git"
            and not os.path.exists(os.path.join(root, d, "pyvenv.cfg")))
        for name in sorted(files):
            full = os.path.join(root, name)
            yield os.path.relpath(full, repo).replace(os.sep, "/"), full


_LOCKFILES = re.compile(
    r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lockb?|"
    r"[^/]*\.lock|npm-shrinkwrap\.json|go\.sum)$")
_CONFIG_EXTS = (".json", ".yaml", ".yml", ".toml")


def _read(path: str, cap: int) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read(cap)
    except (OSError, UnicodeDecodeError):
        return ""


def _head(path: str, cap: int) -> str:
    """Whole file if it fits, else whole lines up to cap plus a marker, so
    a data sample is never cut mid-record."""
    text = _read(path, cap + 1)
    if len(text) <= cap:
        return text
    cut = text[:cap].rsplit("\n", 1)[0]
    try:
        total = os.path.getsize(path)
    except OSError:
        total = 0
    return cut + f"\n[... truncated, {total} bytes total]\n"


def _doc_rank(rel: str) -> tuple[int, int, str]:
    base = rel.rsplit("/", 1)[-1].lower()
    depth = rel.count("/")
    if base.startswith("readme"):
        tier = 0
    elif base.startswith(("contributing", "install", "usage")):
        tier = 1
    elif rel.lower().startswith("docs/"):
        tier = 2
    elif base in ("license", "license.md", "changelog.md", "code_of_conduct.md"):
        tier = 9
    else:
        tier = 3
    return (tier, depth, rel)


def _kind(rel: str, full: str) -> str:
    if _LOCKFILES.search(rel):
        return "other"
    parts = rel.lower().split("/")
    base = parts[-1]
    dirs = set(parts[:-1])
    if base.endswith(_DOC_EXTS[:3]) and not dirs & _FIXTURE_DIRS:
        return "doc"
    is_data = base.endswith(_DATA_EXTS) or base.endswith(_DOC_EXTS[3:])
    if is_data and (dirs & _FIXTURE_DIRS or dirs & _TEST_DIRS or any(
            w in base for w in ("sample", "example", "fixture", "golden"))):
        return "fixture"
    if _TEST_NAME.search(base) or (dirs & _TEST_DIRS and not is_data):
        return "test"
    if base.endswith(_CODE_EXTS) or ("." not in base and os.access(full, os.X_OK)):
        return "code"
    if is_data:
        return "data"
    if base.endswith(_DOC_EXTS):
        return "doc"
    return "other"


def references(text: str, tree: list[str]) -> set[str]:
    """Repo files a text refers to: full relative paths, path suffixes
    ($DIR/hooks/x.sh -> hooks/x.sh) and, when unambiguous enough, bare
    basenames. Pure and repo-agnostic."""
    by_base: dict[str, list[str]] = {}
    for rel in tree:
        by_base.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    found: set[str] = set()
    for tok in set(_REF_TOKEN.findall(text)):
        tok = tok.strip("./")
        tok = re.sub(r"^(\$\{?\w+\}?/)+", "", tok)
        if not tok:
            continue
        hits = [r for r in tree if r == tok or r.endswith("/" + tok)]
        if not hits and "/" not in tok:
            hits = by_base.get(tok, [])
        if 0 < len(hits) <= 3:
            found.update(hits)
    return found


def collect(repo: str, *, doc_budget: int = 40000, code_budget: int = 40000,
            fixture_budget: int = 16000, test_budget: int = 12000,
            per_file: int = 12000) -> RepoText:
    """Deterministic, capped read of what the repo says (docs), what it
    does (scripts and source the docs refer to, plus one hop of what those
    scripts call), and how it is exercised (its own sample data and tests).
    Referenced files always outrank unreferenced ones."""
    out = RepoText()
    kinds: dict[str, list[tuple[str, str]]] = {}
    for rel, full in _walk(repo):
        out.tree.append(rel)
        kinds.setdefault(_kind(rel, full), []).append((rel, full))

    used = 0
    for rel, full in sorted(kinds.get("doc", []), key=lambda d: _doc_rank(d[0])):
        if _doc_rank(rel)[0] == 9:
            continue
        text = _read(full, per_file)
        if not text.strip() or used + len(text) > doc_budget:
            continue
        out.docs[rel] = text
        used += len(text)
    doc_refs = references("\n".join(out.docs.values()), out.tree)

    def rank(refs: set[str]):
        # referenced first, then real source before config, shallow first
        return lambda item: (0 if item[0] in refs else 1,
                             1 if item[0].endswith(_CONFIG_EXTS) else 0,
                             item[0].count("/"), item[0])

    used = 0
    code = kinds.get("code", [])
    for rel, full in sorted(code, key=rank(doc_refs)):
        text = _read(full, per_file)
        if not text.strip() or used + len(text) > code_budget:
            continue
        out.code[rel] = text
        used += len(text)
    # one hop: what the chosen scripts themselves call or read
    hop_refs = references("\n".join(out.code.values()), out.tree)
    for rel, full in sorted(code, key=rank(hop_refs)):
        if rel in out.code or rel not in hop_refs:
            continue
        text = _read(full, per_file)
        if text.strip():
            out.code[rel] = text  # a called script is worth the overrun
    all_refs = doc_refs | hop_refs

    data = kinds.get("fixture", []) + [
        d for d in kinds.get("data", []) if d[0] in all_refs]
    used = 0
    for rel, full in sorted(data, key=rank(all_refs)):
        text = _head(full, 6000)
        if not text.strip() or used + len(text) > fixture_budget:
            continue
        out.fixtures[rel] = text
        used += len(text)

    used = 0
    for rel, full in sorted(kinds.get("test", []), key=rank(all_refs)):
        text = _head(full, 6000)
        if not text.strip() or used + len(text) > test_budget:
            continue
        out.tests[rel] = text
        used += len(text)
    out.tree = out.tree[:400]
    return out


# ------------------------------------------------------------------ promises

@dataclass
class Promise:
    claim: str            # the promise in plain words
    quote: str            # verbatim text from the source doc
    source: str           # rel path of the doc that makes the promise
    scenario: str         # the situation the probe sets up
    probe: str            # bash body; runs after the harness prelude
    grounded: bool = False
    drop_reason: str = ""


def parse_promises(data: dict) -> list[Promise]:
    """Pure: model JSON -> Promise list. Drops malformed items."""
    out: list[Promise] = []
    for item in (data or {}).get("promises", []) or []:
        if not isinstance(item, dict):
            continue
        p = Promise(
            claim=str(item.get("claim", "")).strip(),
            quote=str(item.get("quote", "")).strip(),
            source=str(item.get("source", "")).strip().lstrip("./"),
            scenario=str(item.get("scenario", "")).strip(),
            probe=str(item.get("probe", "")).rstrip() + "\n",
        )
        if p.claim and p.quote and p.source and p.probe.strip():
            out.append(p)
    return out


_MD_NOISE = re.compile(r"[`*_>#|]")


def _norm(text: str) -> str:
    text = _MD_NOISE.sub(" ", text)
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def ground(promises: list[Promise], repo: str) -> None:
    """Deterministic source check: the quote must appear in the cited file
    (markdown noise and whitespace normalized). Sets grounded/drop_reason.
    Also refuses probes with no failure path."""
    cache: dict[str, str] = {}
    for p in promises:
        root = os.path.abspath(repo)
        path = os.path.abspath(os.path.join(root, p.source))
        if not path.startswith(root + os.sep) or \
                not os.path.isfile(path):
            p.drop_reason = f"cited source {p.source!r} is not a file in the repo"
            continue
        if path not in cache:
            cache[path] = _norm(_read(path, 2_000_000))
        needle = _norm(p.quote)
        if len(needle) < 12:
            p.drop_reason = "quote too short to pin to the source"
            continue
        if needle not in cache[path]:
            p.drop_reason = (f"quote not found in {p.source}: the repo does "
                             f"not make this promise in those words")
            continue
        if not re.search(r"(^|[\s;&|({])broken(\s|$|\")", p.probe):
            p.drop_reason = "probe has no failure path (never calls broken)"
            continue
        if not re.search(r"(^|[\s;&|({`])(must|attempt)\s", p.probe):
            p.drop_reason = ("probe never runs the repo through must/attempt, "
                             "so a crash could not be told apart from a "
                             "broken promise")
            continue
        p.grounded = True


# ------------------------------------------------------------------- running

PRELUDE = r"""#!/usr/bin/env bash
# edgeverdict promise probe (sig EDGEVERDICT_PROMISES_V1). Harness prelude:
# the generated body below runs in a scratch copy of the repo, offline.
set -u
WORK="$(pwd -P)"
REPO="$WORK/repo"
export HOME="$WORK/home"
mkdir -p "$HOME"
export GIT_CONFIG_NOSYSTEM=1
export GIT_TERMINAL_PROMPT=0
if command -v git >/dev/null 2>&1; then
  git config --global user.name "edgeverdict probe"
  git config --global user.email "probe@edgeverdict.invalid"
  git config --global init.defaultBranch main
  git config --global --add safe.directory '*'
fi
_ev_mark() { printf '%s %s\n' "$1" "$2" | tee -a "$WORK/.edgeverdict_markers"; }
broken() { _ev_mark "EDGEVERDICT_PROMISE_BROKEN:" "$*"; exit 1; }
held() { _ev_mark "EDGEVERDICT_PROMISE_HELD:" "$*"; exit 0; }
need() {
  for t in "$@"; do
    command -v "$t" >/dev/null 2>&1 || { _ev_mark "EDGEVERDICT_MISSING_TOOL:" "$t"; exit 3; }
  done
}
# must: run a command under test that is expected to SUCCEED. If it fails,
# the promise was never reached: record it (visible even from $(...) and
# background jobs) and stop. stdout passes through for capture.
must() {
  local _e; _e="$(mktemp "$WORK/.ev_err.XXXXXX")"
  "$@" 2>"$_e"; local _rc=$?
  if [ "$_rc" -ne 0 ]; then
    _ev_mark "EDGEVERDICT_UNDER_TEST_ERRORED:" "rc=$_rc cmd=$* stderr=$(tail -c 400 "$_e" | tr '\n' ' ')" >&2
    exit 5
  fi
  cat "$_e" >&2; rm -f "$_e"; return 0
}
# attempt: run a command under test whose FAILURE may itself be the
# observed behavior. Never stops; sets RC, OUT, ERR for the probe to judge.
attempt() {
  local _e; _e="$(mktemp "$WORK/.ev_err.XXXXXX")"
  OUT="$("$@" 2>"$_e")"; RC=$?
  ERR="$(cat "$_e")"; rm -f "$_e"; return 0
}
cd "$REPO"
# ---- probe body ----
"""


def _copy_repo(repo: str, dest: str) -> None:
    def ignore(_dir, names):
        return [n for n in names if n in _SKIP_DIRS]
    shutil.copytree(repo, dest, symlinks=True, ignore=ignore)


def repo_size(repo: str) -> int:
    total = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def classify(returncode: int | None, stdout: str, stderr: str,
             timed_out: bool = False, markers: str = "") -> tuple[str, str]:
    """(status, observed). Pure; the whole verdict lives here.

    `markers` is the side file every helper appends to. It sees markers
    printed inside $(...) captures and background jobs, which stdout never
    does; stdout is the fallback when the file is absent."""
    if timed_out:
        return "timed_out", "probe did not finish in time (inconclusive)"
    lines = (markers or stdout).splitlines()
    errored = [ln[len(MARK_ERRORED):].strip() for ln in lines
               if ln.startswith(MARK_ERRORED)]
    if errored:
        # a command under test crashed somewhere, even in a background job
        # or a capture. Whatever the probe concluded after that is built on
        # a missing value, so it is not evidence about the promise.
        more = f" (+{len(errored) - 1} more)" if len(errored) > 1 else ""
        return "broken_test", (
            f"a command under test errored before the promise could be "
            f"checked{more}: {errored[0][:500]}. Not evidence the promise "
            f"broke; a crash on a documented path may still be worth a look.")
    last_kind, last_msg = "", ""
    for line in lines:
        for kind, mark in (("broken", MARK_BROKEN), ("held", MARK_HELD),
                           ("missing", MARK_MISSING)):
            if line.startswith(mark):
                last_kind, last_msg = kind, line[len(mark):].strip()
    if len(last_msg) > 500:
        # a probe that dumps a whole file into its message buries the
        # verdict; the board keeps the probe, the line keeps the gist
        last_msg = last_msg[:500] + f" ...[{len(last_msg) - 500} more chars]"
    if last_kind == "broken" and returncode == 1:
        return "confirmed_gap", last_msg or "promise broken"
    if last_kind == "held" and returncode == 0:
        return "handled", last_msg or "promise held"
    if last_kind == "missing" and returncode == 3:
        return "broken_test", (f"did not run: the sandbox has no {last_msg!r}. "
                               "Not evidence either way.")
    tail = (stderr.strip() or stdout.strip())[-600:]
    why = (f"probe exited {returncode} without a verdict marker"
           if not last_kind else
           f"probe printed a {last_kind} marker but exited {returncode}")
    return "broken_test", f"{why}; not evidence about the repo.\n{tail}".rstrip()


def run_probe(promise: Promise, repo: str, backend, *, timeout: int = 120,
              scratch_parent: str | None = None) -> tuple[str, str]:
    """Fresh copy of the repo per probe, so one probe's side effects can
    never decide another's verdict."""
    work = tempfile.mkdtemp(prefix="edgeverdict_promise_", dir=scratch_parent)
    try:
        _copy_repo(repo, os.path.join(work, "repo"))
        with open(os.path.join(work, "probe.sh"), "w", encoding="utf-8") as fh:
            fh.write(PRELUDE + promise.probe)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "LANG": "C.UTF-8", "TERM": "dumb"}
        try:
            r = backend.run(["bash", "probe.sh"], cwd=work, env=env,
                            timeout=timeout)
        except subprocess.TimeoutExpired:
            return classify(None, "", "", timed_out=True)
        markers = _read(os.path.join(work, MARKERS_FILE), 200_000)
        return classify(r.returncode, r.stdout or "", r.stderr or "",
                        markers=markers)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def to_finding(p: Promise, status: str, observed: str) -> ReviewFinding:
    return ReviewFinding(
        behavior=f"{p.claim}  [scenario: {p.scenario}]" if p.scenario else p.claim,
        axis="correctness",
        covered_by_existing=False,
        coverage_note=f'{p.source} says: "{p.quote}"',
        test_path=p.source,
        test_code=p.probe,
        source_file=p.source,
        status=status,  # type: ignore[arg-type]
        observed=observed,
    )


@dataclass
class PromisesResult:
    run: ReviewRun
    dropped: list[Promise] = field(default_factory=list)


def verify(promises: list[Promise], repo: str, backend, *, timeout: int = 120,
           log: Callable[..., None] = print) -> PromisesResult:
    ground(promises, repo)
    run = ReviewRun(intent="what the repo's own docs promise", target=repo)
    result = PromisesResult(run=run)
    for p in promises:
        if not p.grounded:
            result.dropped.append(p)
            log(f"  dropped: {p.claim[:70]} ({p.drop_reason})")
            continue
        status, observed = run_probe(p, repo, backend, timeout=timeout)
        log(f"  {status:<14} {p.claim[:70]}")
        run.findings.append(to_finding(p, status, observed))
    return result


def load_probes_file(path: str) -> list[Promise]:
    with open(path, encoding="utf-8") as fh:
        return parse_promises(json.load(fh))
