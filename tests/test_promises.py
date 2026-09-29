"""promises: execute what a repo's own docs promise.

sig EDGEVERDICT_PROMISES_V1. Every verdict here runs a real bash probe
against a real scratch repo; no model is involved in any assertion.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from edgeverdict.agents.promise_agent import PromiseAgent, _loads
from edgeverdict.execution import LocalBackend
from edgeverdict.promises import (
    Promise, classify, collect, ground, parse_promises, run_probe, verify,
)
from edgeverdict.promises_cli import promises as promises_cmd

pytestmark = pytest.mark.skipif(shutil.which("bash") is None,
                                reason="needs bash")

README = """# tool

`./setup.sh <dir>` writes config into a project.
Safe to run twice: it never overwrites a file that already exists.

**Counter.** Each call to `./next.sh` prints the next number, starting at 1.
"""

SETUP = """#!/usr/bin/env bash
set -eu
mkdir -p "$1"
[ -e "$1/config" ] || echo "v1" > "$1/config"
"""

# deliberately buggy: never persists its state, so it always prints 1
NEXT = """#!/usr/bin/env bash
n=$(cat .count 2>/dev/null || echo 0)
echo $((n + 1))
"""


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text(README)
    (root / "setup.sh").write_text(SETUP)
    (root / "next.sh").write_text(NEXT)
    os.chmod(root / "setup.sh", 0o755)
    os.chmod(root / "next.sh", 0o755)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("x")
    return str(root)


def _p(probe, quote="Safe to run twice: it never overwrites a file",
       source="README.md", claim="setup is safe to run twice"):
    return Promise(claim=claim, quote=quote, source=source,
                   scenario="s", probe=probe)


BACKEND = LocalBackend(trusted_fixture=True)


# ------------------------------------------------------------ classify

@pytest.mark.parametrize("rc,out,status", [
    (1, "EDGEVERDICT_PROMISE_BROKEN: got 1 twice\n", "confirmed_gap"),
    (0, "EDGEVERDICT_PROMISE_HELD: fine\n", "handled"),
    (3, "EDGEVERDICT_MISSING_TOOL: jq\n", "broken_test"),
    (1, "", "broken_test"),                       # set -e crash is not a gap
    (0, "", "broken_test"),                       # fell off the end
    (0, "EDGEVERDICT_PROMISE_BROKEN: x\n", "broken_test"),  # marker/rc lie
    (1, "EDGEVERDICT_PROMISE_HELD: x\n", "broken_test"),
    (4, "", "broken_test"),                       # setup failed
])
def test_classify_requires_marker_and_exit_code_to_agree(rc, out, status):
    assert classify(rc, out, "")[0] == status


def test_classify_last_marker_wins_and_names_missing_tool():
    out = "EDGEVERDICT_PROMISE_HELD: a\nEDGEVERDICT_PROMISE_BROKEN: b\n"
    assert classify(1, out, "") == ("confirmed_gap", "b")
    status, observed = classify(3, "EDGEVERDICT_MISSING_TOOL: jq\n", "")
    assert status == "broken_test" and "'jq'" in observed


def test_classify_timeout_is_inconclusive():
    assert classify(None, "", "", timed_out=True)[0] == "timed_out"


# -------------------------------------------------------------- ground

def test_ground_accepts_verbatim_quote_through_markdown_noise(repo):
    p = _p('must true\nbroken "x"', quote="Each call to ./next.sh prints the next number")
    ground([p], repo)
    assert p.grounded, p.drop_reason


@pytest.mark.parametrize("kw,reason", [
    ({"quote": "It is always safe to run as many times as you like"}, "not found"),
    ({"source": "NOPE.md"}, "not a file"),
    ({"source": "../outside.md"}, "not a file"),
    ({"quote": "Safe to run"}, "too short"),
])
def test_ground_drops_promises_the_repo_does_not_make(repo, kw, reason):
    p = _p('must true\nbroken "x"', **kw)
    ground([p], repo)
    assert not p.grounded and reason in p.drop_reason


def test_ground_refuses_probe_without_failure_path(repo):
    p = _p('must true\nheld "always"')
    ground([p], repo)
    assert not p.grounded and "failure path" in p.drop_reason


def test_ground_refuses_probe_that_never_uses_must_or_attempt(repo):
    p = _p('x=$(bash ./next.sh)\n[ "$x" = 2 ] || broken "got $x"\nheld ok\n')
    ground([p], repo)
    assert not p.grounded and "must/attempt" in p.drop_reason


# ----------------------------------------------------------- execution

HOLDS = r'''
must ./setup.sh "$WORK/p"
echo custom > "$WORK/p/config"
must ./setup.sh "$WORK/p"
[ "$(cat "$WORK/p/config")" = custom ] || broken "config overwritten: $(cat "$WORK/p/config")"
held "config kept"
'''

BREAKS = r'''
a=$(must bash ./next.sh); b=$(must bash ./next.sh)
[ "$a" != "$b" ] || broken "two calls both printed $a"
held "got $a then $b"
'''


def test_run_probe_held_and_broken_on_real_scripts(repo):
    assert run_probe(_p(HOLDS), repo, BACKEND)[0] == "handled"
    status, observed = run_probe(_p(BREAKS), repo, BACKEND)
    assert status == "confirmed_gap" and "both printed 1" in observed


def test_setup_crash_never_wears_the_gap_costume(repo):
    crash = 'set -e\nfalse\nbroken "unreachable"\n'
    assert run_probe(_p(crash), repo, BACKEND)[0] == "broken_test"


def test_missing_tool_is_reported_by_name(repo):
    status, observed = run_probe(
        _p('need definitely-not-a-tool-xyz\nbroken "x"\n'), repo, BACKEND)
    assert status == "broken_test" and "definitely-not-a-tool-xyz" in observed


def test_each_probe_gets_a_fresh_copy_and_the_real_repo_is_untouched(repo):
    dirty = 'echo 9 > .count\nrm -f README.md\nheld "dirtied"\nbroken "x"\n'
    check = '[ -e .count ] && broken "state leaked from another probe"\nheld "clean"\n'
    assert run_probe(_p(dirty), repo, BACKEND)[0] == "handled"
    assert run_probe(_p(check), repo, BACKEND)[0] == "handled"
    assert os.path.exists(os.path.join(repo, "README.md"))
    assert not os.path.exists(os.path.join(repo, ".count"))


def test_probe_copy_skips_dependency_dirs(repo):
    probe = '[ -d node_modules ] && broken "copied node_modules"\nheld ok\n'
    assert run_probe(_p(probe), repo, BACKEND)[0] == "handled"


def test_probe_timeout(repo):
    assert run_probe(_p('sleep 5\nbroken "x"\n'), repo, BACKEND,
                     timeout=1)[0] == "timed_out"


def test_verify_drops_ungrounded_and_runs_the_rest(repo):
    fake = _p('must true\nbroken "x"', quote="Encrypted at rest with a per-team key")
    res = verify([_p(BREAKS), fake], repo, BACKEND, log=lambda *a: None)
    assert [f.status for f in res.run.findings] == ["confirmed_gap"]
    assert res.dropped == [fake]
    f = res.run.findings[0]
    assert f.test_code == BREAKS and "README.md" in f.coverage_note


# ------------------------------------------------------ parse / collect

def test_parse_promises_drops_malformed():
    data = {"promises": [
        {"claim": "a", "quote": "q", "source": "./README.md", "probe": "x"},
        {"claim": "no probe", "quote": "q", "source": "R.md"},
        "junk",
    ]}
    out = parse_promises(data)
    assert len(out) == 1 and out[0].source == "README.md"


def test_collect_reads_readme_first_and_scripts_it_mentions(repo):
    text = collect(repo)
    assert list(text.docs)[0] == "README.md"
    assert {"setup.sh", "next.sh"} <= set(text.code)
    assert not any("node_modules" in t for t in text.tree)


# --------------------------------------------------------------- agent

def _openai_client(content):
    msg = SimpleNamespace(content=content)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg,
                                                    finish_reason="stop")])
    create = lambda **kw: resp  # noqa: E731
    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


def test_agent_parses_model_json(repo):
    body = json.dumps({"promises": [{"claim": "c", "quote": "q",
                                     "source": "README.md", "scenario": "s",
                                     "probe": BREAKS}]})
    agent = PromiseAgent(model="gpt-5.5", client=_openai_client(body))
    got = agent.propose(collect(repo))
    assert len(got) == 1 and got[0].probe.strip() == BREAKS.strip()


def test_loads_salvages_truncated_response():
    text = ('{"promises":[{"claim":"a","quote":"q","source":"R","probe":"x }"},'
            '{"claim":"b","pro')
    assert [p["claim"] for p in _loads(text)["promises"]] == ["a"]


# ----------------------------------------------------------------- cli

def _args(repo, **kw):
    base = dict(repo=repo, model="gpt-5.5", max=8, probes="", timeout=60,
                board="")
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_replays_saved_probes_and_exits_2_on_broken(repo, tmp_path, capsys):
    probes = tmp_path / "p.json"
    probes.write_text(json.dumps({"promises": [
        {"claim": "counter increments",
         "quote": "Each call to ./next.sh prints the next number, starting at 1",
         "source": "README.md", "scenario": "call twice", "probe": BREAKS}]}))
    board = str(tmp_path / "b.html")
    rc = promises_cmd(_args(repo, probes=str(probes), board=board),
                      backend=BACKEND)
    out = capsys.readouterr().out
    assert rc == 2 and "BROKEN: 1 confirmed gap" in out
    assert "both printed 1" in open(board).read()


def test_cli_model_run_saves_replayable_probes(repo, tmp_path, capsys):
    body = json.dumps({"promises": [{"claim": "setup twice",
                                     "quote": "Safe to run twice: it never overwrites a file",
                                     "source": "README.md", "scenario": "s",
                                     "probe": HOLDS}]})
    agent = PromiseAgent(client=_openai_client(body))
    board = str(tmp_path / "b.html")
    rc = promises_cmd(_args(repo, board=board), backend=BACKEND, agent=agent)
    assert rc == 0 and "HELD" in capsys.readouterr().out
    saved = json.loads(open(str(tmp_path / "b.probes.json")).read())
    assert saved["promises"][0]["probe"].strip() == HOLDS.strip()


def test_cli_no_model_screen(repo, monkeypatch, capsys):
    for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    assert promises_cmd(_args(repo), backend=BACKEND) == 1
    assert "--probes" in capsys.readouterr().out


def test_cli_all_dropped_is_stopped_not_held(repo, tmp_path, capsys):
    probes = tmp_path / "p.json"
    probes.write_text(json.dumps({"promises": [
        {"claim": "x", "quote": "This tool is certified secure by NIST",
         "source": "README.md", "scenario": "s",
         "probe": 'must true\nbroken "x"'}]}))
    rc = promises_cmd(_args(repo, probes=str(probes),
                            board=str(tmp_path / "b.html")), backend=BACKEND)
    assert rc == 1 and "STOPPED" in capsys.readouterr().out


def test_cli_entrypoint_is_wired():
    r = subprocess.run([sys.executable, "-m", "edgeverdict.cli",
                        "promises", "--help"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "--probes" in r.stdout


def test_docker_path_mounts_scratch_offline_without_secrets(repo, monkeypatch):
    """The probe is model-written shell: under Docker it must get the
    scratch dir as its only mount, no network even when installs are
    allowed network, and no host secrets."""
    from edgeverdict.execution import DockerBackend, DockerLimits

    monkeypatch.setattr("edgeverdict.execution.shutil.which",
                        lambda _: "/usr/bin/docker")

    class Probe:
        returncode = 0

    monkeypatch.setattr("edgeverdict.execution.subprocess.run",
                        lambda *a, **k: Probe())
    monkeypatch.setenv("EDGEVERDICT_SANDBOX_NETWORK", "install")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-leak")
    seen = {}

    class Recording(DockerBackend):
        def run(self, args, *, cwd, env, timeout):
            seen["cmd"] = self._docker_command(args, cwd=cwd, env=env,
                                               name="ev-promise")
            seen["cwd"] = cwd
            seen["probe"] = open(os.path.join(cwd, "probe.sh")).read()
            return subprocess.CompletedProcess(
                args, 0, "EDGEVERDICT_PROMISE_HELD: ok\n", "")

    backend = Recording(image="edgeverdict-sandbox:test",
                        limits=DockerLimits(), log=lambda *a, **k: None)
    status, _ = run_probe(_p(HOLDS), repo, backend)
    joined = " ".join(seen["cmd"])
    assert status == "handled"
    assert "--network none" in joined
    assert f"src={os.path.realpath(seen['cwd'])}," in joined
    assert "--workdir /edgeverdict " in joined + " "
    assert seen["cmd"][-2:] == ["bash", "probe.sh"]
    assert "sk-should-never-leak" not in joined
    assert seen["probe"].startswith("#!/usr/bin/env bash")
    assert not os.path.exists(seen["cwd"])  # scratch removed after the run


def test_cli_proposer_failure_is_stopped_not_a_traceback(repo, tmp_path, capsys):
    class Boom:
        def propose(self, text):
            raise RuntimeError("Error code: 401 - invalid_api_key")
    rc = promises_cmd(_args(repo, board=str(tmp_path / "b.html")),
                      backend=BACKEND, agent=Boom())
    out = capsys.readouterr().out
    assert rc == 1 and out.count("STOPPED") == 1 and "key was rejected" in out



# ---------------------------------------------- V2: must / attempt contract

def test_classify_errored_marker_beats_a_later_broken():
    markers = ("EDGEVERDICT_UNDER_TEST_ERRORED: rc=128 cmd=git push\n"
               "EDGEVERDICT_PROMISE_BROKEN: got nothing\n")
    status, observed = classify(1, "", "", markers=markers)
    assert status == "broken_test" and "rc=128" in observed


def test_crash_inside_capture_is_not_a_gap(repo):
    """The live team-context shape: the command under test crashed inside
    $(...), the probe saw an empty value and called broken."""
    probe = ('a=$(must bash -c "echo boom >&2; exit 128")\n'
             '[ -n "$a" ] || broken "no number came back"\nheld ok\n')
    status, observed = run_probe(_p(probe), repo, BACKEND)
    assert status == "broken_test"
    assert "rc=128" in observed and "boom" in observed


def test_crash_in_background_job_is_not_a_gap(repo):
    probe = ('must bash -c "exit 7" &\nmust bash ./next.sh >/dev/null &\n'
             'wait\nbroken "concurrent runs disagreed"\n')
    status, observed = run_probe(_p(probe), repo, BACKEND)
    assert status == "broken_test" and "rc=7" in observed


def test_attempt_lets_failure_be_the_observed_value(repo):
    probe = ('attempt bash -c "echo out; echo err >&2; exit 3"\n'
             '[ "$RC" = 3 ] && [ "$OUT" = out ] && [ "$ERR" = err ] '
             '|| broken "rc=$RC out=$OUT err=$ERR"\nheld "rc=$RC"\n')
    assert run_probe(_p(probe), repo, BACKEND) == ("handled", "rc=3")


def test_must_passes_stdout_through_for_capture(repo):
    probe = ('a=$(must bash ./next.sh)\n[ "$a" = 1 ] || broken "got $a"\n'
             'held "got $a"\n')
    assert run_probe(_p(probe), repo, BACKEND) == ("handled", "got 1")


# -------------------------------------------- V2: reference-driven collect

@pytest.fixture
def layered(tmp_path):
    """Generic shape: docs name a script, that script calls a helper the
    docs never mention, a data sample lives in examples/, a test exists."""
    root = tmp_path / "layered"
    (root / "bin").mkdir(parents=True)
    (root / "examples").mkdir()
    (root / "tests").mkdir()
    (root / "README.md").write_text("Run `bin/run.sh input.jsonl`.\n")
    (root / "bin" / "run.sh").write_text('bash "$DIR/lib/helper.sh" "$1"\n')
    (root / "lib").mkdir()
    (root / "lib" / "helper.sh").write_text("jq -c . \"$1\"\n")
    (root / "lib" / "unrelated.sh").write_text("echo hi\n")
    (root / "examples" / "input.jsonl").write_text('{"type":"user"}\n' * 5)
    (root / "tests" / "test_run.sh").write_text("bin/run.sh examples/input.jsonl\n")
    (root / "package-lock.json").write_text("{}" * 10)
    return str(root)


def test_collect_follows_references_one_hop(layered):
    text = collect(layered)
    order = list(text.code)
    assert order[0] == "bin/run.sh"
    assert "lib/helper.sh" in text.code          # reached only via run.sh
    assert order.index("lib/helper.sh") < order.index("lib/unrelated.sh") \
        if "lib/unrelated.sh" in order else True


def test_collect_labels_fixtures_and_tests_and_skips_lockfiles(layered):
    text = collect(layered)
    assert "examples/input.jsonl" in text.fixtures
    assert "tests/test_run.sh" in text.tests
    assert "package-lock.json" not in text.code


def test_collect_respects_gitignore(layered):
    if shutil.which("git") is None:
        pytest.skip("needs git")
    root = layered
    os.makedirs(os.path.join(root, "venvish", "lib"))
    with open(os.path.join(root, "venvish", "lib", "README.md"), "w") as fh:
        fh.write("vendored readme")
    with open(os.path.join(root, ".gitignore"), "w") as fh:
        fh.write("venvish/\n")
    subprocess.run(["git", "init", "-q", root], check=True)
    text = collect(root)
    assert not any(t.startswith("venvish/") for t in text.tree)


def test_references_resolves_paths_suffixes_and_basenames():
    from edgeverdict.promises import references
    tree = ["hooks/digest.sh", "hooks/propose.sh", "a/x.json", "b/x.json",
            "c/x.json", "d/x.json"]
    text = ('"$HOOK_DIR/digest.sh" and ./hooks/propose.sh and x.json')
    got = references(text, tree)
    assert {"hooks/digest.sh", "hooks/propose.sh"} <= got
    assert not any(g.endswith("x.json") for g in got)  # 4 hits: too ambiguous


def test_agent_prompt_carries_fixtures_and_tests(layered):
    seen = {}

    def create(**kw):
        seen["user"] = kw["messages"][1]["content"]
        msg = SimpleNamespace(content='{"promises": []}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg,
                                                        finish_reason="stop")])
    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    PromiseAgent(client=client).propose(collect(layered))
    assert "FIXTURE examples/input.jsonl" in seen["user"]
    assert "EXISTING TEST tests/test_run.sh" in seen["user"]
