"""Scorer, profile and finding normalisation of the review-eval harness (no git, no model)."""

import pytest

from tools.review_eval.cli import DEFAULT_PROFILE
from tools.review_eval.profile import load_profile
from tools.review_eval.replay import improve_findings, make_finding, review_findings
from tools.review_eval.report import render_report
from tools.review_eval.scorer import (
    aggregate,
    code_excerpt,
    judge_precision,
    location_match,
    match_unit,
    parse_verdict,
    sample_findings,
)


def _ref(comment_id, start, end, file="src/app.py", severity="Major", label="addressed"):
    return {"comment_id": comment_id, "file": file, "start_line": start, "end_line": end, "severity": severity,
            "category": "bug", "label": label, "body": f"reference {comment_id}"}


def _metrics(tokens=100, usd=0.5, latency=2.0):
    return {"prompt_tokens": tokens, "completion_tokens": 10, "total_tokens": tokens + 10, "ai_calls": 1,
            "live_calls": 1, "cached_calls": 0, "dry_calls": 0, "estimated_prompt_tokens": 0, "usd": usd,
            "latency_seconds": latency, "wall_seconds": latency + 1}


def _unit(key, repo, findings, references, ok=True):
    return {"key": key, "pr_key": key.split("@")[0], "repo": repo, "findings": findings, "references": references,
            "commands": {"review": {"ok": ok, "error": None if ok else "command failed", "metrics": _metrics()},
                         "improve": {"ok": True, "error": None, "metrics": _metrics()}}}


class FakeJudge:
    def __init__(self, same=True):
        self.same = same
        self.asked = []

    async def same_issue(self, reference, finding):
        self.asked.append((reference["comment_id"], finding["text"]))
        return self.same if not callable(self.same) else self.same(reference, finding)

    async def is_valid(self, finding, code_context):
        return "valid" in finding["text"]


@pytest.mark.parametrize("finding,reference,expected", [
    (make_finding("review", "src/app.py", 30, 32, "t"), _ref(1, 40, 42), True),  # 10 lines before
    (make_finding("review", "src/app.py", 29, 29, "t"), _ref(1, 40, 42), False),  # 11 lines before
    (make_finding("review", "./src/app.py", 52, 60, "t"), _ref(1, 40, 42), True),  # 10 lines after
    (make_finding("review", "src/app.py", 53, 60, "t"), _ref(1, 40, 42), False),
    (make_finding("review", "src/app.py", 1, 200, "t"), _ref(1, 40, 42), True),  # finding spans reference
    (make_finding("review", "src/other.py", 40, 42, "t"), _ref(1, 40, 42), False),
    (make_finding("review", "src/app.py", None, 41, "t"), _ref(1, 40, 42), False),  # no valid range
    (make_finding("review", "src/app.py", 45, 41, "t"), _ref(1, 40, 42), False),  # inverted range
    (make_finding("review", "src/app.py", 400, 401, "t"), _ref(1, None, None), True),  # file-level reference
    (make_finding("review", "src/app.py", 40, 40, "t"), _ref(1, None, 40), True),
])
def test_location_match_uses_a_ten_line_window(finding, reference, expected):
    assert location_match(finding, reference) is expected


def test_finding_without_a_valid_range_is_kept_but_never_matches():
    findings = review_findings({"review": {"key_issues_to_review": [
        {"relevant_file": "src/app.py\n", "issue_header": "Bug", "issue_content": "x", "start_line": "n/a",
         "end_line": 3, "severity": "Major"},
        {"relevant_file": "src/app.py", "issue_header": "Bug", "issue_content": "y", "start_line": 4},
    ]}})
    assert [(f["file"], f["start_line"], f["end_line"], f["valid_range"]) for f in findings] == [
        ("src/app.py", None, 3, False), ("src/app.py", 4, 4, True)]
    assert not location_match(findings[0], _ref(1, 3, 3))
    suggestions = improve_findings({"code_suggestions": [
        {"relevant_file": "a.py", "relevant_lines_start": 2, "relevant_lines_end": 5, "score": "7",
         "one_sentence_summary": "Fix", "suggestion_content": "Do it"}, "not a dict"]})
    assert suggestions == [{"source": "improve", "file": "a.py", "start_line": 2, "end_line": 5, "valid_range": True,
                            "text": "Fix\nDo it", "severity": None, "score": 7}]


async def test_match_unit_asks_the_judge_only_for_location_candidates():
    judge = FakeJudge(same=lambda reference, finding: "same" in finding["text"])
    unit = _unit("o/r#1@abc", "o/r", [
        make_finding("review", "src/app.py", 41, 41, "same problem"),
        make_finding("improve", "src/app.py", 44, 44, "different problem"),
        make_finding("review", "src/app.py", 300, 301, "same problem, far away"),
    ], [_ref(1, 40, 42), _ref(2, 500, 500, label="noise")])

    await match_unit(unit, judge)

    assert judge.asked == [(1, "same problem"), (1, "different problem")]
    assert unit["references"][0]["caught"] is True and unit["references"][0]["matched_findings"] == [0]
    assert unit["references"][1]["caught"] is False
    assert [f["matched_references"] for f in unit["findings"]] == [[1], [], []]


async def test_without_a_judge_nothing_matches():
    unit = _unit("o/r#1@abc", "o/r", [make_finding("review", "src/app.py", 41, 41, "same")], [_ref(1, 40, 42)])
    await match_unit(unit, None)
    assert unit["references"][0]["candidates"] == 1 and unit["references"][0]["caught"] is False


async def test_aggregate_reports_recall_share_precision_and_cost_per_repo():
    units = [
        _unit("o/a#1@c1", "o/a", [make_finding("review", "x.py", 10, 10, "same valid"),
                                  make_finding("improve", "x.py", 90, 90, "other")],
              [_ref(1, 10, 10, file="x.py"), _ref(2, 50, 50, file="x.py", severity="Minor"),
               _ref(3, 90, 90, file="x.py", label="noise")]),
        _unit("o/a#1@c2", "o/a", [], [_ref(4, 5, 5, file="x.py", severity="Critical")], ok=False),
        _unit("o/b#2@c3", "o/b", [make_finding("review", "y.py", 1, 1, "valid too")],
              [_ref(5, 1, 1, file="y.py", severity="Minor")]),
    ]
    judge = FakeJudge(same=lambda reference, finding: reference["comment_id"] in {1, 5})
    for unit in units:
        await match_unit(unit, judge)
    precision = await judge_precision(units, judge, size=10, seed=3, read_context=lambda unit, finding: "")
    skipped = [{"repo": "o/a", "pr_key": "o/a#1", "commit": None, "reference_ids": [6, 7], "reason": "deleted"}]

    scores = aggregate(units, skipped, precision)

    total, repo_a, repo_b = scores["total"], scores["per_repo"]["o/a"], scores["per_repo"]["o/b"]
    assert (total["prs"], total["units"], total["units_skipped"], total["references_skipped"]) == (2, 3, 1, 2)
    assert (total["addressed"], total["addressed_caught"], total["recall_addressed"]) == (4, 2, 0.5)
    assert (total["addressed_critical_major"], total["addressed_critical_major_caught"]) == (2, 1)
    assert total["recall_addressed_critical_major"] == 0.5
    assert repo_a["recall_addressed_critical_major"] == 0.5 and repo_b["recall_addressed_critical_major"] is None
    assert repo_b["recall_addressed"] == 1.0
    assert (total["findings"], total["findings_per_pr"], total["findings_per_unit"]) == (3, 1.5, 1.0)
    assert total["findings_matching"] == 2 and total["share_findings_matching_reference"] == 0.6667
    assert (total["precision_judged"], total["precision_valid"], total["precision_sampled"]) == (3, 2, 0.6667)
    assert total["usd"] == 3.0 and total["usd_per_pr"] == 1.5 and total["tokens_per_pr"] == 330
    assert total["latency_seconds_per_unit"] == 4.0 and total["commands_failed"] == 1
    assert repo_a["usd_per_pr"] == 2.0 and repo_a["units_skipped"] == 1 and repo_b["units_skipped"] == 0

    report = render_report({"run": {"profile": "baseline", "profile_description": "", "dataset": "dev.jsonl",
                                    "commands": ["review", "improve"], "judge_model": "m", "line_window": 10,
                                    "precision_sample": 10, "seed": 3, "dry_run": False},
                            "scores": scores, "units": units, "skipped": skipped, "judge": {}})
    assert "| Recall, addressed Critical/Major | 0.5 |" in report
    assert "| o/b | 1 | 1 | 0 | n/a | 1 |" in report
    assert "| o/a#1 | - | 2 | deleted |" in report and "| o/a#1@c2 | review | command failed |" in report


def test_precision_sample_is_seeded():
    units = [{"findings": [{}] * 5}, {"findings": [{}] * 5}]
    first = sample_findings(units, 4, seed=7)
    assert first == sample_findings(units, 4, seed=7) and len(first) == 4
    assert sample_findings(units, 4, seed=8) != first or sample_findings(units, 4, seed=9) != first
    assert len(sample_findings(units, 50, seed=1)) == 10 and sample_findings(units, 0, seed=1) == []


@pytest.mark.parametrize("answer,expected", [
    ('{"same_issue": true, "reason": "x"}', True),
    ('```json\n{"same_issue": false}\n```', False),
    ("Yes, both describe it.", True),
    ("no", False),
    ("I cannot tell", None),
    (None, None),
])
def test_parse_verdict(answer, expected):
    assert parse_verdict(answer, "same_issue") is expected


def test_code_excerpt_numbers_lines_around_the_finding():
    content = "\n".join(f"line {number}" for number in range(1, 101))
    excerpt = code_excerpt(content, 50, 51, around=2)
    assert excerpt.splitlines() == ["48 line 48", "49 line 49", "50 line 50", "51 line 51", "52 line 52",
                                    "53 line 53"]
    assert code_excerpt(None, 1, 1) == "(file not available)"


def test_profile_tables_become_settings_and_review_eval_is_reserved(tmp_path):
    path = tmp_path / "candidate.toml"
    path.write_text('[review_eval]\ndescription = "d"\ncommands = ["review"]\n\n'
                    '[pr_reviewer]\nnum_max_findings = 6\n\n[config]\nmade_up_key = 1\n')
    profile = load_profile(path)
    assert (profile.name, profile.description, profile.commands) == ("candidate", "d", ("review",))
    assert profile.settings == {"pr_reviewer.num_max_findings": 6, "config.made_up_key": 1}
    assert profile.warnings == ["config.made_up_key is not a default PR-Agent setting; it is applied as given"]

    path.write_text('[config]\npublish_output = true\n')
    with pytest.raises(ValueError, match="set by the harness"):
        load_profile(path)
    path.write_text('[review_eval]\ncommands = ["describe"]\n')
    with pytest.raises(ValueError, match="commands"):
        load_profile(path)
    path.write_text('model = "x"\n')
    with pytest.raises(ValueError, match="must be a \\[section\\] table"):
        load_profile(path)


def test_baseline_profile():
    profile = load_profile(DEFAULT_PROFILE)
    assert profile.settings["config.model"] == "anthropic/claude-sonnet-5-5"
    assert profile.settings["config.output_style"] == "coderabbit"
    assert profile.settings["config.fallback_models"]
    assert all(model.startswith("anthropic/") for model in profile.settings["config.fallback_models"])
    assert profile.commands == ("review", "improve") and profile.warnings == []
