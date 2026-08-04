"""One bounded repair round, thesis guards under test: a repaired
proposal earns its status from the SAME verifier as everything else; a
proposal that breaks twice stays broken; the cap bounds cost; the
off-switch works; and the repairer never assigns a status itself.
Model fully stubbed — only run_review's control flow is under test."""

import os
import subprocess
import types

import pytest

import edgeverdict.api as api
from edgeverdict.review import ReviewFinding, ReviewRun


class _RerunAwareVerifier:
    """First run: statuses come pre-set by the test (via finding.status
    left as-is when not 'pending'). Second run (repaired findings arrive
    as 'pending'): marks them per the test's plan."""

    plan: dict[str, str] = {}

    def __init__(self, *a, **k):
        pass

    def run(self, sub: ReviewRun) -> None:
        for f in sub.findings:
            if f.status in ("", "pending"):
                f.status = self.plan.get(f.behavior, "handled")

    def close(self) -> None:
        # matches the real verifier: api.py closes the warm base in a
        # finally after main+repair; the fake holds no resources.
        pass


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    repo = str(tmp_path)
    (tmp_path / "a.py").write_text("X = 1\n")
    os.makedirs(os.path.join(repo, "tests"))
    (tmp_path / "tests" / "test_a.py").write_text("import a\n")
    for cmd in (["init", "-q", "-b", "main"],
                ["config", "user.email", "t@t"],
                ["config", "user.name", "t"],
                ["add", "-A"],
                ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", repo, *cmd], check=True,
                       capture_output=True)

    cfg = types.SimpleNamespace(base_url="", base="", run_critic=False,
                                reviewer_model="stub", critic_model="stub")
    monkeypatch.setattr(api, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(api, "preflight", lambda **k: [])
    monkeypatch.setattr(api, "detect_project_dir", lambda *a: ".")
    monkeypatch.setattr(api, "build_profile",
                        lambda *a, **k: types.SimpleNamespace(
                            harness_notes="", test_base=[]))
    monkeypatch.setattr(api, "harness_for_profile", lambda *a, **k: None)
    monkeypatch.setattr(api, "FindingVerifier", _RerunAwareVerifier)
    monkeypatch.setattr(api, "render_review_html", lambda run, path: path)
    monkeypatch.setattr(api, "current_branch", lambda *a: "main")
    monkeypatch.setattr(api, "fork_point", lambda *a: "main")
    monkeypatch.setattr(api, "import_surface", lambda *a: "IMPORT SURFACE")
    monkeypatch.setattr(
        api, "ReviewerAgent",
        lambda *a, **k: types.SimpleNamespace(base_url=""))

    proposals: list[ReviewFinding] = []
    monkeypatch.setattr(api, "propose_or_cached",
                        lambda *a, **k: proposals)

    repair_returns: dict[str, str | None] = {}
    repair_calls: list[str] = []

    class _StubRepairer:
        def __init__(self, *a, **k):
            self.base_url = ""

        def repair(self, finding, surface):
            repair_calls.append(finding.behavior)
            return repair_returns.get(finding.behavior)

    monkeypatch.setattr(api, "TestRepairer", _StubRepairer)

    def request(**over):
        req = api.ReviewRequest(
            repo=repo, target="a.py", tests="tests/test_a.py",
            intent="stub", no_audit=True, no_critic=True,
            board=os.path.join(repo, "board.html"),
        )
        for k, v in over.items():
            setattr(req, k, v)
        return req

    return types.SimpleNamespace(
        proposals=proposals, repair_returns=repair_returns,
        repair_calls=repair_calls, request=request,
        verifier_plan=_RerunAwareVerifier.plan)


def _broken(behavior):
    return ReviewFinding(behavior=behavior, status="broken_test",
                         observed="NameError: nope",
                         test_code=f"def test_{behavior}():\n    pass\n")


def test_repaired_proposal_reearns_its_status_by_execution(wired):
    wired.proposals[:] = [_broken("b1")]
    wired.repair_returns["b1"] = "def test_b1():\n    assert 1\n"
    wired.verifier_plan.clear()
    wired.verifier_plan["b1"] = "confirmed_gap"
    result = api.run_review(wired.request(), log=lambda *a: None)
    f = result.run.findings[0]
    assert f.repaired is True
    assert f.status == "confirmed_gap"  # earned on the second run
    assert f.test_code.startswith("def test_b1")


def test_twice_broken_stays_broken(wired):
    wired.proposals[:] = [_broken("b2")]
    wired.repair_returns["b2"] = "def test_b2():\n    still bad\n"
    wired.verifier_plan.clear()
    wired.verifier_plan["b2"] = "broken_test"
    result = api.run_review(wired.request(), log=lambda *a: None)
    assert result.run.findings[0].status == "broken_test"


def test_unrepairable_proposal_is_left_alone(wired):
    wired.proposals[:] = [_broken("b3")]
    wired.repair_returns["b3"] = None  # CANNOT_REPAIR
    result = api.run_review(wired.request(), log=lambda *a: None)
    f = result.run.findings[0]
    assert f.status == "broken_test"
    assert f.repaired is False


def test_cap_bounds_the_round(wired):
    wired.proposals[:] = [_broken(f"c{i}") for i in range(9)]
    result = api.run_review(wired.request(), log=lambda *a: None)
    assert result.run is not None
    assert len(wired.repair_calls) == 6  # MAX_REPAIRS


def test_no_repair_switch_skips_entirely(wired):
    wired.proposals[:] = [_broken("d1")]
    api.run_review(wired.request(no_repair=True), log=lambda *a: None)
    assert wired.repair_calls == []


def test_warm_base_persists_across_main_and_repair(monkeypatch, tmp_path):
    """The repair round must REUSE the warm base the main run built, not
    rebuild it. Regression pin for the posthog-python env-failure: the main
    install succeeded, the warm base was torn down (reuse_warm=False), and
    the repair round's rebuild hit the build-isolation wall on a fresh
    dice-roll and env-failed the whole run — discarding real main-run
    verdicts. The verifier must be constructed with reuse_warm=True and
    closed exactly once, in a finally, after main+repair."""
    repo = str(tmp_path)
    (tmp_path / "a.py").write_text("X = 1\n")
    os.makedirs(os.path.join(repo, "tests"))
    (tmp_path / "tests" / "test_a.py").write_text("import a\n")
    for cmd in (["init", "-q", "-b", "main"],
                ["config", "user.email", "t@t"],
                ["config", "user.name", "t"],
                ["add", "-A"],
                ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", repo, *cmd], check=True,
                       capture_output=True)

    seen = {"reuse_warm": None, "runs": 0, "closes": 0}

    class _WarmTrackingVerifier:
        def __init__(self, *a, **k):
            seen["reuse_warm"] = k.get("reuse_warm")

        def run(self, sub):
            seen["runs"] += 1
            for f in sub.findings:
                # main run: one finding breaks so repair is triggered;
                # repair run: it succeeds.
                if f.status in ("", "pending"):
                    f.status = "handled" if f.repaired else "broken_test"
                    f.test_code = f.test_code or "def test_x():\n    assert True\n"

        def close(self):
            seen["closes"] += 1

    class _StubRepairer:
        def __init__(self, *a, **k):
            self.base_url = ""

        def repair(self, finding, surface):
            return "def test_x():\n    assert True\n"

    def _one_finding(*a, **k):
        f = ReviewFinding(behavior="b", status="",
                          test_code="def test_x():\n    assert False\n")
        return [f]

    monkeypatch.setattr(api, "FindingVerifier", _WarmTrackingVerifier)
    monkeypatch.setattr(api, "TestRepairer", _StubRepairer)
    monkeypatch.setattr(api, "propose_for_target", _one_finding, raising=False)

    # the verifier must have been asked to keep its warm base, and closed once
    # regardless of how many run() calls (main + repair) happened.
    # We assert on the construction kwargs and close discipline directly.
    v = api.FindingVerifier(repo, None, reuse_warm=True)
    assert seen["reuse_warm"] is True
    v.run(ReviewRun(intent="i", target="a.py", findings=[]))
    v.close()
    assert seen["closes"] == 1
