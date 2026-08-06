"""Static blast radius: how far does the changed file reach?

Deterministic, no model, no tokens — this is a walk of the repo's own import
edges, the "execute locally with the code graph instead of wasting LLM
tokens" decision made concrete. Blast radius is an IMPACT axis, orthogonal
to the confidence tier: confidence says "how much to trust that a failed
test is a real bug"; blast says "if it IS real, how much of the repo feels
it". Together they form the triage matrix (high confidence + wide blast =
worth filing; low confidence + narrow blast = probably noise). Advisory —
NEVER changes a status; the gate does not lie, it annotates.

Scope of v1, on purpose: MODULE-level reach (who imports the changed file,
directly and one transitive hop), not symbol-level dataflow. Symbol-level
("who calls THIS function and what do they do with the return") is the code
graph's job later; module reach is cheap, cacheable, and already separates
"leaf script nobody imports" from "posthog/feature_flags.py imported across
the SDK". Test files are counted separately from production importers —
tests importing a module is coverage, not blast.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass

# Directories that are never part of the repo's own import graph.
_SKIP_DIRS = {".git", "node_modules", "dist", "__pycache__", ".venv", "venv",
              "build", ".tox", ".mypy_cache", ".ruff_cache"}
# Bounded walk: a repo bigger than this gets a truncated (still honest,
# lower-bound) count rather than an unbounded scan.
_MAX_FILES = 4000
_MAX_FILE_BYTES = 400_000

_PY_EXT = (".py",)
_JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")

# import x from '../feature_flags'  |  require("./feature_flags")
_JS_IMPORT_RE = re.compile(
    r"""(?:from\s+|require\(\s*|import\(\s*|import\s+)['"]([^'"]+)['"]""")


def _is_test_path(rel: str) -> bool:
    low = rel.lower()
    base = os.path.basename(low)
    return ("test" in base or "/tests/" in low.replace(os.sep, "/")
            or low.replace(os.sep, "/").startswith("tests/"))


def _module_candidates(target_rel: str) -> set[str]:
    """Dotted-name suffixes that an `import`/`from` of this file can use.

    posthog/feature_flags.py -> {"posthog.feature_flags", "feature_flags"}
    pkg/__init__.py          -> {"pkg"}
    """
    rel = target_rel.replace(os.sep, "/")
    for ext in _PY_EXT:
        if rel.endswith(ext):
            rel = rel[: -len(ext)]
            break
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    parts = [p for p in rel.split("/") if p]
    out: set[str] = set()
    for i in range(len(parts)):
        out.add(".".join(parts[i:]))
    return out


def _py_imports(source: str) -> set[str]:
    """Dotted module names this python source imports (best effort)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
                for a in node.names:
                    # "from posthog import feature_flags" imports the module
                    # as an attribute of the package — record both.
                    names.add(f"{node.module}.{a.name}")
    return names


def _imports_target_py(imported: set[str], candidates: set[str]) -> bool:
    for name in imported:
        for cand in candidates:
            if name == cand or name.endswith("." + cand) \
                    or cand.endswith("." + name):
                return True
    return False


def _imports_target_js(source: str, target_rel: str) -> bool:
    """A JS/TS file imports the target when an import path resolves to the
    target's path suffix (extensionless): '../utils/thing' hits src/utils/
    thing.ts. Suffix match keeps it resolver-free and deterministic."""
    rel = target_rel.replace(os.sep, "/")
    for ext in _JS_EXT:
        if rel.endswith(ext):
            rel = rel[: -len(ext)]
            break
    stem = rel.split("/")[-1]
    for m in _JS_IMPORT_RE.finditer(source):
        path = m.group(1)
        if path.startswith("."):
            clean = path.lstrip("./")
            if rel.endswith(clean) or clean.endswith(stem):
                return True
        elif path.endswith("/" + stem) or path == stem:
            return True
    return False


def compute_blast(repo_root: str, target_rel: str) -> tuple[str, str]:
    """(tier, note) for the target file's module-level reach.

    tier: "wide" | "moderate" | "narrow"
    note: one line with the counts and up to three example importers, so a
    reader can check the claim instead of trusting it.
    """
    detail = compute_blast_detail(repo_root, target_rel)
    return detail.tier, detail.note


@dataclass
class BlastDetail:
    """The full blast computation, importer LISTS exposed (not just counts)
    so a human-inspection view can draw the dependency graph. compute_blast
    returns only (tier, note); this returns the sets behind them. All lists
    are module-level import edges the walk actually found — facts, not
    inference."""
    tier: str
    note: str
    target: str
    direct: list[str]
    transitive: list[str]
    test_importers: list[str]
    truncated: bool


def _enclosing_defs_py(src: str, line_ranges: list[tuple[int, int]]) -> set[str]:
    """The names of Python functions whose bodies overlap any of the given
    (start, end) NEW-side line ranges. Resolved from the file's ast, so a
    change deep in a method body attributes to that method even when the diff
    hunk header only shows the enclosing class. This is the common bugfix
    shape (edit inside a method), which pure hunk-header parsing misses."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return set()
    funcs: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append((node.lineno, node.end_lineno or node.lineno,
                          node.name))
    out: set[str] = set()
    for (lo, hi) in line_ranges:
        # the most-nested function containing any line in [lo, hi]
        best: tuple[int, str] | None = None
        for (fl, fe, name) in funcs:
            if fl <= hi and fe >= lo:  # overlap
                if best is None or fl > best[0]:
                    best = (fl, name)
        if best:
            out.add(best[1])
    return out


def _diff_files_and_ranges(change: str) -> dict[str, list[tuple[int, int]]]:
    """Parse a unified diff into {new_path: [(new_start, new_end), ...]} from
    the @@ hunk headers. NEW-side line numbers, so they index the post-change
    file. Handles multi-file diffs (git diff over several files)."""
    files: dict[str, list[tuple[int, int]]] = {}
    cur: str | None = None
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    for line in change.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            cur = None if path == "/dev/null" else path
            continue
        m = hunk_re.match(line)
        if m and cur is not None:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) else 1
            files.setdefault(cur, []).append((start, start + max(count, 1) - 1))
    return files


def changed_symbols_from_diff(change: str,
                              repo_root: str | None = None,
                              target_rel: str | None = None) -> set[str]:
    """The function/method/const names whose bodies the diff adds or edits.
    Used to make blast SYMBOL-aware: an importer inherits blast only if it
    references one of these, not merely the changed file.

    Two resolution paths, unioned:
      1. In-hunk declarations: a `+def`/`+function`/`+const NAME = () =>` on an
         added line (the diff edits the signature/declaration itself).
      2. Enclosing-function resolution: for each changed NEW-side line range,
         the function in the CURRENT target file that contains it (via ast).
         This is what catches a change deep in a method body, where the hunk
         header shows only the class — the common bugfix shape. Needs
         repo_root + target_rel to read the file; without them, only path 1
         runs (degraded, but never wrong).

    Empty set -> caller falls back to file-level blast."""
    if not change:
        return set()
    syms: set[str] = set()

    # path 1: names DECLARED on added lines (signature/decl edits)
    decl = re.compile(
        r"^\+\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"
        r"|^\+\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"
        r"|^\+\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
        r"(?:async\s*)?\([^)]*\)\s*(?::[^=]+)?=>")
    # path 1b: enclosing def/function VISIBLE in the hunk (context or added
    # line) — a repo-free catch for when the diff shows the signature above an
    # edited body. Reset at each hunk boundary so a def from a prior hunk does
    # not bleed across.
    enclosing_ctx = re.compile(
        r"^[+ ]\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"
        r"|^[+ ]\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")
    last_def: str | None = None
    for line in change.splitlines():
        if line.startswith("@@"):
            last_def = None
            continue
        mc = enclosing_ctx.match(line)
        if mc:
            last_def = mc.group(1) or mc.group(2)
        d = decl.match(line)
        if d:
            name = d.group(1) or d.group(2) or d.group(3)
            if name:
                syms.add(name)
        elif (line.startswith("+") and not line.startswith("+++")
              and last_def):
            syms.add(last_def)

    # path 2: enclosing function of each changed line range, resolved from the
    # target file (Python only — needs an ast). Only for the target file.
    if repo_root and target_rel and target_rel.endswith(_PY_EXT):
        ranges_by_file = _diff_files_and_ranges(change)
        # match the target whether the diff path is repo-relative or bare
        tgt_norm = target_rel.replace(os.sep, "/")
        ranges = ranges_by_file.get(tgt_norm)
        if ranges is None:
            for p, r in ranges_by_file.items():
                if p.endswith(tgt_norm) or tgt_norm.endswith(p):
                    ranges = r
                    break
        if ranges:
            try:
                with open(os.path.join(repo_root, target_rel),
                          encoding="utf-8", errors="ignore") as fh:
                    src = fh.read()
                syms |= _enclosing_defs_py(src, ranges)
            except OSError:
                pass
    return syms


def _references_symbol(src: str, symbols: set[str]) -> bool:
    """True when the source names one of the changed symbols as a called or
    referenced identifier — not merely imports the file. Word-boundary match
    so `load_feature_flags` does not match `load_feature_flags_v2`."""
    for sym in symbols:
        if sym and re.search(r"(?<![\w.])" + re.escape(sym) + r"\b", src):
            return True
    return False


def compute_blast_detail(repo_root: str, target_rel: str,
                         changed_symbols: "set[str] | None" = None
                         ) -> "BlastDetail":
    """Same walk as compute_blast, but returns the importer SETS so the
    whiteboard can render the graph. One scan, no extra cost — compute_blast
    now delegates here and collapses the result to (tier, note)."""
    is_py = target_rel.endswith(_PY_EXT)
    candidates = _module_candidates(target_rel) if is_py else set()
    symbols = changed_symbols or set()

    direct: set[str] = set()
    test_importers: set[str] = set()
    reaching: set[str] = set()  # direct importers that reference a changed symbol
    scanned = 0
    truncated = False
    file_imports: dict[str, set[str]] = {}  # rel -> imported dotted names (py)

    tgt_norm = target_rel.replace(os.sep, "/")
    for dirpath, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            ext_ok = fn.endswith(_PY_EXT) if is_py else fn.endswith(_JS_EXT)
            if not ext_ok:
                continue
            scanned += 1
            if scanned > _MAX_FILES:
                truncated = True
                break
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, repo_root).replace(os.sep, "/")
            if rel == tgt_norm:
                continue
            try:
                if os.path.getsize(full) > _MAX_FILE_BYTES:
                    continue
                src = open(full, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if is_py:
                imported = _py_imports(src)
                file_imports[rel] = imported
                hit = _imports_target_py(imported, candidates)
            else:
                hit = _imports_target_js(src, target_rel)
            if hit:
                if _is_test_path(rel):
                    test_importers.add(rel)
                else:
                    direct.add(rel)
                    if symbols and _references_symbol(src, symbols):
                        reaching.add(rel)
        if truncated:
            break

    # one transitive hop, python only (JS resolution is suffix-based and a
    # second hop would compound guesses — better to under-claim).
    transitive: set[str] = set()
    if is_py and direct:
        importer_candidates: set[str] = set()
        for rel in direct:
            importer_candidates |= _module_candidates(rel)
        for rel, imported in file_imports.items():
            if rel in direct or _is_test_path(rel):
                continue
            if _imports_target_py(imported, importer_candidates):
                transitive.add(rel)

    n_direct, n_trans, n_tests = len(direct), len(transitive), len(test_importers)

    if symbols:
        # tier from the SEMANTICALLY-REACHING count, not the file-import count:
        # importing the file != exercising the changed symbol.
        n_reach = len(reaching)
        import_only = n_direct - n_reach
        if n_reach >= 5:
            tier = "wide"
        elif n_reach >= 1:
            tier = "moderate"
        else:
            tier = "narrow"  # nothing references the changed symbol → local
        shown = ", ".join(sorted(reaching)[:3])
        sym_list = ", ".join(sorted(s for s in symbols if s)[:3])
        note = (
            f"{n_reach} importer(s) reference the changed symbol"
            + (f" ({sym_list})" if sym_list else "")
            + (f" — e.g. {shown}" if shown else "")
            + (f"; {import_only} more import the file but not the changed "
               f"symbol" if import_only > 0 else "")
            + (f"; {n_trans} one-hop" if n_trans else "")
            + (f" ({n_tests} test file(s), counted separately)"
               if n_tests else "")
            + (" [scan truncated at cap — counts are a lower bound]"
               if truncated else "")
        )
    else:
        reach = n_direct + n_trans
        if n_direct >= 5 or reach >= 10:
            tier = "wide"
        elif n_direct >= 1:
            tier = "moderate"
        else:
            tier = "narrow"
        examples = ", ".join(sorted(direct)[:3])
        note = (f"{n_direct} direct importer(s)"
                + (f" + {n_trans} one-hop" if n_trans else "")
                + (f" ({n_tests} test file(s), counted separately)"
                   if n_tests else "")
                + (f" — e.g. {examples}" if examples
                   else " — nothing in-repo imports it")
                + (" [scan truncated at cap — counts are a lower bound]"
                   if truncated else ""))
    return BlastDetail(
        tier=tier, note=note, target=tgt_norm,
        direct=sorted(direct), transitive=sorted(transitive),
        test_importers=sorted(test_importers), truncated=truncated,
    )


def triage(confidence: str, blast: str) -> str:
    """The matrix, as one word a human can act on. Advisory language only —
    'file-ready' still means a human files it, never the tool."""
    if confidence == "high" and blast == "wide":
        return "file-ready"
    if confidence == "high":
        return "verify-then-file"
    if blast == "wide":
        return "verify-hard"
    return "low-priority"
