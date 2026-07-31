"""TS import matching (resolver step 3.5), born from gauntlet run 1:
unjs/ufo tests src/utils.ts in test/utilities.test.ts — basename
conventions cannot survive human naming. Path matching catches tests
that import ".../<base>" outright (defu style); name matching catches
imports of the target's exported identifiers through a barrel (ufo
style). A tie is ambiguity, and ambiguity asks instead of guessing."""

import os

from edgeverdict.verifiers.harness import VitestHarness as V


def _mk(tmp_path, rel, content=""):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return str(p)


def test_name_matching_through_a_barrel_ufo_shape(tmp_path):
    _mk(tmp_path, "src/utils.ts",
        "export function hasProtocol(x: string) {}\n"
        "export function parsePath(x: string) {}\n"
        "export const isRelative = (x: string) => true\n")
    _mk(tmp_path, "src/index.ts", "export * from './utils'\n")
    _mk(tmp_path, "test/utilities.test.ts",
        "import { hasProtocol, parsePath } from '../src'\n"
        "test('x', () => {})\n")
    _mk(tmp_path, "test/other.test.ts",
        "import { somethingElse } from '../src'\n")
    got = V.default_tests_for(str(tmp_path), "src/utils.ts")
    assert got == os.path.join("test", "utilities.test.ts")


def test_path_matching_defu_shape(tmp_path):
    _mk(tmp_path, "src/defu.ts", "export function defu() {}\n")
    _mk(tmp_path, "test/merge.test.ts",
        "import { defu } from '../src/defu'\n")
    got = V.default_tests_for(str(tmp_path), "src/defu.ts")
    assert got == os.path.join("test", "merge.test.ts")


def test_single_name_overlap_is_too_weak_to_guess(tmp_path):
    _mk(tmp_path, "src/utils.ts", "export function alpha() {}\n")
    _mk(tmp_path, "test/a.test.ts", "import { alpha } from '../src'\n")
    _mk(tmp_path, "test/b.test.ts", "import { alpha } from '../src'\n")
    assert V.default_tests_for(str(tmp_path), "src/utils.ts") \
        .endswith("utils.test.ts")  # falls through to the best-guess ask


def test_colocated_still_wins_before_import_matching(tmp_path):
    _mk(tmp_path, "src/thing.ts", "export const a = 1\n")
    _mk(tmp_path, "src/thing.test.ts", "import { a } from './thing'\n")
    _mk(tmp_path, "test/thing.test.ts", "import { a } from '../src/thing'\n")
    assert V.default_tests_for(str(tmp_path), "src/thing.ts") \
        == os.path.join("src", "thing.test.ts")


def test_spec_suffix_colocated_and_dir(tmp_path):
    # gauntlet catch 3: pathe's entire suite is *.spec.ts
    _mk(tmp_path, "src/a.ts", "export const a = 1\n")
    _mk(tmp_path, "src/a.spec.ts", "import { a } from './a'\n")
    assert V.default_tests_for(str(tmp_path), "src/a.ts") \
        == os.path.join("src", "a.spec.ts")
    _mk(tmp_path, "src/b.ts", "export const b = 1\n")
    _mk(tmp_path, "test/b.spec.ts", "import { b } from '../src/b'\n")
    assert V.default_tests_for(str(tmp_path), "src/b.ts") \
        == os.path.join("test", "b.spec.ts")


def test_smoke_uses_a_real_probe_file_not_filter_tricks():
    # gauntlet catch 5: vitest 4 exits 1 when a -t filter skips everything,
    # --passWithNoTests notwithstanding; the probe is now a real test file
    from edgeverdict.verifiers.vitest_verifier import (_PROBE_REL,
                                                      RepoProfile)
    for prof in (RepoProfile.pnpm_vitest("x"), RepoProfile.npm_vitest("x")):
        assert prof.smoke_cmd[-1] == _PROBE_REL
        assert "-t" not in prof.smoke_cmd
        assert prof.smoke_probe is not None
        assert prof.smoke_probe[0] == _PROBE_REL
        assert "___edgeverdict_env_probe___" in prof.smoke_probe[1]


def test_probe_placement_follows_the_tests_file():
    # catch 5b: a root probe is disowned by monorepo project includes;
    # the probe lives beside the tests file, wearing the suite's flavor
    from edgeverdict.verifiers.vitest_verifier import smoke_probe_for
    rel, content = smoke_probe_for("packages/x/tests/y.test.ts")
    assert rel == "packages/x/tests/__edgeverdict_env_probe__.test.ts"
    rel2, _ = smoke_probe_for("test/index.spec.ts")
    assert rel2 == "test/__edgeverdict_env_probe__.spec.ts"
    rel3, _ = smoke_probe_for("demo.test.js")
    assert rel3 == "__edgeverdict_env_probe__.test.js"
    assert "___edgeverdict_env_probe___" in content


def test_foreign_basename_defers_to_same_dir_test(tmp_path):
    # EDGEVERDICT_316: three util.test.ts exist across a package, none in the
    # target's own directory; a same-basename test in a COUSIN dir
    # (transports/) would inject the target's ./ imports against the wrong
    # file. When the target's own directory holds exactly one test, prefer it.
    _mk(tmp_path, "pkg/src/tools/util.ts", "export function injectableTool(){}\n")
    _mk(tmp_path, "pkg/src/tools/tool-schemas.test.ts",
        "import { createToolSchemas } from './tool-schemas.js'\n")
    _mk(tmp_path, "pkg/src/tools/tool-schemas.ts", "export const createToolSchemas=1\n")
    _mk(tmp_path, "pkg/src/transports/util.ts", "export const x=1\n")
    _mk(tmp_path, "pkg/src/transports/util.test.ts", "import { x } from './util.js'\n")
    _mk(tmp_path, "pkg/src/util.ts", "export const y=1\n")
    _mk(tmp_path, "pkg/src/util.test.ts", "import { y } from './util.js'\n")
    got = V.default_tests_for(str(tmp_path), "pkg/src/tools/util.ts",
                              dir_fallback=False)
    assert got == os.path.join("pkg", "src", "tools", "tool-schemas.test.ts"), got


def test_colocated_still_wins_over_same_dir_sibling(tmp_path):
    # the same-dir deferral must NOT override a real co-located test
    _mk(tmp_path, "pkg/src/tools/util.ts", "export const a=1\n")
    _mk(tmp_path, "pkg/src/tools/util.test.ts", "import { a } from './util.js'\n")
    _mk(tmp_path, "pkg/src/tools/other.test.ts", "import { b } from './other.js'\n")
    got = V.default_tests_for(str(tmp_path), "pkg/src/tools/util.ts")
    assert got == os.path.join("pkg", "src", "tools", "util.test.ts"), got


def test_title_with_apostrophe_not_truncated():
    # EDGEVERDICT_316_x8: a title opened with ' that CONTAINS an apostrophe was
    # truncated at the inner quote, so -t / mark lookup missed the rendered
    # title ("name match failed"). Extraction must read to the matching quote
    # and unescape to the rendered form vitest reports.
    h = V()
    got = h.test_title(r"test('a call shouldn\'t throw', () => {})")
    assert got == "a call shouldn't throw", got
    # backtick and double-quote titles unaffected
    assert h.test_title("test(`plain title`, () => {})") == "plain title"
    assert h.test_title('it("double quoted", () => {})') == "double quoted"
    # a double-quoted title may freely contain an apostrophe
    assert h.test_title('''test("it shouldn't crash", () => {})''') \
        == "it shouldn't crash"


def test_mark_title_survives_apostrophe_title():
    h = V()
    code = r"test('a call shouldn\'t throw', () => {})"
    marked = h.mark_title(code, "___evX___")
    assert marked is not None and "___evX___" in marked
    # the marked title still reads back with its full rendered text
    assert h.test_title(marked) == "___evX___ a call shouldn't throw"
