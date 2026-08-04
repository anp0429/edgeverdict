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
    is_py = target_rel.endswith(_PY_EXT)
    candidates = _module_candidates(target_rel) if is_py else set()

    direct: set[str] = set()
    test_importers: set[str] = set()
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
                (test_importers if _is_test_path(rel) else direct).add(rel)
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
            + (f" ({n_tests} test file(s), counted separately)" if n_tests else "")
            + (f" — e.g. {examples}" if examples else " — nothing in-repo imports it")
            + (" [scan truncated at cap — counts are a lower bound]" if truncated else ""))
    return tier, note


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
