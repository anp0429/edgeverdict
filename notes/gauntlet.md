# The launch gauntlet — final scorecard, increment 8 complete

Status: COMPLETE Jul 23, three days ahead of the bar. 10/10 stranger
repos reached a verdict unassisted. 0 false BROKENs. 13 confirmed gaps,
every one hand-checked against the repo's real contract. The tree under
test shipped to PyPI as 0.5.0 the same night; current release is 0.6.0.
Misses during the runs are listed below, per the rule: every miss
became a fix plus a named regression test before the repo was rerun
once.

Bar (from notes/prove.md): >=9/10 stranger repos reach BROKEN / HELD /
clean STOPPED unassisted; 0 false BROKENs; <3 min to first verdict;
run 11 = no-key machine gets the three-exit screen and a working demo.

## Scorecard (final)

| # | repo | sha replayed | t-to-verdict | verdict | false BROKENs | broken proposals | notes |
| --- | ---- | ------------ | ------------ | ------- | ------------- | ---------------- | ----- |
| 1 | unjs/ufo | 5cd9e67 | VERIFY-T1 | VERIFY-V1 | 0 | VERIFY-P1 | known ground |
| 2 | colinhacks/zod | fbe8ad1 | 345s | VERIFY-V2 | 0 | VERIFY-P2 | time exception recorded; one systematic finding, 8 repros, banked pending upstream engagement |
| 3 | unjs/defu | 40d7ef4 | VERIFY-T3 | VERIFY-V3 | 0 | VERIFY-P3 | |
| 4 | unjs/pathe | b52fcac | VERIFY-T4 | BROKEN | 0 | VERIFY-P4 | 3 confirmed gaps, filed upstream |
| 5 | unjs/ohash | f04e052 | VERIFY-T5 | VERIFY-V5 | 0 | VERIFY-P5 | |
| 6 | python-humanize/humanize | 823ad60 | VERIFY-T6 | BROKEN | 0 | VERIFY-P6 | 1 confirmed gap, filed upstream |
| 7 | marshmallow-code/marshmallow | ec2178c | VERIFY-T7 | BROKEN | 0 | VERIFY-P7 | 1 confirmed gap, minor, filed upstream |
| 8 | python-attrs/attrs | 0f758fe | VERIFY-T8 | VERIFY-V8 | 0 | VERIFY-P8 | |
| 9 | jd/tenacity | c650fb4 | VERIFY-T9 | VERIFY-V9 | 0 | VERIFY-P9 | |
| 10 | more-itertools | 0e6acdf | VERIFY-T10 | VERIFY-V10 | 0 | VERIFY-P10 | |

Totals: 10/10 verdicts, 0 false BROKENs, 13 confirmed gaps
(3 pathe, 8 zod banked, 1 humanize, 1 marshmallow).

Reading the quiet rows: a mature repo's most recent commit SHOULD
mostly come back HELD or covered. A gate that finds something
everywhere is describing itself, not the code.

False-BROKEN check, by hand, for every BROKEN: read the failing test —
does it assert the code's real contract, or the proposer's assumption?
One false BROKEN fails the whole gauntlet. Final count: zero.

## Misses (published, per the rule)

Each miss below cost a run, became a fix plus a named regression test,
and the repo was rerun once. The gauntlet measures prove; these are
what it caught in prove itself:

1. Import matching — proposals referenced symbols the host file never
   imported.
2. .d.ts files leaked into the surface — excluded.
3. .spec suffix test files not discovered — discovery widened.
4. Worktree mode missed diff-tests fallback at the callsite.
5. Smoke probe placed beside the tests file; npm noise stripped from
   verdict output.

## Run 11 — the no-key machine (PASS)

No key present: three-exit screen shown as designed. `agentboard demo`
reached BROKEN with the failing test in 14.9s. The smoke half of run 11
also caught a stale pip-install-from-git (an unmerged PR) — fixed by
merging, then a fresh smoke pass.

## Protocol (as run — kept for reproduction)

Setup, once, testing what strangers get:

python3 -m venv /tmp/gauntlet-venv
source /tmp/gauntlet-venv/bin/activate
pip install "git+https://github.com/anp0429/agentboard"
agentboard --help

Per repo, the replay trick: replay the repo's own most recent real
change as if an agent just wrote it — clone, pop the latest non-docs
commit back into the working tree, hand prove that commit's message as
the intent.

git clone <repo> /tmp/g
cd /tmp/g
git log --oneline -8
git reset HEAD~1
agentboard prove --intent "<that commit's subject line>"

Eligibility check before counting a repo (2 min): tests exist, the
runner is vitest or pytest, install succeeds. A repo that fails
eligibility is swapped, not scored — the gauntlet measures prove, not
npm's mood. A repo that passes eligibility and then stops dirty counts
as a miss unless the STOPPED cause is honest and actionable.

Two known-ground repos per lane was deliberate: they calibrate (we
knew what honest output looks like there); the rest were true
strangers.

## Cost + time

Spend is proposals only (~1 min sampling per changed file; the gate is
seconds). Ten runs landed within the 30-60 min wall-clock budget and a
few dollars, with zod's 345s the one recorded exception to the
per-repo bar.

## After (done)

Green Jul 23 → tagged 0.5.0, published to PyPI, README prove section
merged, count frozen in notes/prove-birth.md. This sheet published as
promised — misses included. Receipts, not memory.