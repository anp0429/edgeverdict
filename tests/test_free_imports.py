# EDGEVERDICT_FREE_IMPORTS_TESTS_V1
"""Free-identifier import resolution.

An LLM proposal routinely uses names it never imports (`z`, `injectableTool`),
because it writes the test as if it were already inside the module. If the host
file does not bind them the injected test dies with ReferenceError before it can
prove anything — a lost verdict, not a finding.

The resolver binds a name ONLY when the repo vouches for where it comes from:
  1. the target file exports it
  2. a bare specifier used elsewhere in the repo, IF the host package declares
     that dependency
  3. a relative specifier used elsewhere, re-based to the host and verified
     against the module's real export surface
Everything else stays unbound and fails honestly.
"""
import json
import os

from edgeverdict.verifiers.harness import VitestHarness as V


def _w(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


def _pkg(tmp_path, deps=None):
    root = str(tmp_path)
    _w(root, "pnpm-workspace.yaml", "packages:\n  - packages/*\n")
    _w(root, "packages/app/package.json",
       json.dumps({"name": "@acme/app",
                   "devDependencies": {"vitest": "^2", **(deps or {})}}))
    return root


def test_free_identifiers_ignores_globals_locals_and_keys():
    code = ("test('x', async () => { const p = z.object({ q: z.string() }); "
            "const t = injectableTool({}); expect(t).toBeDefined(); });")
    host = "import { describe, expect, test } from 'vitest';\n"
    free = V._free_identifiers(code, host)
    assert "z" in free and "injectableTool" in free
    # vitest globals, locally declared names and object keys are not free
    for name in ("test", "expect", "p", "t", "q"):
        assert name not in free, name


def test_free_identifier_bound_by_host_is_not_reported():
    code = "test('x', () => { expect(z).toBeDefined(); });"
    host = "import { z } from 'zod/v4';\nimport { expect, test } from 'vitest';\n"
    assert "z" not in V._free_identifiers(code, host)


def test_resolves_name_exported_by_the_target(tmp_path):
    root = _pkg(tmp_path)
    _w(root, "packages/app/src/util.ts", "export function injectableTool() {}\n")
    host = _w(root, "packages/app/src/util.test.ts",
              "import { describe, expect, test } from 'vitest';\n")
    target = os.path.join(root, "packages/app/src/util.ts")
    code = "test('x', () => { expect(injectableTool()).toBeUndefined(); });"
    merged = V.resolve_free_imports(open(host).read(), code, host, target)
    assert "injectableTool" in merged
    assert './util.js"' in merged or "./util.js'" in merged


def test_reuses_bare_specifier_when_package_declares_the_dep(tmp_path):
    root = _pkg(tmp_path, deps={"zod": "^4"})
    _w(root, "packages/app/src/other.test.ts",
       "import { z } from 'zod/v4';\ntest('a', () => {});\n")
    host = _w(root, "packages/app/src/util.test.ts",
              "import { describe, expect, test } from 'vitest';\n")
    code = "test('x', () => { expect(z.string()).toBeDefined(); });"
    merged = V.resolve_free_imports(open(host).read(), code, host, None)
    assert "zod/v4" in merged and "{ z }" in merged


def test_bare_specifier_rejected_when_dep_not_declared(tmp_path):
    root = _pkg(tmp_path)  # no zod dependency
    _w(root, "packages/app/src/other.test.ts",
       "import { z } from 'zod/v4';\ntest('a', () => {});\n")
    host = _w(root, "packages/app/src/util.test.ts",
              "import { describe, expect, test } from 'vitest';\n")
    code = "test('x', () => { expect(z.string()).toBeDefined(); });"
    merged = V.resolve_free_imports(open(host).read(), code, host, None)
    assert "zod/v4" not in merged  # unresolvable here -> fail honestly


def test_relative_specifier_is_never_rebased(tmp_path):
    """Generic names collide across suites; a verified export is not the same
    thing. supabase/mcp has a local `setup` in server.test.ts AND an exported
    `setup` in test/e2e/utils.ts with a different signature; binding by name
    ran a proposal against the wrong server and minted three false gaps.
    Unbound fails honestly as broken_test, which is the correct outcome."""
    root = _pkg(tmp_path)
    _w(root, "packages/app/test/mocks.ts",
       "export async function setup() { return { client: 1 }; }\n")
    _w(root, "packages/app/test/e2e/projects.e2e.ts",
       "import { setup } from '../mocks.js';\ntest('a', () => {});\n")
    host = _w(root, "packages/app/src/tools/schemas.test.ts",
              "import { describe, expect, test } from 'vitest';\n")
    code = "test('x', async () => { await setup({ readOnly: true }); });"
    merged = V.resolve_free_imports(open(host).read(), code, host, None)
    assert "mocks.js" not in merged
    assert "setup" not in merged.split("test(")[0]


def test_noop_when_nothing_is_free(tmp_path):
    root = _pkg(tmp_path)
    host = _w(root, "packages/app/src/util.test.ts",
              "import { describe, expect, test } from 'vitest';\n")
    code = "test('x', () => { expect(1).toBe(1); });"
    before = open(host).read()
    assert V.resolve_free_imports(before, code, host, None) == before


def test_noop_without_a_host_path():
    before = "import { test } from 'vitest';\n"
    code = "test('x', () => { expect(z).toBeDefined(); });"
    assert V.resolve_free_imports(before, code, None, None) == before
