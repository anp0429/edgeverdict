"""TS/JS behavior-surface facts: the vitest-lane twin of the Python
behavior_surface (reviewer_agent.behavior_surface). Same job, same
contract, different parser.

Python pulls the target file's VISIBLE decisions from the `ast`:
module constants, guard clauses (early-exit if-chains), and one-hop
helper bodies. Fed to the proposer, they stop it inventing specs the
shown lines already contradict — the whole false-positive-killer, and
the reason the gemini converter came back with zero judgment FPs.

TS has no stdlib ast, and the rest of this codebase parses TS by
REGEX (ts_surface.py), on purpose: no node dependency, deterministic,
and it fails to nothing rather than guessing. This module keeps that
contract. It is regex-grade, not a compiler: anything it cannot parse
cleanly it drops, because a surface whose claims might be false is
worse than a shorter one. Worst case is an empty string; a surface
must never kill a run.

The output string is byte-for-byte the same shape the Python side
emits ("VISIBLE BEHAVIOR (...)" + the three labeled sections) so the
proposer sees one consistent contract regardless of target language.
"""

from __future__ import annotations

import os
import re

from .ts_surface import (
    _TS_EXTS,
    _ts_resolve,
    _ts_strip_comments,
    parse_es_imports,
)

# ── constants ────────────────────────────────────────────────────────────
# Top-level `const NAME = <value>` (optionally exported). We keep the
# CONVENTION-BEARING ones: SCREAMING_SNAKE or a literal/array/object
# initializer — the kind of "this is the allowed set" / "this is the
# lookup table" decision that the posthog None case turned on. We do NOT
# keep `const x = someCall()` (runtime value, not a visible decision) or
# arrow-function consts (those are helpers, handled separately).
_CONST_RE = re.compile(
    r"^export\s+const\s+(?P<name>[A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=\s*(?P<val>.+?);?\s*$"
    r"|^const\s+(?P<name2>[A-Z][A-Z0-9_]*)\s*(?::[^=]+)?=\s*(?P<val2>.+?);?\s*$",
    re.MULTILINE,
)
# a second pass for non-SCREAMING consts whose value is a literal object/
# array/primitive (still a visible decision: `const NONE_OPS = ["is_not"]`
# written lowercase). Excludes arrow funcs (=>) and calls.
_LIT_CONST_RE = re.compile(
    r"^(?:export\s+)?const\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
    r"(?P<val>(?:\[[^\]]*\]|\{[^{}]*\}|['\"`][^'\"`]*['\"`]|-?\d[\d_.]*|true|false|null))\s*;?\s*$",
    re.MULTILINE,
)


def _constants(src: str) -> list[str]:
    """Module-level constant declarations that encode a decision. Verbatim,
    deduped, order-preserving. Value length is bounded so a giant inline
    table cannot blow the cap by itself (the cap logic drops helpers and
    guards first, never constants, so a runaway constant must be bounded
    here)."""
    seen: set[str] = set()
    out: list[str] = []

    def _take(name: str, line: str) -> None:
        if not name or name in seen:
            return
        line = line.strip().rstrip(";")
        if len(line) > 300:
            line = line[:297] + "..."
        seen.add(name)
        out.append(line)

    for m in _CONST_RE.finditer(src):
        name = m.group("name") or m.group("name2")
        val = (m.group("val") or m.group("val2") or "").strip()
        if not name or "=>" in val:          # arrow func is a helper, skip
            continue
        _take(name, m.group(0))
    for m in _LIT_CONST_RE.finditer(src):
        _take(m.group("name"), m.group(0))
    return out[:20]


# ── guards ───────────────────────────────────────────────────────────────
# Early-exit if statements: `if (cond) return ...;` / `if (cond) throw ...;`
# and their braced forms, taken from the LEADING run of a function body.
# The Python side skips leading assignments then collects the early-exit
# if-chain; we do the brace-depth-aware equivalent. These are the code's
# own "this case is handled thus" decisions.
_EARLY_EXIT_RE = re.compile(
    r"\bif\s*\((?P<cond>(?:[^()]|\([^()]*\))*)\)\s*"
    r"(?:\{(?P<block>[^{}]*)\}|(?P<simple>[^\n;{}]*[;\n]))",
    re.DOTALL,
)
_EXIT_KEYWORD = re.compile(r"\b(return|throw|continue|break)\b")


def _guard_clauses(src: str) -> list[str]:
    """Early-exit guards across the file, verbatim. We scan for `if (...)`
    whose body is ONLY an exit (return/throw/continue/break) — the guard
    shape — and keep it if it is short. This is looser than the Python
    per-function leading-chain (regex has no reliable function-body
    boundaries), but the intent is the same: surface the visible
    case-handling decisions. Bounded count and length; never guesses a
    body it cannot see closed."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _EARLY_EXIT_RE.finditer(src):
        body = m.group("block")
        simple = m.group("simple")
        payload = body if body is not None else (simple or "")
        if not _EXIT_KEYWORD.search(payload):
            continue
        # a guard's exit body is small; a big block is ordinary control flow
        if body is not None and len(body) > 160:
            continue
        seg = m.group(0).strip().rstrip()
        seg = re.sub(r"\s+", " ", seg)
        if len(seg) > 240 or seg in seen:
            continue
        seen.add(seg)
        out.append(seg)
        if len(out) >= 12:
            break
    return out


# ── one-hop helpers ──────────────────────────────────────────────────────
# Functions imported from a RELATIVE module and actually called in this
# file: pull their bodies (one hop). The posthog array case turned on
# str_istartswith, one relative hop away. Named imports only; we resolve
# the relative path with the shared _ts_resolve ladder and extract the
# named function body by brace matching.
_CALL_RE = re.compile(r"\b(?P<name>[A-Za-z_$][\w$]*)\s*\(")


def _called_names(src: str) -> set[str]:
    return {m.group("name") for m in _CALL_RE.finditer(src)}


def _extract_fn_body(src: str, name: str) -> str:
    """The source of a named function/arrow-const `name`, by brace match.
    Returns "" if not found or not brace-closable — never a partial guess."""
    # function declaration form
    decl = re.search(
        rf"\b(?:export\s+)?(?:async\s+)?function\s+{re.escape(name)}\s*"
        rf"\([^)]*\)[^{{]*\{{",
        src,
    )
    # arrow / function-expression const form
    if not decl:
        decl = re.search(
            rf"\b(?:export\s+)?const\s+{re.escape(name)}\s*(?::[^=]+)?=\s*"
            rf"(?:async\s+)?(?:function\s*)?\([^)]*\)[^{{]*(?:=>\s*)?\{{",
            src,
        )
    if not decl:
        return ""
    open_idx = src.index("{", decl.start())
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                seg = src[decl.start(): i + 1]
                return seg if len(seg) <= 900 else ""
    return ""


def _one_hop_helpers(repo_root: str, target_rel: str, src: str) -> list[str]:
    """Bodies of relatively-imported functions that this file calls. One
    hop, named imports only, relative specifiers only (a bare-package
    import is not our code to explain). Bounded; any hop that will not
    resolve or brace-close is dropped."""
    called = _called_names(src)
    base_dir = os.path.dirname(os.path.join(repo_root, target_rel))
    out: list[str] = []
    seen: set[str] = set()
    for imp in parse_es_imports(src):
        spec = imp.get("spec") or ""
        if not spec.startswith("."):
            continue
        dep_path = _ts_resolve(base_dir, spec)
        if not dep_path or not os.path.isfile(dep_path):
            continue
        try:
            with open(dep_path, encoding="utf-8", errors="replace") as fh:
                dep_src = _ts_strip_comments(fh.read())
        except OSError:
            continue
        for name in imp.get("names", []):
            if name not in called or name in seen:
                continue
            body = _extract_fn_body(dep_src, name)
            if body:
                seen.add(name)
                rel = os.path.relpath(dep_path, repo_root)
                out.append(f"// from {rel}\n{body.strip()}")
            if len(out) >= 6:
                return out
    return out


# ── render (identical shape to the Python side) ──────────────────────────
def _render(constants: list[str], guards: list[str],
            helpers: list[str]) -> str:
    parts = []
    if constants:
        parts.append("module constants:\n" + "\n".join(constants))
    if guards:
        parts.append("guard clauses (early exits, verbatim):\n"
                     + "\n".join(guards))
    if helpers:
        parts.append("helpers called here (bodies, one hop):\n"
                     + "\n\n".join(helpers))
    if not parts:
        return ""
    body = "\n\n".join(parts)
    return (
        "VISIBLE BEHAVIOR (the code already decides these cases — do "
        "not propose a behavior that a line shown here contradicts; if "
        "you believe a shown decision is itself wrong, mark that "
        "proposal as a design question, not a gap):\n" + body
    )


def ts_behavior_surface(repo_root: str, target_rel: str,
                        cap_chars: int = 9000) -> str:
    """The TS/JS VISIBLE BEHAVIOR surface. Same contract as the Python
    behavior_surface: deterministic facts from the source, never
    judgment, advisory-only (never changes an execution-earned verdict),
    and "" on any trouble. Cap drop order matches Python: helpers first,
    then guards, never constants."""
    if not target_rel.endswith(_TS_EXTS):
        return ""
    try:
        with open(os.path.join(repo_root, target_rel),
                  encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return ""
    src = _ts_strip_comments(raw)

    constants = _constants(src)
    guards = _guard_clauses(src)
    helpers = _one_hop_helpers(repo_root, target_rel, src)
    if not (constants or guards or helpers):
        return ""

    out = _render(constants, guards, helpers)
    while len(out) > cap_chars and helpers:
        helpers.pop()
        out = _render(constants, guards, helpers)
    while len(out) > cap_chars and guards:
        guards.pop()
        out = _render(constants, guards, helpers)
    return out if len(out) <= cap_chars else out[:cap_chars]
