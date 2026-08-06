"""ReviewerAgent — a generic staff-engineer reviewer.

Design constraints, each earned the hard way this session:
  * GENERIC. The intent is INJECTED as data ({intent}), never baked into the
    persona. Same agent reviews any change against any intent.
  * NO SHAPE HINTS. The prompt never names composite keys, cross-schema refs,
    etc. If it listed them, a "finding" would just be the prompt read back. The
    edge-case judgment must come from the model, or we learn it can't.
  * NO FAILURE BIAS. It does NOT try to make tests fail. It asserts what the
    intent IMPLIES and lets reality answer. Aiming at red manufactures red.
  * COVERAGE-AWARE. It reads the existing tests and SKIPS what's already covered.
    Untested-but-intended behavior is where real defects hide (that is exactly
    where the composite-FK bug lived: the only test had no foreign keys).
  * DUAL-AXIS. Correctness (does output match the intent?) AND consistency (does
    the same input produce the same output across two runs?).

The agent only PROPOSES (behaviors + tests). The FindingVerifier runs them and
decides. No model certifies a gap.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re

from ..providers import chat_completion
from ..review import ReviewFinding
from ..test_setup import setup_surface
from ..ts_behavior import ts_behavior_surface
from ..ts_surface import _ts_exports

# Review axes bias WHICH cases the reviewer enumerates, injected as data so the
# base prompt stays generic (same no-shape-hints, no-failure-bias rules apply).
# An axis never names a specific payload — only categories of input — so a
# finding is still the model's judgment, not the prompt read back. "default"
# is the empty string: byte-identical to pre-axis behavior, so old caches and
# fingerprints are unchanged.
AXES: dict[str, str] = {
    "default": "",
    "security": (
        "REVIEW EMPHASIS FOR THIS RUN: adversarial and untrusted input. When you "
        "enumerate the domain (step 2), weight it toward inputs an attacker or a "
        "hostile environment could supply: values that collide with reserved or "
        "inherited names in the target language, inputs that try to cross a "
        "boundary they should not (traversal, injection into a nested "
        "structure), malformed or truncated data, oversized or deeply nested "
        "input, and mixed or unexpected encodings. Still assert only what the "
        "intent IMPLIES the correct handling is; do not assert a vulnerability "
        "or aim at a failure. Reality answers."
    ),
}


def resolve_axis(name: str) -> str:
    """Axis directive text for a name; '' for default/unknown (never raises)."""
    return AXES.get((name or "default").strip().lower(), "")

_SYSTEM = """You are a staff engineer reviewing a code change against the intent it is meant to satisfy. You are not improving the code and not inventing features. You are checking whether it does what the intent says.

INTENT:
{intent}

You are given the source file under review and the existing tests for it.

Do this:
1. First, identify WHAT KIND OF SYSTEM this change operates on, from the intent and the code. For example: does it work with a database? an authentication or authorization layer? a serialization/parsing format? a network protocol? Name the domain(s) it touches.

2. Then review it SYSTEMATICALLY against that domain's standard concerns — go through the domain's full space of structures and rules exhaustively, not just the cases the intent happens to mention. An experienced engineer does not free-associate a few cases; they enumerate the domain thoroughly. Depending on the domain, be systematic about things like:
   - if it works with structured data: every kind of element and every kind of constraint or relationship that data model supports — including compound/multi-part ones and ones that cross boundaries, not only the simple single-part case.
   - if it touches security or access: every way access is granted, denied, escalated, or leaked.
   - for any domain: the boundary cases, the empty/absent case, and the compound case (two or more of something interacting).
   Enumerate the domain's structure space and, for EACH kind, ask: does this code handle THAT kind correctly? You must reason from these categories to concrete cases yourself. (Do not expect anyone to name the specific failing case for you — that is your job.)

3. Also reason along these review dimensions across the cases you enumerate:
   - correctness: does the output match what the intent implies for realistic inputs and covers all edge cases for given intent ?
   - data integrity: does the code preserve and represent the underlying structures faithfully — without dropping, duplicating, or inventing information? Check both completeness (nothing missing) and correctness (nothing fabricated).
   - consistency: does the same input produce the same output if run twice?
   - failure modes: are malformed, empty, or missing inputs handled the way the intent implies?

4. For each behavior, check the EXISTING TESTS. If a behavior is already genuinely tested, mark it covered and move on. Pay attention to what the existing tests do NOT exercise — untested-but-implied behavior is where problems hide.

5. For each behavior that is NOT already covered, write ONE test that asserts the correct behavior the intent implies, using the SAME test style/harness as the existing tests. Do not try to make it fail; assert what SHOULD happen and let it run.

Rules:
- Pick realistic inputs that are allowed by the intent. Do not invent behavior the intent does not ask for.
- Reuse the existing test harness exactly: same imports, same setup helpers, same style. Study how the existing tests prepare their environment and reproduce that setup faithfully before exercising the code under review. The test must compile and run.
- GROUND TRUTH FOR SETUP AND RESULT SHAPE: the existing tests file — and especially any test the reviewed diff itself adds or modifies — is authoritative, because it demonstrates the CURRENT working harness by construction. Copy its setup sequence, its data seeding, and EXACTLY how it unwraps the result it asserts on (which properties it reads off the response, at what nesting). Do not invent a response shape from the source code alone; read it off an existing test's assertions. Any repo notes below are background knowledge — when they conflict with what the existing tests or the diff's own tests actually do, THE TESTS WIN.
{harness_notes}
- Assert the CORRECT expected result, not a failure.
- EXACTNESS FOR PRODUCED COLLECTIONS (mandatory). Whenever the tool returns a collection (an array or set of items) and the intent implies that collection is the complete, authoritative answer, assert it EXACTLY: assert the count AND deep-equal the full expected set — nothing missing and nothing extra. A consumer relies on this output as truth; an item that should not be there is false information the consumer will act on, a correctness failure, not a cosmetic one. Presence-only matchers (arrayContaining, toContain, toContainEqual, objectContaining, stringContaining) are FORBIDDEN when the collection is meant to be complete, because they pass even when the tool returns extra or duplicated items. Assert length (e.g. toHaveLength(N)) plus a full deep-equal; if output order is not guaranteed, sort both sides by a stable key first, but still assert the exact length — the length check is what catches over-production.

Output ONLY JSON:
{{"behaviors": [
  {{"behavior": "<one sentence>",
    "axis": "correctness" | "consistency",
    "covered_by_existing": true | false,
    "coverage_note": "<which existing test covers it, or 'none found'>",
    "test_path": "<repo-root-relative path to the test file, or null if covered>",
    "test_code": "<a complete test in the existing harness style, or null if covered>"
  }}
]}}"""


# Column zero only. A `const` inside a test body is scoped to that body, and
# listing it as available would cause the very bug this block exists to stop.
_DECL_RE = re.compile(
    r"^(?:export\s+)?(?:declare\s+)?(?:async\s+)?"
    r"(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE)


# The static scaffolding of the user message. Keep in sync with review()
# below: it exists so the cache key moves when the prompt's SHAPE moves, not
# only when its inputs do.
_USER_SHAPE = (
    "WHAT THIS PR CHANGED|SOURCE FILE|EXISTING TESTS|IMPORT SURFACE|"
    "REAL SIGNATURES|VISIBLE BEHAVIOR|IN SCOPE IN THE HOST TESTS FILE|axis"
)


def prompt_fingerprint() -> str:
    """A hash of the PROMPT TEXT, so the cache invalidates when it changes.

    proposal_key hashed every input the prompt reads but never the prompt
    itself, so editing the instructions produced byte-identical keys and the
    next run silently replayed proposals written under the old wording. A
    prompt change you cannot observe is a prompt change you cannot evaluate.
    Hashing the templates here means no one has to remember to bump a version
    constant.
    """
    parts = [_SYSTEM, _USER_SHAPE, host_scope.__doc__ or ""]
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def host_scope(tests_rel: str, tests_src: str) -> str:
    """What is ALREADY bound at module scope in the file the test lands in.

    A proposal is written as if it lived in the module it is describing, but it
    is injected into ONE specific existing tests file, and JavaScript scope is
    per file. The diff routinely shows tests from OTHER files using helpers
    those files define locally -- supabase/mcp defines `setup` as a plain local
    function in server.test.ts, exports nothing, and a proposal for
    tool-schemas.ts copied that call into tool-schemas.test.ts, where the name
    does not exist. Three verdicts were lost to one unbound identifier.

    So state the scope as data instead of hoping the model infers it.
    """
    if not tests_src.strip():
        return ""
    names: set[str] = set()
    if tests_rel.endswith((".py",)):
        try:
            tree = ast.parse(tests_src)
        except SyntaxError:
            return ""
        for node in tree.body:
            if isinstance(node, ast.Import):
                names.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.update(a.asname or a.name for a in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        names.add(tgt.id)
    else:
        from ..ts_surface import bound_import_names
        names |= bound_import_names(tests_src)
        names |= {m.group(1) for m in _DECL_RE.finditer(tests_src)}
    if not names:
        return ""
    listed = ", ".join(sorted(names))
    return (
        f"IN SCOPE IN THE HOST TESTS FILE ({tests_rel}) — your test is injected "
        f"into THIS file, so only these names are already available:\n"
        f"  {listed}\n"
        f"Any other name you use MUST be imported by your test, with a path "
        f"correct relative to {os.path.dirname(tests_rel) or '.'}. "
        f"Helpers you see in the diff or in other test files are NOT in scope "
        f"here unless that module exports them; a locally-defined helper in "
        f"another file cannot be imported at all. An unbound name makes the "
        f"test unrunnable and the finding is discarded."
    )


def _ts_import_surface(repo_root: str, target_rel: str) -> str:
    """Catch 6: the TS-lane twin of the Python surface. The gauntlet
    measured the cost of its absence — ufo produced 9 broken proposals
    and ohash 7, one shared signature, tests that never compile because
    the import path or name was invented. Same contract as the Python
    side: deterministic facts in the prompt, no judgment, and silence
    over a confident wrong answer."""
    path = os.path.join(repo_root, target_rel)
    values, types = _ts_exports(path, set(), 0)
    if not values and not types:
        return ""
    pkg_line = ""
    try:
        import json as _json
        with open(os.path.join(repo_root, "package.json"),
                  encoding="utf-8") as fh:
            pkg_name = _json.load(fh).get("name", "")
        if pkg_name:
            pkg_line = (f" The package is `{pkg_name}`; existing tests "
                        f"show whether they import the package name or a "
                        f"relative path — copy their style.")
    except (OSError, ValueError):
        pass
    type_line = (f" Type-only names (import type): {', '.join(types)}."
                 if types else "")
    listed = ", ".join(values) if values else "(no runtime exports)"
    return (
        f"IMPORT SURFACE ({target_rel}) — import the target via a "
        f"relative path from your test file to this file.{pkg_line} "
        f"Exported runtime names: {listed}.{type_line} Import the names "
        f"you need from this file with a normal import statement; the "
        f"harness merges your imports into the host tests file and drops "
        f"any name this file does not actually export. Never invent "
        f"names, and never rely on fixtures not defined inside your own "
        f"test."
    )


def import_surface(repo_root: str, target_rel: str) -> str:
    """Deterministic prompt DATA for Python targets: the module path a test
    must import and the public names that actually exist. Exists because
    the reviewer, given only source text, invented `_targets_from_diff`
    in edgeverdict.cli when the real names were public in edgeverdict.config
    — 33 proposals died of hallucinated imports in one self-review run.
    Facts from the ast, not judgment; the prompt stays repo-agnostic."""
    if target_rel.endswith((".ts", ".mts", ".cts", ".tsx",
                            ".js", ".mjs", ".cjs", ".jsx")):
        return _ts_import_surface(repo_root, target_rel)
    if not target_rel.endswith(".py"):
        return ""
    try:
        with open(os.path.join(repo_root, target_rel),
                  encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return ""
    # Name collection: every public top-level BINDING is importable, and
    # the gate proved the narrow first version wrong five ways in one run
    # (fingerprint e4a011add5ff924c): annotated constants, compound and
    # tuple assignments, lowercase publics like `router`, __init__
    # re-exports, namespace-package paths.
    is_init = os.path.basename(target_rel) == "__init__.py"
    names: list[str] = []

    def _bind(name: str) -> None:
        if name and not name.startswith("_") and name not in names:
            names.append(name)

    def _target_names(t) -> list[str]:
        if isinstance(t, ast.Name):
            return [t.id]
        if isinstance(t, (ast.Tuple, ast.List)):
            out: list[str] = []
            for e in t.elts:
                out.extend(_target_names(e))
            return out
        if isinstance(t, ast.Starred):
            return _target_names(t.value)
        return []

    def _walruses(node) -> list[str]:
        # a top-level `(name := ...)` binds a module global; one inside a
        # nested function/class/lambda does not — recurse but never enter
        # a new scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            return []
        out: list[str] = []
        if isinstance(node, ast.NamedExpr) and isinstance(node.target,
                                                          ast.Name):
            out.append(node.target.id)
        for child in ast.iter_child_nodes(node):
            out.extend(_walruses(child))
        return out

    def _walrus_scan(expr) -> None:
        for w in _walruses(expr):
            _bind(w)

    _TRY_NODES = (ast.Try,) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())

    def _collect(stmts) -> None:
        # Recursive MODULE-SCOPE walker (round six): statements inside
        # module-level if/try/for/while/with bodies bind real globals, so
        # the walk descends compound statements — but never def/class/
        # lambda, which open new scopes. The previous top-level-only walk
        # missed a binding created inside an except body (run
        # c6b038372514ff65).
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                _bind(node.name)
                # decorator, default, annotation, and base-class
                # expressions evaluate AT MODULE LEVEL when the def does:
                # a walrus inside any of them binds a module global
                for dec in node.decorator_list:
                    _walrus_scan(dec)
                if isinstance(node, ast.ClassDef):
                    for b in node.bases:
                        _walrus_scan(b)
                    for kw in node.keywords:
                        _walrus_scan(kw.value)
                else:
                    a = node.args
                    for d in [*a.defaults, *a.kw_defaults]:
                        if d is not None:
                            _walrus_scan(d)
                    for arg in [*getattr(a, "posonlyargs", []), *a.args,
                                *a.kwonlyargs]:
                        if arg.annotation is not None:
                            _walrus_scan(arg.annotation)
                    if node.returns is not None:
                        _walrus_scan(node.returns)
                continue  # bodies open a new scope: never descend
            _walrus_scan(node)
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    for nm in _target_names(t):
                        _bind(nm)
            elif isinstance(node, ast.AnnAssign):
                # ENABLED: bool = True binds; a bare `x: int` annotation
                # does not exist at runtime and is excluded
                if node.value is not None and isinstance(node.target,
                                                         ast.Name):
                    _bind(node.target.id)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                # a module-level loop variable persists after the loop
                for nm in _target_names(node.target):
                    _bind(nm)
                _collect(node.body)
                _collect(node.orelse)
            elif isinstance(node, ast.While):
                _collect(node.body)
                _collect(node.orelse)
            elif isinstance(node, ast.If):
                _collect(node.body)
                _collect(node.orelse)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        for nm in _target_names(item.optional_vars):
                            _bind(nm)
                _collect(node.body)
            elif isinstance(node, _TRY_NODES):
                _collect(node.body)
                for h in node.handlers:
                    # the alias (`except E as x`) is DELETED by Python
                    # after the handler; bindings INSIDE the handler body
                    # persist and are collected
                    _collect(h.body)
                _collect(node.orelse)
                _collect(node.finalbody)
            elif isinstance(node, ast.Match):
                # defect 26 (board scenario e6ecaf785f9bbc67): a module-
                # level match binds globals two ways, and both persist
                # after the statement — capture patterns (`case x:`,
                # `case Point(x=px):`, `case [*rest]:`, mapping `**rest`)
                # bind like assignments, and case bodies bind like any
                # compound body. Walruses in the subject and guards are
                # already collected by the unconditional scan above.
                def _pattern_names(p) -> list[str]:
                    out: list[str] = []
                    if isinstance(p, ast.MatchAs):
                        if p.name:
                            out.append(p.name)
                        if p.pattern is not None:
                            out.extend(_pattern_names(p.pattern))
                    elif isinstance(p, ast.MatchStar):
                        if p.name:
                            out.append(p.name)
                    elif isinstance(p, ast.MatchMapping):
                        for sub in p.patterns:
                            out.extend(_pattern_names(sub))
                        if p.rest:
                            out.append(p.rest)
                    elif isinstance(p, (ast.MatchSequence, ast.MatchOr)):
                        for sub in p.patterns:
                            out.extend(_pattern_names(sub))
                    elif isinstance(p, ast.MatchClass):
                        for sub in [*p.patterns, *p.kwd_patterns]:
                            out.extend(_pattern_names(sub))
                    return out

                for case in node.cases:
                    for nm in _pattern_names(case.pattern):
                        _bind(nm)
                    _collect(case.body)
            elif isinstance(node, ast.Delete):
                # in ORDER, so del-then-rebind keeps the rebind; tuple
                # forms (`del (A, B)`) remove every contained name
                for t in node.targets:
                    for nm in _target_names(t):
                        if nm in names:
                            names.remove(nm)
            elif is_init and isinstance(node, (ast.Import, ast.ImportFrom)):
                # a package __init__'s re-exports ARE its public surface;
                # regular modules' imports are dependencies, not API
                for al in node.names:
                    if al.name == "*":
                        continue
                    _bind(al.asname or al.name.split(".")[0])

    _collect(tree.body)

    # Module path, best effort for real layouts: walk up through
    # identifier-named directories (namespace packages have no
    # __init__.py, PEP 420), then drop a topmost src/lib segment, which
    # is a source ROOT, not a package. Requiring __init__.py at every
    # level truncated `company.product.feature` to `product.feature`.
    stem = os.path.basename(target_rel)[: -len(".py")]
    if stem != "__init__" and not stem.isidentifier():
        # "my-module.py" cannot be imported by any path; a surface built
        # on it would be a confident wrong answer (round six, stem twin
        # of the directory-segment rule)
        return ""
    parts = [] if stem == "__init__" else [stem]
    d = os.path.dirname(target_rel)
    truncated = False
    while d:
        seg = os.path.basename(d)
        if not seg.isidentifier():
            truncated = True
            break
        parts.append(seg)
        d = os.path.dirname(d)
    if truncated and not (parts and parts[-1] in ("src", "lib")):
        # the walk was cut by a non-importable path segment somewhere
        # BELOW a source root: any module path we emit would be a guess,
        # and a wrong path in the prompt is worse than none (round four)
        return ""
    while parts and parts[-1] in ("src", "lib"):
        parts.pop()
    if not parts:
        return ""
    module = ".".join(reversed(parts))
    listed = ", ".join(names) if names else "(no public top-level names)"
    return (
        f"IMPORT SURFACE ({target_rel}) — importable as `{module}`. "
        f"Public top-level names: {listed}. In every proposed test, import "
        f"the target ONLY via this module path and ONLY these names; never "
        f"invent private helpers, other module paths, or fixtures that are "
        f"not defined inside your own test."
    )


def _loads_lenient(text: str) -> dict:
    """Parse model JSON; if truncated, salvage the complete objects seen so far.

    Anthropic's messages API has no guaranteed-JSON mode, so a long response can
    be cut off mid-string. Rather than lose the whole review, recover every fully
    formed object we can find.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # salvage: try to parse the object closed at every '}', capturing nested ones
    objs, stack, instr, esc = [], [], False, False
    for i, ch in enumerate(text):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            try:
                o = json.loads(text[start : i + 1])
                if isinstance(o, dict) and "behavior" in o:
                    objs.append(o)
            except json.JSONDecodeError:
                pass
    return {"behaviors": objs}


def parse_review_plan(data: dict) -> list[ReviewFinding]:
    """Pure: model JSON -> ReviewFinding list. Defensive; drops malformed items."""
    findings: list[ReviewFinding] = []
    for b in data.get("behaviors", []) or []:
        behavior = str(b.get("behavior", "")).strip()
        if not behavior:
            continue
        axis = b.get("axis", "correctness")
        if axis not in ("correctness", "consistency"):
            axis = "correctness"
        covered = bool(b.get("covered_by_existing", False))
        tp, tc = b.get("test_path"), b.get("test_code")
        findings.append(
            ReviewFinding(
                behavior=behavior,
                axis=axis,
                covered_by_existing=covered,
                coverage_note=str(b.get("coverage_note", "")).strip()[:200],
                test_path=tp if (isinstance(tp, str) and tp and not covered) else None,
                test_code=tc
                if (isinstance(tc, str) and tc.strip() and not covered)
                else None,
            )
        )
    return findings


def _sig_of_class(node) -> str:
    """One-line constructible surface for a class: explicit __init__
    parameters if present, else dataclass-style annotated fields (the
    posthog FeatureFlag case — a @dataclass with no __init__, whose real
    fields are key/enabled/variant/reason/metadata and NOT `id`)."""
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and item.name == "__init__":
            a = item.args
            params = [p.arg for p in (list(a.posonlyargs) + list(a.args))
                      if p.arg != "self"]
            if a.vararg:
                params.append("*" + a.vararg.arg)
            params += [p.arg for p in a.kwonlyargs]
            if a.kwarg:
                params.append("**" + a.kwarg.arg)
            return f"{node.name}({', '.join(params)})"
    # no __init__: dataclass / annotated fields at class-body scope
    fields = [b.target.id for b in node.body
              if isinstance(b, ast.AnnAssign) and isinstance(b.target, ast.Name)
              and not b.target.id.startswith("_")]
    if fields:
        return f"{node.name}({', '.join(fields)})"
    return node.name + "()"


def _sig_of_func(node) -> str:
    a = node.args
    params = [p.arg for p in (list(a.posonlyargs) + list(a.args))
              if p.arg != "self"]
    if a.vararg:
        params.append("*" + a.vararg.arg)
    params += [p.arg for p in a.kwonlyargs]
    if a.kwarg:
        params.append("**" + a.kwarg.arg)
    return f"{node.name}({', '.join(params)})"


def _signatures_in_source(src: str) -> tuple[dict, dict]:
    """(classes, funcs) name -> one-line signature, public top-level only."""
    classes: dict = {}
    funcs: dict = {}
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return classes, funcs
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            classes[node.name] = _sig_of_class(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and not node.name.startswith("_"):
            funcs[node.name] = _sig_of_func(node)
    return classes, funcs


def _resolve_relative_import(repo_root: str, target_rel: str,
                             module: str, level: int) -> str | None:
    """Map an import in the target file to a repo-relative .py path, one
    hop. Handles `from .types import` / `from pkg.types import` and
    absolute `from pkg.mod import` within the repo. Returns None when it
    escapes the repo or the file is absent."""
    if level:  # relative: resolve against the target's package dir
        base = os.path.dirname(target_rel)
        for _ in range(level - 1):
            base = os.path.dirname(base)
        rel = os.path.join(base, *module.split(".")) if module else base
    else:      # absolute: try as a repo-rooted path (drop leading pkg if it
               # matches the target's top package, e.g. posthog.types)
        rel = os.path.join(*module.split("."))
    for cand in (rel + ".py", os.path.join(rel, "__init__.py")):
        full = os.path.join(repo_root, cand)
        if os.path.isfile(full) and os.path.abspath(full).startswith(
                os.path.abspath(repo_root)):
            return cand
    return None


def signature_surface(repo_root: str, target_rel: str) -> str:
    """Deterministic prompt DATA: the real constructor/parameter shapes of
    the classes and functions a test will call, so the proposer stops
    inventing kwargs. Motivated by posthog-python #813, where proposals
    wrote FeatureFlag(id=...) (a @dataclass whose real fields are
    key/enabled/variant/reason/metadata, imported from posthog.types) and
    get_feature_flags_and_payloads(only_evaluate_locally=...) (a real
    method with no such parameter) — both died as TypeErrors before
    executing. Generic Python-shaped, no repo names: the target file's own
    classes/functions plus a ONE-HOP resolve of the names it imports and
    actually calls. Facts from the ast, never judgment."""
    if not target_rel.endswith(".py"):
        return ""
    try:
        with open(os.path.join(repo_root, target_rel),
                  encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return ""

    classes, funcs = _signatures_in_source(src)

    # one-hop: resolve the PUBLIC names the target imports from repo
    # modules and pull their signatures. A test constructs the target's
    # API types (return/param types) even when the target file itself only
    # references them — posthog #813's FeatureFlag is imported into
    # client.py but never constructed there, yet the test must build one.
    # Over-inclusion here costs prompt weight, never correctness, since
    # signatures are DATA the proposer may ignore, not bindings.
    imported: dict = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        wanted = [a.asname or a.name for a in node.names
                  if a.name != "*"
                  and not (a.asname or a.name).startswith("_")
                  and (a.asname or a.name) not in classes
                  and (a.asname or a.name) not in funcs]
        if not wanted:
            continue
        dep = _resolve_relative_import(repo_root, target_rel,
                                       node.module or "", node.level or 0)
        if not dep:
            continue
        try:
            with open(os.path.join(repo_root, dep),
                      encoding="utf-8", errors="replace") as fh:
                dsrc = fh.read()
        except OSError:
            continue
        dclasses, dfuncs = _signatures_in_source(dsrc)
        for name in wanted:
            if name in dclasses:
                imported[name] = dclasses[name]
            elif name in dfuncs:
                imported[name] = dfuncs[name]
        if len(imported) >= 40:
            break

    local = {**classes, **funcs}
    if not local and not imported:
        return ""
    lines = []
    if local:
        lines.append("  in this file: "
                     + "; ".join(sorted(local[k] for k in local)))
    if imported:
        lines.append("  imported and used here: "
                     + "; ".join(sorted(imported[k] for k in imported)))
    body = "\n".join(lines)
    return (
        "REAL SIGNATURES (construct these with ONLY the parameters shown; "
        "these are read from the code under test, so do not invent "
        "constructor keywords or function arguments):\n" + body
    )


def _changed_ranges(change: str) -> "list[tuple[int, int]] | None":
    """Line ranges the diff touches on the NEW side, from @@ hunk headers.
    None when there is no parseable diff (whole-file fallback). Lets
    behavior_surface scope to the changed function instead of the whole
    file, so a guard-heavy neighbor doesn't crowd the real helpers out of
    the cap."""
    ranges: list = []
    for m in re.finditer(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", change or ""):
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) else 1
        if count > 0:
            ranges.append((start, start + count - 1))
    return ranges or None


def _guard_chain(fn: "ast.FunctionDef | ast.AsyncFunctionDef",
                 src: str) -> list[str]:
    """The leading early-exit statements of a function: `if cond:
    return/raise/continue` chains before the main body. These are the
    code's own visible decisions — the posthog None case lived in exactly
    such a guard (`if operator not in NONE_VALUES_ALLOWED_OPERATORS and
    override_value is None: return False`) and the proposer, unable to see
    it, invented a raise instead."""
    out: list[str] = []
    # real functions do a little setup (assignments) before their guards;
    # skip leading simple assignments so we don't stop at the first line.
    body = list(fn.body)
    i = 0
    while i < len(body) and isinstance(body[i], (ast.Assign, ast.AnnAssign,
                                                 ast.Expr)):
        i += 1
    # collect the run of early-exit ifs, tolerating interleaved assignments
    # (guard, extract, guard, ... is common). Stop at a compound body.
    while i < len(body):
        stmt = body[i]
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            i += 1
            continue
        if not isinstance(stmt, ast.If):
            break
        exits = all(isinstance(x, (ast.Return, ast.Raise, ast.Continue))
                    for x in stmt.body) and len(stmt.body) <= 2
        if not exits or stmt.orelse:
            break
        seg = ast.get_source_segment(src, stmt)
        if seg and len(seg) <= 400:
            out.append(seg)
        i += 1
        if len(out) >= 12:   # bound: a dozen guards is plenty of context
            break
    return out


def behavior_surface(repo_root: str, target_rel: str,
                     changed_line_ranges: "list[tuple[int, int]] | None" = None,
                     cap_chars: int = 9000) -> str:
    """Deterministic prompt DATA: the code's own VISIBLE decisions —
    module constants, guard clauses, and one-hop helper bodies — so the
    proposer stops inventing specs that the shown lines already contradict.

    Motivated by every hand-verified judgment false positive to date:
    posthog's None case (the guard + NONE_VALUES_ALLOWED_OPERATORS =
    ["is_not"] were in the file), posthog's array case (str_istartswith,
    one hop away, just stringifies), dateparser's resolve_date_order (a
    pure lookup table) and YDM ordering (derived from the visible chart).
    On the one pure-logic run (tenacity) the proposer invented nothing —
    given visible behavior, disagreement mostly cannot be written.

    Facts from the ast, never judgment. Advisory DATA the proposer may
    ignore; worst case is prompt weight. NEVER replaces execution — this
    shapes what gets PROPOSED; verdicts are still earned by running.
    Failure posture: any trouble -> "" (a surface must never kill a run).
    Cap drop order: helpers first, then guards, never constants."""
    if target_rel.endswith((".ts", ".mts", ".cts", ".tsx",
                            ".js", ".mjs", ".cjs", ".jsx")):
        return ts_behavior_surface(repo_root, target_rel, cap_chars=cap_chars)
    if not target_rel.endswith(".py"):
        return ""
    try:
        with open(os.path.join(repo_root, target_rel),
                  encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return ""

    def _in_ranges(node) -> bool:
        if not changed_line_ranges:
            return True
        lo = getattr(node, "lineno", None)
        hi = getattr(node, "end_lineno", None) or lo
        if lo is None or hi is None:
            return False
        return any(not (hi < a or lo > b) for a, b in changed_line_ranges)

    # 1) module constants: top-level literal assignments, verbatim.
    constants: list[str] = []
    for cnode in tree.body:
        value: "ast.expr | None"
        if isinstance(cnode, ast.Assign) and cnode.targets:
            value = cnode.value
        elif isinstance(cnode, ast.AnnAssign):
            value = cnode.value
        else:
            continue
        if value is None:
            continue
        if isinstance(value, (ast.Constant, ast.List, ast.Tuple, ast.Set,
                              ast.Dict)):
            seg = ast.get_source_segment(src, cnode)
            if seg and len(seg) <= 300:
                constants.append(seg)

    # 2) guard clauses of changed (or all, when ranges unknown) functions.
    guards: list[str] = []
    # 3) names CALLED inside those functions, for one-hop helper bodies.
    called: set = set()
    attr_calls: set = set()
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for fn in funcs:
        if fn.name.startswith("__") or not _in_ranges(fn):
            continue
        for g in _guard_chain(fn, src):
            guards.append(f"{fn.name}: {g}")
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute) and \
                        isinstance(node.func.value, ast.Name):
                    # utils.str_istartswith(...) -> record (module, attr) so
                    # we can resolve the module one hop and pull the func.
                    called.add(node.func.attr)
                    attr_calls.add((node.func.value.id, node.func.attr))

    # resolve helpers: same-file defs, then intra-repo imports (one hop).
    helpers: list[str] = []
    local_defs = {f.name: f for f in funcs}
    seen: set = set()
    for name in sorted(called):
        if name in local_defs and name not in seen:
            seen.add(name)
            fn = local_defs[name]
            seg = ast.get_source_segment(src, fn) or ""
            body_lines = seg.splitlines()
            if 0 < len(body_lines) <= 30:
                helpers.append(seg)
            elif body_lines:
                head = body_lines[0]
                first_guards = _guard_chain(fn, src)
                helpers.append("\n".join([head, *first_guards[:1]]))
    # imported helpers, one hop
    imported_from: dict = {}
    for inode in ast.walk(tree):
        if isinstance(inode, ast.ImportFrom):
            dep = _resolve_relative_import(repo_root, target_rel,
                                           inode.module or "", inode.level or 0)
            if dep:
                for a in inode.names:
                    imported_from[a.asname or a.name] = dep
        elif isinstance(inode, ast.Import):
            for a in inode.names:
                dep = _resolve_relative_import(repo_root, target_rel,
                                               a.name, 0)
                if dep:
                    imported_from[(a.asname or a.name).split(".")[0]] = dep
    # module aliases: `from pkg import utils` / `import pkg.utils as u`,
    # so module.func() attribute calls resolve one hop.
    module_alias: dict = {}
    for mnode in ast.walk(tree):
        if isinstance(mnode, ast.ImportFrom):
            for a in mnode.names:
                sub = (mnode.module or "") + ("." if mnode.module else "") + a.name
                dep = _resolve_relative_import(repo_root, target_rel, sub,
                                               mnode.level or 0)
                if dep:
                    module_alias[a.asname or a.name] = dep
        elif isinstance(mnode, ast.Import):
            for a in mnode.names:
                dep = _resolve_relative_import(repo_root, target_rel,
                                               a.name, 0)
                if dep:
                    module_alias[(a.asname or a.name).split(".")[0]] = dep

    dep_cache: dict = {}

    # resolve module.func() attribute calls (utils.str_istartswith) one hop.
    for mod, attr in sorted(attr_calls):
        if attr in seen or mod not in module_alias:
            continue
        dep = module_alias[mod]
        if dep not in dep_cache:
            try:
                with open(os.path.join(repo_root, dep),
                          encoding="utf-8", errors="replace") as fh:
                    dsrc = fh.read()
                dep_cache[dep] = (dsrc, ast.parse(dsrc))
            except (OSError, SyntaxError, ValueError):
                dep_cache[dep] = None
        if not dep_cache[dep]:
            continue
        dsrc, dtree = dep_cache[dep]
        for dfn in dtree.body:
            if isinstance(dfn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and dfn.name == attr:
                seen.add(attr)
                seg = ast.get_source_segment(dsrc, dfn) or ""
                if 0 < len(seg.splitlines()) <= 30:
                    helpers.append(f"# from {dep}\n{seg}")
                elif seg:
                    helpers.append(f"# from {dep}\n" + "\n".join(
                        [seg.splitlines()[0], *_guard_chain(dfn, dsrc)[:1]]))
                break

    for name in sorted(called):
        if name in seen or name not in imported_from:
            continue
        dep = imported_from[name]
        if dep not in dep_cache:
            try:
                with open(os.path.join(repo_root, dep),
                          encoding="utf-8", errors="replace") as fh:
                    dsrc = fh.read()
                dep_cache[dep] = (dsrc, ast.parse(dsrc))
            except (OSError, SyntaxError, ValueError):
                dep_cache[dep] = None
        if not dep_cache[dep]:
            continue
        dsrc, dtree = dep_cache[dep]
        for fn in dtree.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))                     and fn.name == name:
                seen.add(name)
                seg = ast.get_source_segment(dsrc, fn) or ""
                body_lines = seg.splitlines()
                if 0 < len(body_lines) <= 30:
                    helpers.append(f"# from {dep}\n{seg}")
                elif body_lines:
                    helpers.append(f"# from {dep}\n" + "\n".join(
                        [body_lines[0], *_guard_chain(fn, dsrc)[:1]]))
                break

    # imported helpers may ALSO shadow: resolve module-attribute calls where
    # the module itself was imported (utils.str_istartswith) — handled above
    # by attribute-name matching against each dep's top-level functions.
    for name in sorted(called):
        if name in seen:
            continue
        for dep, cached in dep_cache.items():
            if not cached:
                continue
            dsrc, dtree = cached
            for fn in dtree.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))                         and fn.name == name:
                    seen.add(name)
                    seg = ast.get_source_segment(dsrc, fn) or ""
                    if 0 < len(seg.splitlines()) <= 30:
                        helpers.append(f"# from {dep}\n{seg}")
                    break
            if name in seen:
                break

    if not constants and not guards and not helpers:
        return ""

    def _render(cs, gs, hs) -> str:
        parts = []
        if cs:
            parts.append("module constants:\n" + "\n".join(cs))
        if gs:
            parts.append("guard clauses (early exits, verbatim):\n"
                         + "\n".join(gs))
        if hs:
            parts.append("helpers called here (bodies, one hop):\n"
                         + "\n\n".join(hs))
        body = "\n\n".join(parts)
        return (
            "VISIBLE BEHAVIOR (the code already decides these cases — do "
            "not propose a behavior that a line shown here contradicts; if "
            "you believe a shown decision is itself wrong, mark that "
            "proposal as a design question, not a gap):\n" + body
        )

    # cap: drop helpers first, then guards, never constants.
    out = _render(constants, guards, helpers)
    while len(out) > cap_chars and helpers:
        helpers.pop()
        out = _render(constants, guards, helpers)
    while len(out) > cap_chars and guards:
        guards.pop()
        out = _render(constants, guards, helpers)
    return out if len(out) <= cap_chars else out[:cap_chars]


class ReviewerAgent:
    def __init__(
        self,
        repo_root: str,
        target_path: str,
        existing_tests_path: str,
        model: str = "gpt-5.5",
        client=None,
        max_chars: int = 12000,
        harness_notes: str = "",
        axis: str = "default",
        log=print,
    ):
        self.repo_root = repo_root
        self.target_path = target_path
        self.existing_tests_path = existing_tests_path
        self.model = model
        # optional provider pin from repo config; ambient env still wins
        # upstream (api.py resolves precedence before passing it here).
        self.base_url = ""
        self._client = client
        self.max_chars = max_chars
        # print-shaped narration sink; the caller picks where lines go (the
        # CLI passes print, the MCP server a per-call buffer). See api.py.
        self.log = log
        # Repo-specific test-writing rules (e.g. RepoProfile.harness_notes).
        # Injected into the prompt as data; the prompt stays repo-agnostic.
        self.harness_notes = harness_notes.strip()
        self.axis = (axis or "default").strip().lower()
        self._axis_directive = resolve_axis(self.axis)
        from ..providers import uses_anthropic
        self._is_openai = not uses_anthropic(model)

    def _read(self, rel: str) -> str:
        try:
            with open(os.path.join(self.repo_root, rel), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def _client_lazy(self):
        if self._client is None:
            from ..providers import client_for

            self._client = client_for(self.model, self.base_url)
        return self._client

    def review(self, intent: str, change: str = "") -> list[ReviewFinding]:
        source = self._read(self.target_path)[: self.max_chars]
        tests = self._read(self.existing_tests_path)[: self.max_chars]
        surface = import_surface(self.repo_root, self.target_path)
        sigs = signature_surface(self.repo_root, self.target_path)
        behavior = behavior_surface(self.repo_root, self.target_path,
                                    _changed_ranges(change))
        # test-setup surface: the fixtures + boundary doubles the proposer can
        # reuse. Fed with blast's test_importers so the conftest chain and mock
        # idioms of EVERY test file that reaches the target are in scope, not
        # just the one named by --tests.
        extra_tests: list[str] = []
        try:
            from ..blast import compute_blast_detail
            extra_tests = compute_blast_detail(
                self.repo_root, self.target_path).test_importers
        except Exception:  # noqa: BLE001 — advisory must never kill a run
            extra_tests = []
        setup = setup_surface(self.repo_root, self.target_path,
                              self.existing_tests_path, extra_tests)
        scope = host_scope(self.existing_tests_path, tests)
        change_block = (
            f"WHAT THIS PR CHANGED (review THIS against the intent, not the whole file):\n"
            f"```\n{change}\n```\n\n"
            if change.strip()
            else ""
        )
        user = (
            f"{change_block}"
            f"SOURCE FILE ({self.target_path}):\n```\n{source}\n```\n\n"
            f"EXISTING TESTS ({self.existing_tests_path}):\n```\n{tests}\n```"
            + (f"\n\n{surface}" if surface else "")
            + (f"\n\n{sigs}" if sigs else "")
            + (f"\n\n{behavior}" if behavior else "")
            + (f"\n\n{setup}" if setup else "")
            + (f"\n\n{scope}" if scope else "")
            + (f"\n\n{self._axis_directive}" if self._axis_directive else "")
        )
        notes = (
            "- " + self.harness_notes if self.harness_notes else
            "(no repo-specific harness notes provided)"
        )
        try:
            client = self._client_lazy()
            if self._is_openai:
                resp = chat_completion(
                    client,
                    model=self.model,
                    response_format={"type": "json_object"},
                    # a JSON plan never needs the model's full output ceiling;
                    # an uncapped request lets a chatty provider run for
                    # minutes and makes metered routers reserve the whole
                    # ceiling against the account balance.
                    max_tokens=6000,
                    messages=[
                        {"role": "system", "content": _SYSTEM.format(intent=intent, harness_notes=notes)},
                        {"role": "user", "content": user},
                    ],
                )
                content = resp.choices[0].message.content or ""
                if not content.strip():
                    # An empty completion is a failure wearing success's
                    # clothes: a reasoning model can spend the whole token
                    # budget thinking and return nothing, which downstream
                    # reads as "0 behaviors" with no hint. Say it loudly.
                    reason = getattr(resp.choices[0], "finish_reason", "?")
                    self.log(f"  [warn] reviewer: empty completion "
                             f"(finish_reason={reason}) — the model likely "
                             f"spent its whole token budget reasoning")
                # lenient, not strict: local models behind the same client
                # sometimes fence their JSON despite response_format.
                data = _loads_lenient(content or "{}")
            else:
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=8000,
                    system=_SYSTEM.format(intent=intent, harness_notes=notes)
                    + "\n\nRespond with ONLY the JSON object, no prose before or after.",
                    messages=[{"role": "user", "content": user}],
                )
                text = "".join(
                    b.text for b in resp.content if getattr(b, "type", None) == "text"
                )
                data = _loads_lenient(text)
        except Exception as e:  # never crash the loop
            self.log(f"  [warn] reviewer: {e}")
            return []
        return parse_review_plan(data)