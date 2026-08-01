# EDGEVERDICT_HOST_SCOPE_TESTS_V1
"""The proposer is told what is in scope, and the cache tracks the prompt.

A proposal is written as if it lived in the module it describes, but it is
injected into one specific existing tests file, and scope is per file. The diff
routinely shows tests from OTHER files using helpers those files define
locally. supabase/mcp defines `setup` as a plain local function in
server.test.ts and exports nothing; a proposal for tool-schemas.ts copied that
call into tool-schemas.test.ts, where the name does not exist, and three
verdicts were lost to one unbound identifier.
"""
import edgeverdict.agents.reviewer_agent as ra
from edgeverdict.agents.reviewer_agent import host_scope, prompt_fingerprint
from edgeverdict.proposal_cache import proposal_key

TS_HOST = """import { describe, expect, test } from 'vitest';
import { createToolSchemas } from './tool-schemas.js';

const MODULE_LEVEL = 1;

describe('a suite', () => {
  test('a case', () => {
    const scopedToThisTest = createToolSchemas();
    expect(scopedToThisTest).toBeDefined();
  });
});
"""


def _names(block: str) -> set[str]:
    return set(block.splitlines()[1].strip().split(", "))


def test_ts_scope_lists_module_level_names_only():
    names = _names(host_scope("pkg/src/tools/x.test.ts", TS_HOST))
    assert {"describe", "expect", "test", "createToolSchemas",
            "MODULE_LEVEL"} <= names
    # declared inside a test body: NOT available to an injected sibling test.
    # Listing it would cause the exact bug this block exists to prevent.
    assert "scopedToThisTest" not in names


def test_ts_scope_omits_a_helper_the_host_does_not_have():
    """The supabase/mcp case, in miniature."""
    names = _names(host_scope("pkg/src/tools/x.test.ts", TS_HOST))
    assert "setup" not in names


def test_scope_block_names_the_host_and_warns_about_the_diff():
    block = host_scope("pkg/src/tools/x.test.ts", TS_HOST)
    assert "pkg/src/tools/x.test.ts" in block
    assert "pkg/src/tools" in block          # where imports resolve from
    assert "diff" in block.lower()           # the actual failure mode
    assert "discarded" in block.lower()      # and its cost


def test_python_scope_uses_the_ast():
    src = ("import os\n"
           "from pathlib import Path as P\n"
           "def helper():\n"
           "    inner = 1\n"
           "    return inner\n"
           "CONST = 2\n")
    names = _names(host_scope("tests/test_x.py", src))
    assert {"os", "P", "helper", "CONST"} <= names
    assert "inner" not in names


def test_empty_or_unparseable_host_yields_no_block():
    assert host_scope("tests/test_x.py", "") == ""
    assert host_scope("tests/test_x.py", "def (:::") == ""


def test_cache_key_changes_when_the_prompt_changes():
    """Hashing only the inputs meant an instruction edit produced an identical
    key, so the next run replayed proposals written under the old wording and
    the change looked like it had done nothing."""
    kw = dict(intent="i", change="c", source="s", tests="t",
              reviewer_model="m", critic_model="m", harness_notes="n",
              run_critic=True)
    before = proposal_key(**kw)
    original = ra._SYSTEM
    try:
        ra._SYSTEM = original + "\nAn added instruction."
        assert proposal_key(**kw) != before
    finally:
        ra._SYSTEM = original
    assert proposal_key(**kw) == before


def test_prompt_fingerprint_is_stable_and_short():
    first = prompt_fingerprint()
    assert first == prompt_fingerprint()
    assert len(first) == 16
