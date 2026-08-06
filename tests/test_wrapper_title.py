# EDGEVERDICT_WRAPPER_TITLE_TESTS_V1
"""A test declared through a custom wrapper (e.g. pg-meta's
withTestDatabase('title', async ({db}) => ...)) must still yield its title.
Repos with shared test setup wrap the runner, and those are exactly the
DB-backed repos — so 'could not read test name' on every proposal blocked
the whole supabase/pg-meta monorepo run until test_title generalized."""
from __future__ import annotations

from edgeverdict.verifiers.harness import VitestHarness


def _h() -> VitestHarness:
    return VitestHarness()


def test_withtestdatabase_wrapper_title():
    code = ("withTestDatabase(\n  'maps composite-FK columns positionally',\n"
            "  async ({ executeQuery }) => {")
    assert _h().test_title(code) == "maps composite-FK columns positionally"


def test_wrapper_with_plain_function():
    assert _h().test_title(
        "dbTest('db case', function() {})") == "db case"


def test_standard_test_still_works():
    assert _h().test_title("test('basic', () => {})") == "basic"


def test_standard_it_still_works():
    assert _h().test_title("it('it form', async () => {})") == "it form"


def test_apostrophe_title_not_truncated():
    # the supabase/mcp#316 fix must survive the generalization
    assert _h().test_title(
        "test(\"shouldn't truncate\", () => {})") == "shouldn't truncate"


def test_non_test_string_call_returns_none():
    assert _h().test_title("console.log('just a message')") is None


def test_expect_call_returns_none():
    assert _h().test_title("expect(x).toBe('y')") is None


def test_outer_wrapper_wins_over_inner_string_callback():
    # inner executeQuery('sql', () => ...) must not steal the title from the
    # outer wrapper — first match is the outermost declaration.
    code = ("withTestDatabase('the real title', async ({ executeQuery }) => {\n"
            "  await executeQuery('create table foo', () => {})\n})")
    assert _h().test_title(code) == "the real title"
