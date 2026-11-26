"""Per-repository extra_instructions: the generator and the harness hook, on synthetic datasets."""

import subprocess
import tomllib
from pathlib import Path

from tests.unittest import test_review_eval_harness as harness
from tools.review_eval import repo_instructions
from tools.review_eval.dataset import parse_entry

PROFILES_DIR = Path(__file__).resolve().parents[2] / "tools" / "review_eval" / "profiles"
BODY = "The cache key leaks the session token."
# The harness test fixtures: a hosted repository stand-in and the shared harness state (autouse).
remote = harness.remote
harness_state = harness.harness_state


def _ref(comment_id, category, severity, label="addressed"):
    return {"github_comment_id": comment_id, "file": "src/private_module.py", "line": 3, "severity": severity,
            "category": category, "label": label, "body": BODY}


def _generate(tmp_path, entries):
    dataset = harness._write_dataset(tmp_path / "data" / "dev.jsonl", entries)
    out = tmp_path / "data" / "repo_instructions.toml"
    return repo_instructions.main(["--dataset", str(dataset), "--out", str(out)]), dataset, out


def _harness_entry():
    return {"repo": f"{harness.OWNER}/{harness.NAME}", "pr": harness.PR, "reference": [harness._reference(101, 40)]}


def test_instructions_count_only_the_addressed_categories_and_severities(tmp_path):
    code, _, out = _generate(tmp_path, [
        {"repo": "example-org/widgets", "pr": 1, "reference": [
            _ref(1, "Potential_Issue", "Major"), _ref(2, "potential issue", "Critical"), _ref(3, "security", "Major"),
            _ref(4, "refactor suggestion", "Minor", label="noise"), _ref(5, "security", "Minor", label="pending")]},
        {"repo": "example-org/widgets", "pr": 2, "reference": [_ref(6, "bug", "minor")]},
        {"repo": "example-org/gadgets", "pr": 3, "reference": [_ref(7, "nitpick", "Trivial", label="noise")]},
    ])

    assert code == 0
    text = out.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert list(data) == ["widgets"]
    assert data["widgets"]["extra_instructions"] == (
        "In earlier reviews of this repository, developers fixed 4 reported findings. "
        "The kinds fixed most often: potential issue (2), bug (1), security (1). "
        "By severity: 1 Critical, 2 Major, 1 Minor. "
        "Look for these kinds of defect first, and report each one with its file and line.")
    for private in (BODY, "private_module", "example-org", "refactor", "nitpick"):
        assert private not in text
    assert repo_instructions.load_repo_instructions(out) == {"widgets": data["widgets"]["extra_instructions"]}


def test_output_inside_a_work_tree_must_be_git_ignored(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    dataset = harness._write_dataset(tmp_path / "data" / "dev.jsonl", [_harness_entry()])
    out = work / "repo_instructions.toml"

    assert repo_instructions.main(["--dataset", str(dataset), "--out", str(out)]) == 2
    assert not out.exists()


def test_repository_text_follows_the_profile_instructions():
    entry = parse_entry({"repo": "example-org/widgets", "pr": 1})

    settings = repo_instructions.unit_settings(
        {"widgets": "Repository text."}, entry, {"pr_reviewer.extra_instructions": "Profile text."})

    assert settings == {"pr_reviewer.extra_instructions": "Profile text.\n\nRepository text.",
                        "pr_code_suggestions.extra_instructions": "Repository text."}
    assert repo_instructions.unit_settings({"gadgets": "Other text."}, entry, {}) == {}


def test_harness_adds_the_repository_text_to_review_and_improve(tmp_path, remote, monkeypatch):
    monkeypatch.setenv("ANTHROPIC__KEY", "test-key-not-real")
    code, dataset, instructions = _generate(tmp_path, [_harness_entry()])
    assert code == 0
    text = repo_instructions.load_repo_instructions(instructions)[harness.NAME]

    code, _ = harness._run(dataset, remote, "--profile", str(PROFILES_DIR / "repo_instructions.toml"),
                           "--repo-instructions", str(instructions))

    assert code == 0
    review = [call for call in harness.FakeModel.calls if "$PRReview" in call["system"]]
    improve = [call for call in harness.FakeModel.calls if "class CodeSuggestion" in call["system"]]
    assert review and improve
    assert all(text in call["system"] + call["user"] for call in review + improve)
    results = harness._results(dataset, "repo_instructions")
    assert results["run"]["repo_instructions"] == "repo_instructions.toml"
    assert [unit["repo_instructions"] for unit in results["units"]] == [True]


def test_instructions_need_the_profile_option_and_the_file(tmp_path, remote, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC__KEY", "test-key-not-real")
    _, dataset, instructions = _generate(tmp_path, [_harness_entry()])
    capsys.readouterr()

    code, gh = harness._run(dataset, remote, "--profile", str(PROFILES_DIR / "repo_instructions.toml"))
    assert code == 2 and gh.calls == [] and harness.FakeModel.calls == []
    assert "pass --repo-instructions PATH" in capsys.readouterr().err

    code, _ = harness._run(dataset, remote, "--repo-instructions", str(instructions))
    assert code == 0
    assert "--repo-instructions ignored" in capsys.readouterr().err
    assert harness.FakeModel.calls
    assert not any("developers fixed" in call["system"] + call["user"] for call in harness.FakeModel.calls)
    results = harness._results(dataset)
    assert results["run"]["repo_instructions"] is None
    assert [unit["repo_instructions"] for unit in results["units"]] == [False]
