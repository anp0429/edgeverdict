"""Test-setup facts: the fixtures and boundary-doubles a proposed test can
reuse, surfaced so the proposer USES the author's existing mocks instead of
reaching for a real database / cache / queue.

The proposer is an LLM: it already knows how to request a pytest fixture by
parameter name, how @patch("pkg.mod.name") works, and how to use a fake
class. What it cannot do is SEE the setup — conftest.py fixtures are auto-
injected by pytest and never appear in the test file the proposer is shown,
and the mocks for a boundary often live in a SIBLING test file, not the one
named by --tests. So a proposer told to test db-touching code, shown only
the source and one test file, will invent a fresh mock (or worse, write a
test that reaches a real connection and dies on install-less infra).

This surface closes that gap the same way import_surface and behavior_surface
close theirs: show the model the facts it can't infer, then let it write the
test. Two facts:

  1. FIXTURES IN SCOPE — every fixture the conftest.py chain provides for the
     target's test files (request by putting the name in the test signature).
  2. BOUNDARY DOUBLES IN USE — the patch targets and fixture/fake names the
     EXISTING tests already use, so the proposer copies the repo's idiom for
     faking db/cache/queue instead of inventing one.

Which test files? Not just the one named by --tests. The blast walk already
computes test_importers — every test file that reaches the target. Their
conftest chain and their mock idioms are ALL in scope for an injected test,
so the surface unions across them.

Pytest-first: conftest.py auto-loading by directory is a pytest mechanic
(vitest declares setup files in config — a separate port, later). Non-py
targets return "". Regex/ast-grade, facts only, never judgment; any trouble
-> "" (a surface must never kill a run).
"""

from __future__ import annotations

import ast
import os

_MOCK_HINTS = (
    "mock", "Mock", "patch", "monkeypatch", "fake", "Fake", "stub",
    "Stub", "spy", "Spy", "MagicMock", "AsyncMock",
)


def _conftest_chain(repo_root: str, test_rel: str) -> list[str]:
    """The conftest.py files pytest would load for a test at test_rel: one
    per directory from the repo root down to the test's directory. Returned
    repo-relative, root-first (pytest's load order)."""
    out: list[str] = []
    parts = os.path.dirname(test_rel).split("/") if os.path.dirname(test_rel) else []
    acc = ""
    # repo-root conftest first
    for d in [""] + parts:
        acc = os.path.join(acc, d) if d else acc
        cand = os.path.join(acc, "conftest.py") if acc else "conftest.py"
        full = os.path.join(repo_root, cand)
        if os.path.isfile(full) and cand not in out:
            out.append(cand)
    return out


def _fixtures_in(src: str) -> list[tuple[str, str]]:
    """(name, one-line summary) for every @pytest.fixture in a source string.
    The summary is the fixture's first docstring line if present, else its
    signature params (which reveal what it composes, e.g. monkeypatch)."""
    out: list[tuple[str, str]] = []
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_fixture = False
        autouse = False
        for dec in node.decorator_list:
            d = dec.func if isinstance(dec, ast.Call) else dec
            name = getattr(d, "attr", None) or getattr(d, "id", None)
            if name == "fixture":
                is_fixture = True
                if isinstance(dec, ast.Call):
                    for kw in dec.keywords:
                        if (kw.arg == "autouse"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value):
                            autouse = True
        if not is_fixture:
            continue
        doc = ast.get_docstring(node)
        if doc:
            summary = doc.strip().splitlines()[0][:80]
        else:
            params = [a.arg for a in node.args.args]
            summary = ("composes " + ", ".join(params)) if params else ""
        if autouse:
            summary = ("[autouse] " + summary).strip()
        out.append((node.name, summary))
    return out


def _patch_targets(src: str) -> list[str]:
    """The dotted targets the existing tests patch for a boundary, e.g.
    "posthog.client.get" from @patch("posthog.client.get") or
    mock.patch.object(...). Deduped, order-preserving. These show the
    proposer the CORRECT lookup path to patch (patch-where-used, not
    where-defined — the classic mock gotcha the model can't guess)."""
    out: list[str] = []
    seen: set[str] = set()
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return out
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # match patch(...) and mock.patch(...) and patch.object(...)
        fname = getattr(f, "attr", None) or getattr(f, "id", None)
        if fname not in ("patch", "object"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            tgt = node.args[0].value
            if tgt and tgt not in seen and "." in tgt:
                seen.add(tgt)
                out.append(tgt)
    return out[:20]


def _double_names(src: str) -> list[str]:
    """Names bound in the tests that look like doubles (fake/stub/mock/spy),
    so the proposer can reuse an existing fake class or helper instead of
    building one. Top-level defs/classes/assigns whose name carries a mock
    hint."""
    out: list[str] = []
    seen: set[str] = set()
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return out
    for node in tree.body:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    name = tgt.id
                    break
        if not name or name in seen:
            continue
        if any(h in name for h in _MOCK_HINTS):
            seen.add(name)
            out.append(name)
    return out[:20]


def setup_surface(repo_root: str, target_rel: str, tests_rel: str,
                       extra_test_files: "list[str] | None" = None,
                       cap_chars: int = 4000) -> str:
    """Prompt DATA: the fixtures and boundary-doubles a proposed test can
    reuse. Unions the conftest chain of tests_rel with the mock idioms of
    tests_rel AND every extra_test_files entry (pass blast's test_importers
    here). Pytest-only; "" on any trouble or non-py target.

    NOTE: this surfaces AVAILABILITY, not technique. It never tells the
    model HOW to mock (it knows); it tells it WHAT exists and WHICH path the
    repo patches. Advisory — never changes an execution-earned verdict.
    """
    if not tests_rel.endswith(".py"):
        return ""

    test_files: list[str] = [tests_rel]
    for f in (extra_test_files or []):
        if f.endswith(".py") and f not in test_files:
            test_files.append(f)

    # fixtures: from the conftest chain of every test file (deduped by path)
    conftests: list[str] = []
    for tf in test_files:
        for c in _conftest_chain(repo_root, tf):
            if c not in conftests:
                conftests.append(c)

    fixtures: list[tuple[str, str]] = []
    fx_seen: set[str] = set()
    for c in conftests:
        try:
            with open(os.path.join(repo_root, c), encoding="utf-8",
                      errors="replace") as fh:
                for name, summ in _fixtures_in(fh.read()):
                    if name not in fx_seen:
                        fx_seen.add(name)
                        fixtures.append((name, summ))
        except OSError:
            continue

    # boundary doubles: patch targets + fake names across the test files
    patch_targets: list[str] = []
    doubles: list[str] = []
    pt_seen: set[str] = set()
    db_seen: set[str] = set()
    for tf in test_files:
        try:
            with open(os.path.join(repo_root, tf), encoding="utf-8",
                      errors="replace") as fh:
                src = fh.read()
        except OSError:
            continue
        for t in _patch_targets(src):
            if t not in pt_seen:
                pt_seen.add(t)
                patch_targets.append(t)
        for d in _double_names(src):
            if d not in db_seen:
                db_seen.add(d)
                doubles.append(d)

    if not (fixtures or patch_targets or doubles):
        return ""

    lines = [
        "TEST SETUP AVAILABLE (reuse the repo's own doubles — do NOT reach "
        "a real database / cache / queue, and do NOT invent a new mock when "
        "one of these fits; write the edge-case test the way this repo's "
        "existing tests are written):"
    ]
    if fixtures:
        fx = "; ".join(
            f"{n}" + (f" ({s})" if s else "") for n, s in fixtures[:25])
        lines.append(
            "- fixtures in scope (request by putting the name in your test's "
            f"parameter list): {fx}")
    if patch_targets:
        lines.append(
            "- patch targets the existing tests use (patch the SAME path — "
            "it is where the name is looked up, not where it is defined): "
            + ", ".join(patch_targets[:20]))
    if doubles:
        lines.append(
            "- fakes/stubs already defined for reuse: "
            + ", ".join(doubles[:20]))

    out = "\n".join(lines)
    return out if len(out) <= cap_chars else out[:cap_chars]
