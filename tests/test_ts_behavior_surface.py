# EDGEVERDICT_TS_BEHAVIOR_SURFACE_TESTS_V1
"""The TS/JS twin of test_behavior_surface: ts_behavior_surface pulls the
target file's VISIBLE decisions (module constants, early-exit guards,
one-hop relative helper bodies) so the proposer stops inventing specs the
shown lines contradict. Same contract as the Python side: facts from the
source, never judgment, "" on any trouble, cap drops helpers then guards
never constants. Regex-grade parser — anything it cannot read cleanly it
drops rather than guessing.
"""
from __future__ import annotations

from edgeverdict.agents.reviewer_agent import behavior_surface
from edgeverdict.ts_behavior import ts_behavior_surface


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_screaming_constant_appears_verbatim(tmp_path):
    _write(tmp_path, "src/m.ts",
           'export const NONE_ALLOWED = ["is_not"];\n'
           "export function f(x: string): boolean { return true; }\n")
    out = ts_behavior_surface(str(tmp_path), "src/m.ts")
    assert "NONE_ALLOWED" in out
    assert '["is_not"]' in out
    assert "module constants" in out


def test_lowercase_literal_constant_is_kept(tmp_path):
    # a lowercase const with a literal value is still a visible decision
    _write(tmp_path, "src/m.ts",
           'const noneOps = ["is_not"];\n'
           "export function f(): void {}\n")
    out = ts_behavior_surface(str(tmp_path), "src/m.ts")
    assert "noneOps" in out


def test_arrow_const_is_not_a_constant(tmp_path):
    # `const f = () => ...` is a helper, not a decision constant
    _write(tmp_path, "src/m.ts",
           "export const doThing = (x: number) => x + 1;\n")
    out = ts_behavior_surface(str(tmp_path), "src/m.ts")
    assert "doThing = (x" not in out  # not surfaced as a constant


def test_early_return_guard_is_captured(tmp_path):
    _write(tmp_path, "src/m.ts",
           "export function match(op: string, v: unknown): boolean {\n"
           "  if (op == null) return false;\n"
           "  return true;\n"
           "}\n")
    out = ts_behavior_surface(str(tmp_path), "src/m.ts")
    assert "op == null" in out
    assert "guard clauses" in out


def test_throw_guard_is_captured(tmp_path):
    _write(tmp_path, "src/m.ts",
           "export function f(x: unknown): void {\n"
           '  if (x === undefined) throw new Error("no x");\n'
           "}\n")
    out = ts_behavior_surface(str(tmp_path), "src/m.ts")
    assert "x === undefined" in out
    assert "throw" in out


def test_one_hop_relative_helper_body_included(tmp_path):
    _write(tmp_path, "src/helpers.ts",
           "export function strLower(v: unknown): string {\n"
           "  return String(v).toLowerCase();\n"
           "}\n")
    _write(tmp_path, "src/m.ts",
           'import { strLower } from "./helpers";\n'
           "export function f(v: unknown): string { return strLower(v); }\n")
    out = ts_behavior_surface(str(tmp_path), "src/m.ts")
    assert "strLower" in out
    assert "toLowerCase" in out
    assert "helpers called here" in out
    assert "// from" in out


def test_uncalled_import_is_not_hopped(tmp_path):
    # imported but never called -> not a decision this file relies on
    _write(tmp_path, "src/helpers.ts",
           "export function unused(): void {}\n")
    _write(tmp_path, "src/m.ts",
           'import { unused } from "./helpers";\n'
           "export const RULE = 1;\n")
    out = ts_behavior_surface(str(tmp_path), "src/m.ts")
    assert "helpers called here" not in out


def test_bare_package_import_is_not_hopped(tmp_path):
    # a node_modules import is not our code to explain
    _write(tmp_path, "src/m.ts",
           'import { thing } from "some-package";\n'
           "export function f(): unknown { return thing(); }\n")
    out = ts_behavior_surface(str(tmp_path), "src/m.ts")
    assert "some-package" not in out


def test_non_ts_returns_empty(tmp_path):
    _write(tmp_path, "src/m.py", "X = 1\n")
    assert ts_behavior_surface(str(tmp_path), "src/m.py") == ""


def test_missing_file_returns_empty(tmp_path):
    assert ts_behavior_surface(str(tmp_path), "src/nope.ts") == ""


def test_empty_when_nothing_to_show(tmp_path):
    # a file with only types/interfaces has no runtime decisions
    _write(tmp_path, "src/m.ts",
           "export interface Foo { a: number; }\n"
           "export type Bar = string;\n")
    out = ts_behavior_surface(str(tmp_path), "src/m.ts")
    assert out == ""


def test_comment_cannot_mint_a_constant(tmp_path):
    # a decision named only in a comment must not be surfaced
    _write(tmp_path, "src/m.ts",
           "// export const FAKE_RULE = [1,2,3];\n"
           "export const REAL_RULE = [4];\n")
    out = ts_behavior_surface(str(tmp_path), "src/m.ts")
    assert "FAKE_RULE" not in out
    assert "REAL_RULE" in out


def test_cap_drops_helpers_before_constants(tmp_path):
    # a cap that fits the constants + header but not the big helper body:
    # the helper section is dropped, the constant survives (drop order is
    # helpers, then guards, never constants).
    _write(tmp_path, "src/helpers.ts",
           "export function big(): string {\n"
           "  return 'x'.repeat(50) + 'y'.repeat(50) + 'z'.repeat(50);\n"
           "}\n")
    _write(tmp_path, "src/m.ts",
           "export const KEEP_ME = [1];\n"
           'import { big } from "./helpers";\n'
           "export function f(): string { return big(); }\n")
    full = ts_behavior_surface(str(tmp_path), "src/m.ts", cap_chars=9000)
    assert "big" in full and "KEEP_ME" in full  # both present uncapped
    # cap just below the full length forces the helper body out
    capped = ts_behavior_surface(str(tmp_path), "src/m.ts",
                                 cap_chars=len(full) - 50)
    assert "KEEP_ME" in capped          # constant survives
    assert "helpers called here" not in capped  # helper section dropped
    assert len(capped) <= len(full) - 50


def test_behavior_surface_dispatches_ts_to_the_ts_path(tmp_path):
    # the public behavior_surface must route a .ts target to the TS surface
    _write(tmp_path, "src/m.ts",
           'export const NONE_ALLOWED = ["is_not"];\n'
           "export function f(): boolean { return true; }\n")
    out = behavior_surface(str(tmp_path), "src/m.ts")
    assert "NONE_ALLOWED" in out
    assert "VISIBLE BEHAVIOR" in out
