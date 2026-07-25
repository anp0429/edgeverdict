"""TS/JS export-surface facts, shared across layers.

Extracted from the reviewer at catch 6b: the proposer needs export names
to put in the prompt, and the vitest injector needs the same names to
verify a stripped-but-needed import before merging it into the host test
file. One extractor, two consumers, zero drift. Pure stdlib; facts from
the source text, not judgment; silence over a confident wrong answer.
"""

from __future__ import annotations

import os
import re


_TS_EXTS = (".ts", ".mts", ".cts", ".tsx", ".js", ".mjs", ".cjs", ".jsx")


def _ts_binding_names(pattern: str) -> list[str]:
    """Binding identifiers of a destructuring pattern, best effort.
    `{a, b: c, d = 1, ...rest}` binds a, c, d, rest; `[x, , y]` binds
    x, y. Nested patterns recurse. Regex-grade, not a parser — the
    surface is an aid whose claims must still be true, so unparseable
    input yields nothing rather than guesses."""
    inner = pattern.strip()
    if inner and inner[0] in "{[":
        inner = inner[1:-1] if inner[-1] in "}]" else inner[1:]
    out: list[str] = []
    depth = 0
    part = ""
    parts: list[str] = []
    for ch in inner:
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(part)
            part = ""
        else:
            part += ch
    parts.append(part)
    for p in parts:
        p = p.split("=", 1)[0].strip()
        if not p:
            continue
        if p.startswith("..."):
            p = p[3:].strip()
        if ":" in p and not p.startswith(("{", "[")):
            p = p.split(":", 1)[1].strip()
        if p.startswith(("{", "[")):
            out.extend(_ts_binding_names(p))
            continue
        m = re.match(r"[A-Za-z_$][\w$]*$", p)
        if m:
            out.append(p)
    return out


def _ts_strip_comments(text: str) -> str:
    """Drop block and line comments so `// export function fake` cannot
    mint a name. String-literal contents can survive, which is why every
    scan below also anchors `export` at line start."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def _ts_resolve(base_dir: str, spec: str) -> str:
    """Resolve a relative re-export specifier to a real file, trying the
    extension ladder and index files; ESM-style `./x.js` may mean x.ts
    on disk. Returns "" when nothing exists — a missing hop is dropped,
    never guessed."""
    if not spec.startswith("."):
        return ""
    stem = os.path.normpath(os.path.join(base_dir, spec))
    for js_ext in (".js", ".mjs", ".cjs"):
        if stem.endswith(js_ext):
            stem = stem[: -len(js_ext)]
            break
    candidates = [stem + ext for ext in _TS_EXTS]
    candidates += [os.path.join(stem, "index" + ext) for ext in _TS_EXTS]
    if os.path.splitext(stem)[1]:
        candidates.insert(0, stem)
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


def _ts_exports(path: str, visited: set, depth: int) -> tuple:
    """(value_names, type_names) exported by a TS/JS module, following
    `export * from` and `export {..} from` up to four hops with a
    visited set. Entry files like ufo's index.ts are pure re-export
    chains, so refusing to follow them would return an empty surface for
    exactly the files that matter most (ufo 9 / ohash 7 broken-proposal
    signature)."""
    if depth > 4 or path in visited:
        return [], []
    visited.add(path)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = _ts_strip_comments(fh.read())
    except OSError:
        return [], []
    values: list[str] = []
    types: list[str] = []

    def _add(seq: list[str], name: str) -> None:
        if name and name not in seq:
            seq.append(name)

    decl = re.compile(
        r"^\s*export\s+(?:declare\s+)?(default\s+)?"
        r"(async\s+)?(function\s*\*?|abstract\s+class|class|"
        r"const\s+enum|enum|interface|type|namespace|const|let|var)\s+"
        r"([A-Za-z_$][\w$]*)", re.M)
    for m in decl.finditer(text):
        is_default, kind, name = m.group(1), m.group(3), m.group(4)
        kind = re.sub(r"\s+", " ", kind.strip())
        if is_default:
            _add(values, "default")
            continue
        if kind in ("interface", "type"):
            _add(types, name)
        else:
            _add(values, name)
    for m in re.finditer(
            r"^\s*export\s+(?:const|let|var)\s*([{\[])", text, re.M):
        tail = text[m.end() - 1:]
        eq = tail.find("=")
        if eq > 0:
            for name in _ts_binding_names(tail[:eq]):
                _add(values, name)
    if re.search(r"^\s*export\s+default\b", text, re.M):
        _add(values, "default")
    braces = re.compile(
        r"^\s*export\s+(type\s+)?\{([^}]*)\}\s*"
        r"(?:from\s*['\"]([^'\"]+)['\"])?", re.M)
    for m in braces.finditer(text):
        all_types, body, _spec = m.group(1), m.group(2), m.group(3)
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            item_is_type = bool(all_types)
            if item.startswith("type "):
                item_is_type = True
                item = item[5:].strip()
            exported = item.split(" as ")[-1].strip()
            if re.match(r"[A-Za-z_$][\w$]*$", exported):
                _add(types if item_is_type else values, exported)
    star = re.compile(
        r"^\s*export\s*\*\s*(?:as\s+([A-Za-z_$][\w$]*)\s+)?"
        r"from\s*['\"]([^'\"]+)['\"]", re.M)
    for m in star.finditer(text):
        ns, spec = m.group(1), m.group(2)
        if ns:
            _add(values, ns)
            continue
        hop = _ts_resolve(os.path.dirname(path), spec)
        if hop:
            hop_values, hop_types = _ts_exports(hop, visited, depth + 1)
            for n in hop_values:
                _add(values, n)
            for n in hop_types:
                _add(types, n)
    return values, types




_ES_IMPORT_RE = re.compile(
    r"^[ \t]*import[ \t]+(?:(?P<istype>type)[ \t]+)?"
    r"(?:"
    r"\*[ \t]*as[ \t]+(?P<ns>[A-Za-z_$][\w$]*)"
    r"|(?P<default>[A-Za-z_$][\w$]*)[ \t]*(?:,[ \t]*\{(?P<both>[^}]*)\})?"
    r"|\{(?P<named>[^}]*)\}"
    r")[ \t]*from[ \t]*['\"](?P<spec>[^'\"]+)['\"]",
    re.M | re.S,
)


def parse_es_imports(code: str) -> list[dict]:
    """Binding-level view of a module's import statements (catch 6b).

    Each entry: names (local named bindings, type-only ones excluded),
    default, namespace, spec, is_type. Side-effect imports bind nothing
    and are skipped. Multi-line named blocks are handled by the regex's
    DOTALL brace match. Regex-grade like the rest of this module: an
    unparseable statement contributes nothing rather than a guess.
    """
    out: list[dict] = []
    for m in _ES_IMPORT_RE.finditer(code or ""):
        names: list[str] = []
        body = m.group("named") or m.group("both") or ""
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            if item.startswith("type "):
                continue
            local = item.split(" as ")[-1].strip()
            if re.match(r"[A-Za-z_$][\w$]*$", local):
                names.append(local)
        out.append({
            "names": names,
            "default": m.group("default"),
            "namespace": m.group("ns"),
            "spec": m.group("spec"),
            "is_type": bool(m.group("istype")),
        })
    return out


def bound_import_names(code: str) -> set:
    """Every identifier the module's imports bind, one flat set."""
    got: set = set()
    for imp in parse_es_imports(code):
        got.update(imp["names"])
        if imp["default"]:
            got.add(imp["default"])
        if imp["namespace"]:
            got.add(imp["namespace"])
    return got
