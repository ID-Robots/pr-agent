"""Offline tests of the review-eval harness: gh and the model are fakes, git runs in tmp_path."""

import json
import socket
import subprocess
from pathlib import Path

import pytest

import pr_agent.git_providers as git_providers
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.run_details import record_ai_call
from tools.review_eval import cli
from tools.review_eval.dataset import load_dataset, select_entries
from tools.review_eval.github import GhClient, GitStore
from tools.review_eval.handlers import cache_key
from tools.review_eval.provider import PROVIDER_ID, ReplayGitProvider, ReplayTarget, register_target, unregister_target
from tools.review_eval.units import build_units

OWNER, NAME, PR = "example-org", "widgets", 7
UNFETCHABLE = "0123456789abcdef0123456789abcdef01234567"

REVIEW_ANSWER = """review:
  estimated_effort_to_review_[1-5]: 2
  relevant_tests: |
    No
  key_issues_to_review:
    - relevant_file: |
        app.py
      issue_header: |
        Possible Bug
      issue_content: |
        Division by zero when the list is empty.
      start_line: 2
      end_line: 3
      issue_title: |
        The average fails on an empty list.
      severity: Major
      effort: Quick win
    - relevant_file: |
        app.py
      issue_header: |
        Unclear name
      issue_content: |
        The helper name does not say what it returns.
      start_line: not-a-line
      end_line: 1
      issue_title: |
        The name is unclear.
      severity: Trivial
      effort: Quick win
  security_concerns: |
    No
"""
IMPROVE_ANSWER = """code_suggestions:
  - relevant_file: |
      app.py
    language: |
      python
    existing_code: |
      return total / count
    suggestion_content: |
      Return 0 for an empty list instead of dividing by zero.
    improved_code: |
      return total / count if count else 0
    one_sentence_summary: |
      Guard the empty-list division
    label: |
      possible bug
"""
REFLECT_ANSWER = """code_suggestions:
  - suggestion_summary: |
      Guard the empty-list division
    relevant_file: |
      app.py
    relevant_lines_start: 3
    relevant_lines_end: 3
    suggestion_score: 8
    why: |
      It prevents a crash.
"""


class FakeModel(BaseAiHandler):
    """Answers each prompt the harness sends and reports usage the way LiteLLMAIHandler does."""

    calls: list = []

    def __init__(self):
        pass

    @property
    def deployment_id(self):
        return None

    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
        FakeModel.calls.append({"model": model, "system": system, "user": user})
        record_ai_call({"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100},
                       model=model, cost_usd=0.0125)
        if "same_issue" in system:
            return ('{"same_issue": %s, "reason": "r"}' % ("true" if "zero" in user else "false")), "stop"
        if '"valid"' in system:
            return '{"valid": true, "reason": "r"}', "stop"
        if "evaluating code suggestions" in system:
            return REFLECT_ANSWER, "stop"
        if "class CodeSuggestion" in system:
            return IMPROVE_ANSWER, "stop"
        return REVIEW_ANSWER, "stop"


class ForbiddenModel(FakeModel):
    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
        FakeModel.calls.append({"model": model, "system": system, "user": user})
        raise AssertionError("a dry run must not call the model")


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, message, files):
    for name, content in files.items():
        (repo / name).write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path):
    """A hosted repository stand-in whose pull request is already merged.

    main: B0 -> B1 (unrelated change) -> M (merges the feature); feature: B0 -> C1 (bug) -> C2 (fix).
    The pull request's base.sha is B1, so merge-base(C1, B1) = B0, while the live main tip M already
    contains C1 and would give an empty diff.
    """
    repo = tmp_path / "remote"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dev@example.invalid")
    _git(repo, "config", "user.name", "Dev")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    b0 = _commit(repo, "initial", {"app.py": "def average(values):\n    return 0\n", "other.py": "A = 1\n"})
    _git(repo, "checkout", "-qb", "feature")
    c1 = _commit(repo, "compute the average", {
        "app.py": "def average(values):\n    total, count = sum(values), len(values)\n    return total / count\n"})
    c2 = _commit(repo, "guard empty lists", {
        "app.py": "def average(values):\n    total, count = sum(values), len(values)\n"
                  "    return total / count if count else 0\n"})
    _git(repo, "update-ref", f"refs/pull/{PR}/head", c2)
    _git(repo, "checkout", "-q", "main")
    b1 = _commit(repo, "unrelated", {"other.py": "A = 2\n"})
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    merged_tip = _git(repo, "rev-parse", "HEAD")
    return {"path": repo, "b0": b0, "b1": b1, "c1": c1, "c2": c2, "tip": merged_tip}


def _comments(remote):
    return {
        101: {"original_commit_id": remote["c1"], "path": "app.py", "original_line": 3, "original_start_line": 2},
        103: {"original_commit_id": UNFETCHABLE, "path": "app.py", "original_line": 1},
        104: {"original_commit_id": remote["c2"], "path": "app.py", "original_line": 1},
        105: {"original_commit_id": remote["b0"], "path": "app.py", "original_line": 1},
    }


class FakeGh:
    """Stands in for `gh`: answers GET paths from a table, 404 for anything else."""

    def __init__(self, remote, base_sha=None):
        self.calls = []
        self.answers = {f"repos/{OWNER}/{NAME}/pulls/{PR}": {
            "title": "Average helper", "base": {"sha": base_sha or remote["b1"], "ref": "main"},
            "head": {"sha": remote["c2"], "ref": "feature"}}}
        for comment_id, comment in _comments(remote).items():
            self.answers[f"repos/{OWNER}/{NAME}/pulls/comments/{comment_id}"] = comment

    def __call__(self, command, **kwargs):
        assert command[:2] == ["gh", "api"] and len(command) == 3, command
        self.calls.append(command[2])
        if command[2] in self.answers:
            return subprocess.CompletedProcess(command, 0, json.dumps(self.answers[command[2]]), "")
        return subprocess.CompletedProcess(command, 1, "", "gh: Not Found (HTTP 404)")


def _reference(comment_id, line, severity="Major", label="addressed"):
    return {"github_comment_id": comment_id, "file": "app.py", "line": line, "severity": severity,
            "category": "bug", "label": label, "body": "Dividing by zero crashes on an empty list."}


def _write_dataset(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


@pytest.fixture
def dataset(tmp_path):
    return _write_dataset(tmp_path / "data" / "dev.jsonl", [{
        "repo": f"{OWNER}/{NAME}", "pr": PR, "url": f"https://example.invalid/{OWNER}/{NAME}/pull/{PR}",
        "reference": [_reference(101, 40), _reference(102, 5), _reference(103, 1),
                      _reference(104, 1, severity="Minor", label="noise")],
    }])


@pytest.fixture(autouse=True)
def harness_state(monkeypatch, tmp_path):
    FakeModel.calls = []
    monkeypatch.setattr(cli, "setup_logger", lambda *args, **kwargs: None)
    # git must not find a work tree above tmp_path, wherever the temporary folder lives.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    yield
    # The harness registers its provider on first use; keep the shared registry as other tests expect it.
    git_providers._GIT_PROVIDERS.pop(PROVIDER_ID, None)


@pytest.fixture
def no_network(monkeypatch):
    """Record and refuse every socket connection and every GitHub provider construction."""
    attempts = []

    def refuse(kind):
        def raiser(*args, **kwargs):
            attempts.append(kind)
            raise OSError(f"network access is not allowed in this test ({kind})")
        return raiser

    monkeypatch.setattr(socket.socket, "connect", refuse("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", refuse("connect_ex"))
    monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("getaddrinfo"))
    from pr_agent.git_providers.github_provider import GithubProvider
    monkeypatch.setattr(GithubProvider, "__init__", refuse("GithubProvider"))
    return attempts


def _run(dataset, remote, *extra, gh=None, model=FakeModel):
    gh = gh or FakeGh(remote)
    code = cli.main(["--dataset", str(dataset), *extra], gh_runner=gh,
                    remote_url=lambda owner, name: str(remote["path"]), model_handler=model)
    return code, gh


def _results(dataset, profile="baseline"):
    return json.loads((dataset.parent / "results" / profile / "results.json").read_text())


def test_review_and_improve_end_to_end_publish_nothing(dataset, remote, no_network, monkeypatch):
    monkeypatch.setenv("ANTHROPIC__KEY", "test-key-not-real")

    code, _ = _run(dataset, remote)

    assert code == 0
    assert no_network == []
    results = _results(dataset)
    units = {unit["commit"]: unit for unit in results["units"]}
    assert set(units) == {remote["c1"], remote["c2"]}
    unit = units[remote["c1"]]
    assert unit["published"] == []
    assert unit["commands"]["review"]["ok"] and unit["commands"]["improve"]["ok"]
    assert [(f["source"], f["file"], f["start_line"], f["end_line"], f["valid_range"]) for f in unit["findings"]] == [
        ("review", "app.py", 2, 3, True), ("review", "app.py", None, 1, False), ("improve", "app.py", 3, 3, True)]
    assert unit["findings"][0]["severity"] == "Major"
    assert "Division by zero" in unit["findings"][0]["text"]
    assert unit["findings"][2]["score"] == 8
    review_metrics = unit["commands"]["review"]["metrics"]
    assert review_metrics["live_calls"] == 1 and review_metrics["ai_calls"] == 1
    assert review_metrics["prompt_tokens"] == 1000 and review_metrics["usd"] == pytest.approx(0.0125)
    # The coderabbit output style reached the review prompt: the provider can publish one review.
    review_prompts = [call["system"] for call in FakeModel.calls if "$PRReview" in call["system"]]
    assert review_prompts and all("Exactly one of: 'Critical'" in system for system in review_prompts)
    reference = next(r for r in unit["references"] if r["comment_id"] == 101)
    assert (reference["start_line"], reference["end_line"], reference["caught"]) == (2, 3, True)
    assert "body" not in reference
    totals = results["scores"]["total"]
    assert totals["recall_addressed_critical_major"] == 1.0
    assert totals["units_skipped"] == 2 and totals["references_skipped"] == 2
    reasons = {tuple(item["reference_ids"]): item["reason"] for item in results["skipped"]}
    assert reasons[(102,)] == "review comment deleted"
    assert reasons[(103,)].startswith("commit 0123456789ab not fetchable")
    report = (dataset.parent / "results" / "baseline" / "report.md").read_text()
    assert "Recall, addressed Critical/Major | 1 |" in report


def test_replayed_diff_is_the_reviewed_commit_against_its_merge_base(dataset, remote, monkeypatch):
    monkeypatch.setenv("ANTHROPIC__KEY", "test-key-not-real")

    _run(dataset, remote)

    review_users = [call["user"] for call in FakeModel.calls if "$PRReview" in call["system"]]
    first = review_users[0]
    assert "total / count" in first and "other.py" not in first


def test_dry_run_makes_no_model_call_and_needs_no_key(dataset, remote, no_network, monkeypatch):
    monkeypatch.delenv("ANTHROPIC__KEY", raising=False)

    code, _ = _run(dataset, remote, "--dry-run", model=ForbiddenModel)

    assert code == 0
    assert FakeModel.calls == []
    assert no_network == []
    results = _results(dataset)
    assert results["run"]["dry_run"] is True
    totals = results["scores"]["total"]
    assert totals["units"] == 2 and totals["findings"] == 0
    assert totals["live_calls"] == 0 and totals["ai_calls"] == 0 and totals["usd"] == 0
    assert totals["dry_calls"] >= 4 and totals["estimated_prompt_tokens"] > 0
    assert results["judge"]["calls"] == 0
    assert all(command["ok"] for unit in results["units"] for command in unit["commands"].values())
    prompts = list((dataset.parent / "results" / "baseline" / "raw").glob("*/review.prompts.jsonl"))
    assert len(prompts) == 2


def test_real_run_without_key_fails_fast(dataset, remote, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC__KEY", raising=False)

    code, gh = _run(dataset, remote)

    assert code == 2
    assert gh.calls == [] and FakeModel.calls == []
    assert "ANTHROPIC__KEY is not set" in capsys.readouterr().err


def test_rerun_is_served_from_cache_with_equal_numbers(dataset, remote, monkeypatch):
    monkeypatch.setenv("ANTHROPIC__KEY", "test-key-not-real")
    _run(dataset, remote)
    first = _results(dataset)
    live_calls = len(FakeModel.calls)
    assert live_calls > 0

    FakeModel.calls = []
    _, gh = _run(dataset, remote)
    second = _results(dataset)

    assert FakeModel.calls == [] and gh.calls == []
    assert second["scores"]["total"]["live_calls"] == 0
    assert second["scores"]["total"]["cached_calls"] == first["scores"]["total"]["live_calls"]
    for scores in (first["scores"], second["scores"]):
        for bucket in (scores["total"], *scores["per_repo"].values()):
            for key in ("wall_seconds", "live_calls", "cached_calls"):
                bucket.pop(key)
    assert second["scores"] == first["scores"]
    assert [unit["findings"] for unit in second["units"]] == [unit["findings"] for unit in first["units"]]
    assert second["judge"]["usd"] == first["judge"]["usd"] and second["judge"]["live_calls"] == 0


def test_cache_key_ignores_the_prompt_date():
    first = cache_key("m", "system", "Today's Date: 2026-01-01\ndiff", 0.2)
    assert first == cache_key("m", "system", "Today's Date: 2026-12-31\ndiff", 0.2)
    assert first != cache_key("m", "system", "Today's Date: 2026-01-01\nother diff", 0.2)
    assert first != cache_key("m", "system", "Today's Date: 2026-01-01\ndiff", 0.0)


def test_units_use_the_pull_request_base_sha_not_the_live_branch_tip(tmp_path, remote):
    entry = load_dataset(_write_dataset(tmp_path / "d.jsonl", [{
        "repo": f"{OWNER}/{NAME}", "pr": PR, "reference": [_reference(101, 40), _reference(105, 1)]}]))[0]
    git = GitStore(tmp_path / "cache", remote_url=lambda owner, name: str(remote["path"]))

    units, skipped = build_units(entry, GhClient(tmp_path / "cache", runner=FakeGh(remote)), git)

    assert [(unit.commit, unit.base_sha, unit.merge_base) for unit in units] == [
        (remote["c1"], remote["b1"], remote["b0"])]
    assert [(item.reference_ids, item.reason) for item in skipped] == [([105], "empty diff")]
    assert units[0].references[0].start_line == 2 and units[0].references[0].end_line == 3
    # The merged branch tip already contains the commit: its diff would be empty.
    assert git.ensure_commits(OWNER, NAME, PR, [remote["tip"]])[1] == {}
    live_tip_base = git.merge_base(units[0].repo_path, remote["c1"], remote["tip"])
    assert live_tip_base == remote["c1"] and git.diff_is_empty(units[0].repo_path, live_tip_base, remote["c1"])

    target_id = "review-eval/test"
    register_target(target_id, ReplayTarget(repo_path=units[0].repo_path, head_sha=remote["c1"],
                                            diff_base_sha=units[0].merge_base, context_sha=remote["b1"]))
    try:
        provider = ReplayGitProvider(target_id)
        files = provider.get_diff_files()
        assert [file.filename for file in files] == ["app.py"]
        assert "return total / count" in files[0].head_file and "return 0" in files[0].base_file
        assert provider.get_repo_file_content("other.py") == "A = 2\n"
        assert "compute the average" in provider.get_pr_description_full()
    finally:
        unregister_target(target_id)


def test_unreadable_pull_request_skips_every_reference(tmp_path, remote):
    entry = load_dataset(_write_dataset(tmp_path / "d.jsonl", [{
        "repo": f"{OWNER}/{NAME}", "pr": PR, "reference": [_reference(101, 1)]}]))[0]
    gh = FakeGh(remote)
    gh.answers.clear()

    units, skipped = build_units(entry, GhClient(tmp_path / "cache", runner=gh), GitStore(tmp_path / "cache"))

    assert units == []
    assert skipped[0].reference_ids == [101] and skipped[0].reason.startswith("pull request not readable")


def test_only_and_limit_select_pull_requests(tmp_path, remote, monkeypatch):
    monkeypatch.setenv("ANTHROPIC__KEY", "test-key-not-real")
    data = _write_dataset(tmp_path / "data" / "dev.jsonl", [
        {"repo": f"{OWNER}/{NAME}", "pr": PR, "reference": [_reference(101, 3)]},
        {"repo": f"{OWNER}/{NAME}", "pr": PR + 1, "reference": [_reference(201, 3)]},
        {"repo": f"{OWNER}/gadgets", "pr": 1, "reference": [_reference(301, 3)]},
    ])
    entries = load_dataset(data)
    assert [e.key for e in select_entries(entries, limit=2)] == [f"{OWNER}/{NAME}#{PR}", f"{OWNER}/{NAME}#{PR + 1}"]
    assert [e.key for e in select_entries(entries, only=["gadgets#1"])] == [f"{OWNER}/gadgets#1"]
    assert [e.number for e in select_entries(entries, only=[f"{OWNER}/{NAME}#{PR + 1}", "gadgets#1"], limit=1)] == [
        PR + 1]
    with pytest.raises(ValueError, match="matches no dataset entry"):
        select_entries(entries, only=[f"{NAME}#999"])

    code, gh = _run(data, remote, "--only", f"{NAME}#{PR}")
    assert code == 0
    assert {call.split("/pulls/")[1] for call in gh.calls} == {str(PR), "comments/101"}
    assert [unit["pr"] for unit in _results(data)["units"]] == [PR]

    code, gh = _run(data, remote, "--limit", "0")
    assert code == 0 and gh.calls == [] and _results(data)["units"] == []

    assert _run(data, remote, "--only", "nothing#1")[0] == 2


def test_output_folders_inside_a_work_tree_must_be_git_ignored(tmp_path, dataset, remote, monkeypatch):
    work_tree = tmp_path / "checkout"
    work_tree.mkdir()
    _git(work_tree, "init", "-q")
    (work_tree / ".gitignore").write_text(".eval-data/\n")

    assert cli.check_output_dir(work_tree / ".eval-data" / "results" / "baseline") is None
    assert "git does not ignore it" in cli.check_output_dir(work_tree / "results")
    assert "git does not ignore it" in cli.check_output_dir(work_tree)
    assert cli.check_output_dir(tmp_path / "outside") is None

    monkeypatch.setenv("ANTHROPIC__KEY", "test-key-not-real")
    code, gh = _run(dataset, remote, "--out", str(work_tree / "results"))
    assert code == 2 and gh.calls == []
    assert not Path(work_tree / "results").exists()
