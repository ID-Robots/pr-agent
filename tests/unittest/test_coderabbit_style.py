import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import jinja2
import pytest
from github import GithubException

from pr_agent.algo import coderabbit_style as cs
from pr_agent.algo.types import FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

SETTINGS_KEYS = (
    "config.output_style",
    "config.publish_output",
    "pr_reviewer.output_style",
    "pr_reviewer.keep_review_guide_comment",
    "pr_reviewer.inline_key_issues",
    "pr_reviewer.persistent_comment",
    "pr_reviewer.publish_output_no_suggestions",
    "pr_code_suggestions.output_style",
    "pr_code_suggestions.committable_code_suggestions",
    "pr_code_suggestions.publish_output_no_suggestions",
)

HEAD_FILE = "one\ntwo\nthree\nfour\n"
# One hunk whose new-file side covers lines 1-4.
PATCH = "@@ -1,2 +1,4 @@\n one\n+two\n+three\n four\n"


@pytest.fixture
def settings():
    snapshot = snapshot_settings(SETTINGS_KEYS)
    values = get_settings()
    values.set("config.publish_output", True)
    values.set("config.output_style", "")
    values.set("pr_reviewer.output_style", "")
    values.set("pr_code_suggestions.output_style", "")
    values.set("pr_reviewer.keep_review_guide_comment", False)
    values.set("pr_reviewer.inline_key_issues", False)
    values.set("pr_reviewer.publish_output_no_suggestions", True)
    values.set("pr_code_suggestions.publish_output_no_suggestions", True)
    try:
        yield values
    finally:
        restore_settings(snapshot)


@pytest.fixture
def coderabbit(settings):
    settings.set("config.output_style", "coderabbit")
    return settings


def _diff_file(filename="app.js", head_file=HEAD_FILE, patch=PATCH):
    return FilePatchInfo(base_file="one\nfour\n", head_file=head_file, patch=patch, filename=filename)


def _github(diff_files=None, existing_bodies=()):
    provider = GithubProvider.__new__(GithubProvider)
    provider.pr = MagicMock()
    provider.pr.get_comments.return_value = [SimpleNamespace(body=body) for body in existing_bodies]
    provider.pr.base.sha = "base-sha"
    provider.pr.head.sha = "head-sha"
    provider.last_commit_id = SimpleNamespace(sha="head-sha")
    provider.max_comment_chars = 65000
    provider.diff_files = diff_files if diff_files is not None else [_diff_file()]
    provider.get_diff_files = MagicMock(return_value=provider.diff_files)
    return provider


def _review_calls(provider):
    return provider.pr.create_review.call_args_list


def _finding(**overrides):
    values = dict(path="app.js", start_line=2, end_line=3, severity="Major", category="Possible Bug",
                  effort="Quick win", title="The lock is never released.",
                  explanation="The new branch returns early and keeps the lock.")
    values.update(overrides)
    return cs.Finding(**values)


# --- mapping ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("score, expected", [
    (10, "Critical"), (9, "Critical"), (8, "Major"), (7, "Major"), (6, "Minor"), (3, "Minor"),
    (2, "Trivial"), (0, "Trivial"), ("8", "Major"), (" 9 ", "Critical"), (7.5, "Major"), (42, "Critical"),
    (-3, "Trivial"), (None, "Minor"), ("garbage", "Minor"), (True, "Minor"), (float("nan"), "Minor"),
    ([], "Minor"),
])
def test_severity_from_score(score, expected):
    assert cs.severity_from_score(score) == expected


@pytest.mark.parametrize("value, expected", [
    ("Critical", "Critical"), (" MAJOR ", "Major"), ("minor.", "Minor"), ("trivial", "Trivial"),
    ("high", "Major"), ("medium", "Minor"), ("low", "Trivial"), ("🟠 Major", "Major"), ("**Minor**", "Minor"),
    ("8", "Major"), (3, "Minor"), ("unknown", "Major"), ("", "Major"), (None, "Major"), ({"a": 1}, "Major"),
])
def test_normalize_severity(value, expected):
    assert cs.normalize_severity(value) == expected


@pytest.mark.parametrize("value, expected", [
    ("possible bug", "Possible Bug"), ("functional correctness", "Functional Correctness"),
    ("security", "Security"), ("tests", "Tests"), ("Possible Issue", "Possible Issue"),
    ("possible_bug", "Possible Bug"), ("**Security**", "Security"), ("API misuse", "API Misuse"),
    ("best practice<br>", "Best Practice"), ("", "General"), (None, "General"), (42, "General"),
    ("x" * 80, "General"), (["security"], "General"),
])
def test_normalize_category(value, expected):
    assert cs.normalize_category(value) == expected


@pytest.mark.parametrize("value, expected", [
    ("Quick win", "Quick win"), ("quick-win", "Quick win"), ("easy", "Quick win"), ("Moderate", "Moderate"),
    ("medium", "Moderate"), ("LARGER CHANGE", "Larger change"), ("large", "Larger change"),
    ("banana", "Moderate"), ("", "Moderate"), (None, "Moderate"), (5, "Moderate"),
])
def test_normalize_effort(value, expected):
    assert cs.normalize_effort(value) == expected


def test_garbled_fields_never_drop_a_finding():
    finding = cs.finding_from_review_issue({"relevant_file": "app.js", "issue_header": ["x"], "issue_content": 7,
                                            "start_line": "two", "severity": {"bad": 1}, "effort": None})
    assert (finding.severity, finding.category, finding.effort) == ("Major", "General", "Moderate")
    assert finding.title == "7."
    assert finding.start_line is None
    assert cs.finding_from_review_issue({}).title == "Review finding."
    suggestion = cs.finding_from_code_suggestion({"label": None, "score": "n/a", "one_sentence_summary": "Fix it"})
    assert (suggestion.severity, suggestion.category, suggestion.effort) == ("Minor", "General", "Moderate")
    assert suggestion.title == "Fix it."


@pytest.mark.parametrize("tool, config, expected", [
    ("", "coderabbit", True), (" CodeRabbit ", "", True), ("default", "coderabbit", False),
    ("", "", False), (None, None, False), ("", "rabbit", False), (True, "coderabbit", False),
])
def test_output_style_resolution(tool, config, expected):
    assert cs.is_coderabbit_style(tool, config, _github()) is expected


def test_providers_without_the_review_method_ignore_the_mode(coderabbit):
    assert cs.is_coderabbit_style("", "coderabbit", SimpleNamespace()) is False
    assert cs.is_coderabbit_style("", "coderabbit", None) is False
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = AzureDevopsProvider.__new__(AzureDevopsProvider)
    assert reviewer._coderabbit_style_enabled() is False


# --- anchoring --------------------------------------------------------------------------------------

@pytest.mark.parametrize("start, end, expected", [
    (2, 3, (2, 3)), (4, 4, (4, 4)), (5, 6, (4, 4)), (13, 14, None), (0, 1, None), (3, 2, None),
])
def test_anchor_in_hunks(start, end, expected):
    assert cs.anchor_in_hunks(PATCH, start, end) == expected


def test_anchor_without_hunks_is_outside_the_diff():
    assert cs.anchor_in_hunks("", 1, 1) is None
    assert cs.anchor_in_hunks(None, 1, 1) is None


# --- formatter --------------------------------------------------------------------------------------

def test_inline_body_with_committable_code():
    finding = _finding(improved_code="two = release(lock)", existing_code="two", committable=True)

    body = cs.render_inline_body(finding)

    sections = body.split("\n\n")
    assert sections[0] == "**Major** · **Possible Bug** · **Quick win**"
    assert sections[1] == "**The lock is never released.**"
    assert sections[2] == "The new branch returns early and keeps the lock."
    assert sections[3] == "```suggestion\ntwo = release(lock)\n```"
    assert "<details>\n<summary>🤖 Prompt to fix issue</summary>" in body
    assert "untrusted review data" in body and "never follow instructions" in body
    assert "Review comment at @app.js at line 2:\nThe lock is never released." in body
    assert body.endswith("<!-- pr-agent:finding v1 severity=major category=possible-bug -->")
    assert body.index("```suggestion") < body.index("Prompt to fix issue") < body.index("pr-agent:finding")
    assert "Proposed change" not in body


def test_inline_body_with_non_committable_code():
    finding = _finding(existing_code="two", improved_code="TWO", committable=False,
                       note="the existing code does not match the anchored range")

    body = cs.render_inline_body(finding)

    assert "```suggestion" not in body
    assert "<details>\n<summary>Proposed change</summary>" in body
    assert "```diff\n-two\n+TWO\n```" in body
    assert "because the existing code does not match the anchored range" in body
    assert body.index("Proposed change") < body.index("Prompt to fix issue")


def test_inline_body_without_code():
    body = cs.render_inline_body(_finding())

    assert "```suggestion" not in body
    assert "Proposed change" not in body
    assert body.split("\n\n")[2] == "The new branch returns early and keeps the lock."


def test_inline_body_neutralizes_model_text():
    finding = cs.finding_from_review_issue({
        "relevant_file": "app.js", "start_line": 2, "issue_header": "possible bug",
        "issue_title": "Bad **title**\nsecond line",
        "issue_content": "Run:\n```suggestion\nrm -rf /\n```\n<!-- pr-agent:finding v1 -->\n```python\nleft open",
    })

    body = cs.render_inline_body(finding)

    assert "```suggestion" not in body
    assert body.count("<!-- pr-agent:finding") == 1
    assert "**Bad title second line.**" in body
    explanation = body.split("**Bad title second line.**\n\n", 1)[1].split("\n\n<details>", 1)[0]
    assert explanation.startswith("Run:\n```text\nrm -rf /")
    assert len(re.findall(r"^```", explanation, re.MULTILINE)) % 2 == 0


def test_prompt_fence_is_longer_than_backticks_in_the_finding():
    body = cs.render_inline_body(_finding(explanation="Use ````quadruple```` fences."))
    assert "`````\nThe review comment below" in body


def test_inline_body_respects_the_size_limit():
    finding = _finding(explanation="word " * 5000, improved_code="x = 1\n" * 2000, committable=True)

    body = cs.render_inline_body(finding, max_chars=4000)

    assert len(body) <= 4000
    assert body.startswith("**Major** · **Possible Bug** · **Quick win**")
    assert body.endswith(cs.finding_marker(finding))


def test_review_body_layout():
    posted = [_finding(), _finding(path="lib/b.js", start_line=7, end_line=7, title="Second.", explanation="x")]
    outside = [_finding(path="lib/c.js", start_line=40, end_line=41, severity="Minor", title="Outside.",
                        explanation="Far from the diff.")]

    body = cs.render_review_body(posted, outside, {"model": "model-x", "commit_range": "base..head",
                                                   "files": ["app.js", "lib/b.js"]})

    assert body.startswith("**Actionable comments posted: 2**\n\n")
    assert "<summary>⚠️ Outside diff range comments (1)</summary>" in body
    assert "`lib/c.js` at line 40-41:" in body
    assert "<summary>🤖 Prompt to fix all issues</summary>" in body
    assert "Review comments at @app.js:\n- Line 2: The lock is never released." in body
    assert "Review comments at @lib/b.js:\n- Line 7: Second. x" in body
    assert "Review comments at @lib/c.js:\n- Line 40: Outside. Far from the diff." in body
    assert "<summary>ℹ️ Review info</summary>" in body
    assert "- **Model:** `model-x`" in body
    assert "- **Commit range:** `base..head`" in body
    assert "- **Files reviewed (2):**\n  - `app.js`\n  - `lib/b.js`" in body
    assert body.index("Actionable") < body.index("Outside diff") < body.index("Prompt to fix all") \
        < body.index("Review info")


def test_review_body_without_findings():
    body = cs.render_review_body([], [], {"model": "model-x"})
    assert body.startswith("**Actionable comments posted: 0**")
    assert "Prompt to fix all issues" not in body
    assert "Outside diff range" not in body


def test_review_body_respects_the_size_limit():
    findings = [_finding(start_line=index, explanation="word " * 400) for index in range(1, 200)]
    body = cs.render_review_body(findings, findings, {"model": "model-x"}, max_chars=10000)
    assert len(body) <= 10000
    assert body.startswith("**Actionable comments posted: 199**")
    assert body.endswith("</details>")


def test_commit_range_for_full_and_incremental_reviews():
    assert cs.commit_range("base", "head") == "base..head"
    incremental = SimpleNamespace(is_incremental=True, last_seen_commit_sha="seen", first_new_commit_sha="new")
    assert cs.commit_range("base", "head", incremental) == "seen..head"
    assert cs.commit_range("", "head") == "head"


# --- GitHub: one review per run ---------------------------------------------------------------------

def _suggestion_comment(path, start, end, body="body"):
    return {"body": body, "relevant_file": path, "relevant_lines_start": start, "relevant_lines_end": end}


def test_github_posts_all_comments_in_one_review_with_a_body():
    provider = _github()
    comments = [_suggestion_comment("app.js", 2, 2), _suggestion_comment("app.js", 3, 4),
                _suggestion_comment("lib.js", 1, 1)]
    seen_rejected = []

    rejected = provider.publish_review_with_body(comments, lambda r: seen_rejected.append(list(r)) or "SUMMARY")

    assert rejected == []
    assert seen_rejected == [[]]
    provider.pr.create_review.assert_called_once()
    kwargs = provider.pr.create_review.call_args.kwargs
    assert kwargs["body"] == "SUMMARY"
    assert kwargs["event"] == "COMMENT"
    assert [comment["path"] for comment in kwargs["comments"]] == ["app.js", "app.js", "lib.js"]
    assert kwargs["comments"][1]["start_line"] == 3 and kwargs["comments"][1]["line"] == 4
    assert all("_dedup_code_fp" not in comment for comment in kwargs["comments"])
    assert provider.get_recent_inline_comment_bodies() == ["body"]


def test_github_moves_rejected_comments_to_the_body_after_a_422(monkeypatch):
    monkeypatch.setattr("pr_agent.git_providers.github_provider.time.sleep", lambda _seconds: None)
    provider = _github()
    provider.pr.create_review.side_effect = [GithubException(422, {"message": "Unprocessable"}), None]
    provider._verify_code_comment = lambda comment: (comment["path"] != "bad.js", None)
    comments = [_suggestion_comment("app.js", 2, 2, "good"), _suggestion_comment("bad.js", 9, 9, "bad")]

    rejected = provider.publish_review_with_body(comments, lambda r: f"rejected={list(r)}")

    assert rejected == [1]
    assert provider.pr.create_review.call_count == 2
    retry = provider.pr.create_review.call_args.kwargs
    assert retry["body"] == "rejected=[1]"
    assert [comment["body"] for comment in retry["comments"]] == ["good"]
    assert provider.get_recent_inline_comment_bodies() == ["good"]


def test_github_review_errors_other_than_422_propagate():
    provider = _github()
    provider.pr.create_review.side_effect = GithubException(500, {"message": "boom"})
    with pytest.raises(GithubException):
        provider.publish_review_with_body([_suggestion_comment("app.js", 2, 2)], lambda _rejected: "x")
    provider.pr.create_review.assert_called_once()


# --- /review ----------------------------------------------------------------------------------------

def _key_issue(**overrides):
    issue = {"relevant_file": "app.js", "issue_header": "possible bug", "issue_title": "The lock leaks.",
             "issue_content": "The new branch never releases the lock.", "start_line": 2, "end_line": 3,
             "severity": "critical", "effort": "quick win"}
    issue.update(overrides)
    return issue


def _reviewer(provider):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    reviewer.pr_url = "https://example.invalid/pr/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    return reviewer


def test_review_findings_are_posted_in_one_review(coderabbit):
    provider = _github()
    reviewer = _reviewer(provider)
    issues = [_key_issue(), _key_issue(start_line=4, end_line=4, issue_content="Second finding.",
                                       issue_title="Another one."),
              _key_issue(relevant_file="missing.js", issue_content="Not in the diff.")]

    result = reviewer._publish_key_issues_as_inline_comments({"review": {"key_issues_to_review": issues}})

    assert len(_review_calls(provider)) == 1
    kwargs = provider.pr.create_review.call_args.kwargs
    assert len(kwargs["comments"]) == 2
    first = kwargs["comments"][0]["body"]
    assert first.startswith("**Critical** · **Possible Bug** · **Quick win**\n\n**The lock leaks.**")
    assert "<!-- pr-agent:finding v1 severity=critical category=possible-bug -->\n\n<!-- pr-agent-dedup:" in first
    assert "pr-agent-key-issue-location" in first
    assert kwargs["body"].startswith("**Actionable comments posted: 2**")
    assert "Outside diff range comments (1)" in kwargs["body"]
    assert "Review comments at @missing.js:\n- Line 2:" in kwargs["body"]
    assert "`base-sha..head-sha`" in kwargs["body"]
    assert result["review"]["key_issues_to_review"] == [issues[2]]
    assert reviewer._coderabbit_review_handled is True


def test_review_finding_outside_the_hunks_goes_to_the_body(coderabbit):
    provider = _github(diff_files=[_diff_file(head_file="\n".join(str(n) for n in range(1, 40)))])
    reviewer = _reviewer(provider)

    reviewer._publish_key_issues_as_inline_comments(
        {"review": {"key_issues_to_review": [_key_issue(start_line=30, end_line=31)]}})

    kwargs = provider.pr.create_review.call_args.kwargs
    assert kwargs["comments"] == []
    assert kwargs["body"].startswith("**Actionable comments posted: 0**")
    assert "`app.js` at line 30-31:" in kwargs["body"]


def test_review_finding_rejected_with_422_moves_to_the_body(coderabbit, monkeypatch):
    monkeypatch.setattr("pr_agent.git_providers.github_provider.time.sleep", lambda _seconds: None)
    provider = _github()
    provider.pr.create_review.side_effect = [GithubException(422, {"message": "Unprocessable"}), None]
    provider._verify_code_comment = lambda comment: ("Second finding" not in comment["body"], None)
    reviewer = _reviewer(provider)
    issues = [_key_issue(), _key_issue(start_line=4, end_line=4, issue_content="Second finding.",
                                       issue_title="Another one.")]

    result = reviewer._publish_key_issues_as_inline_comments({"review": {"key_issues_to_review": issues}})

    assert len(_review_calls(provider)) == 2
    retry = provider.pr.create_review.call_args.kwargs
    assert len(retry["comments"]) == 1
    assert retry["body"].startswith("**Actionable comments posted: 1**")
    assert "Outside diff range comments (1)" in retry["body"]
    assert "**Another one.**" in retry["body"]
    assert result["review"]["key_issues_to_review"] == [issues[1]]


def test_review_findings_are_not_posted_again_on_a_second_run(coderabbit):
    first_provider = _github()
    _reviewer(first_provider)._publish_key_issues_as_inline_comments(
        {"review": {"key_issues_to_review": [_key_issue()]}})
    posted = [comment["body"] for comment in first_provider.pr.create_review.call_args.kwargs["comments"]]

    second_provider = _github(existing_bodies=posted)
    reviewer = _reviewer(second_provider)
    result = reviewer._publish_key_issues_as_inline_comments(
        {"review": {"key_issues_to_review": [_key_issue(start_line=3, end_line=4)]}})

    kwargs = second_provider.pr.create_review.call_args.kwargs
    assert kwargs["comments"] == []
    assert kwargs["body"].startswith("**Actionable comments posted: 0**")
    assert "Already posted, not repeated:** 1" in kwargs["body"]
    assert "key_issues_to_review" not in result["review"]


def test_review_without_findings_follows_publish_output_no_suggestions(coderabbit):
    provider = _github()
    reviewer = _reviewer(provider)
    reviewer._publish_key_issues_as_inline_comments({"review": {"key_issues_to_review": []}})
    assert provider.pr.create_review.call_args.kwargs["body"].startswith("**Actionable comments posted: 0**")

    coderabbit.set("pr_reviewer.publish_output_no_suggestions", False)
    quiet_provider = _github()
    quiet = _reviewer(quiet_provider)
    quiet._publish_key_issues_as_inline_comments({"review": {"key_issues_to_review": []}})
    quiet_provider.pr.create_review.assert_not_called()
    assert quiet._coderabbit_review_handled is True


def test_review_falls_back_to_the_guide_when_the_review_cannot_be_posted(coderabbit):
    provider = _github()
    provider.pr.create_review.side_effect = GithubException(500, {"message": "boom"})
    reviewer = _reviewer(provider)
    issue = _key_issue()

    result = reviewer._publish_key_issues_as_inline_comments({"review": {"key_issues_to_review": [issue]}})

    assert result["review"]["key_issues_to_review"] == [issue]
    assert reviewer._coderabbit_review_handled is False


def test_review_mode_off_keeps_the_classic_inline_comments(settings):
    settings.set("pr_reviewer.inline_key_issues", True)
    provider = _github()
    provider.validate_comments_inside_hunks = lambda comments: comments
    reviewer = _reviewer(provider)

    reviewer._publish_key_issues_as_inline_comments({"review": {"key_issues_to_review": [_key_issue()]}})

    kwargs = provider.pr.create_review.call_args.kwargs
    assert "body" not in kwargs
    assert kwargs["comments"][0]["body"].startswith("**Possible Issue**\n\nThe new branch never releases the lock.")
    assert "pr-agent:finding" not in kwargs["comments"][0]["body"]
    assert reviewer._coderabbit_review_handled is False


async def _run_review(provider, monkeypatch):
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    reviewer = _reviewer(provider)
    reviewer.vars = {}
    reviewer.prediction = None
    reviewer._should_publish_review_no_suggestions = lambda _review: True

    def prepare_review():
        reviewer._coderabbit_review_handled = reviewer._coderabbit_style_enabled()
        return "## Review guide"

    async def predict(*_args, **_kwargs):
        reviewer.prediction = "review: valid"

    reviewer._prepare_pr_review = prepare_review
    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(pr_reviewer_module, "retry_with_fallback_models", predict)
    await reviewer.run()
    return [call.args[0] for call in provider.publish_comment.call_args_list if call.args]


def _run_provider():
    provider = MagicMock()
    provider.get_files.return_value = ["app.js"]
    provider.should_publish_review_as_thread.return_value = False
    return provider


@pytest.mark.asyncio
async def test_review_guide_comment_is_suppressed_in_the_mode(coderabbit, monkeypatch):
    coderabbit.set("pr_reviewer.persistent_comment", False)
    published = await _run_review(_run_provider(), monkeypatch)
    assert "## Review guide" not in published


@pytest.mark.asyncio
async def test_review_guide_comment_is_kept_by_the_flag(coderabbit, monkeypatch):
    coderabbit.set("pr_reviewer.persistent_comment", False)
    coderabbit.set("pr_reviewer.keep_review_guide_comment", True)
    published = await _run_review(_run_provider(), monkeypatch)
    assert "## Review guide" in published


@pytest.mark.asyncio
async def test_review_guide_comment_is_posted_when_the_mode_is_off(settings, monkeypatch):
    settings.set("pr_reviewer.persistent_comment", False)
    published = await _run_review(_run_provider(), monkeypatch)
    assert "## Review guide" in published


def test_finding_state_is_off_while_the_guide_is_not_posted(coderabbit):
    reviewer = _reviewer(_github())
    assert reviewer._review_finding_state_enabled() is False


# --- /improve ---------------------------------------------------------------------------------------

def _code_suggestion(**overrides):
    suggestion = {"relevant_file": "app.js", "language": "javascript", "relevant_lines_start": 2,
                  "relevant_lines_end": 2, "existing_code": "two", "improved_code": "TWO",
                  "suggestion_content": "Upper-case the constant so the lookup matches.",
                  "suggestion_title": "The lookup misses the constant", "one_sentence_summary": "Fix the lookup",
                  "label": "possible bug", "score": 9, "effort": "quick win"}
    suggestion.update(overrides)
    return suggestion


def _improver(provider):
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = provider
    tool.progress_response = None
    tool.incremental = SimpleNamespace(is_incremental=False)
    return tool


@pytest.mark.asyncio
async def test_improve_suggestions_are_posted_in_one_review(coderabbit):
    provider = _github()
    tool = _improver(provider)
    data = {"code_suggestions": [_code_suggestion(),
                                 _code_suggestion(relevant_file="missing.js", score=4, label="tests")]}

    await tool.push_inline_code_suggestions(data)

    assert len(_review_calls(provider)) == 1
    kwargs = provider.pr.create_review.call_args.kwargs
    assert len(kwargs["comments"]) == 1
    comment = kwargs["comments"][0]
    assert comment["path"] == "app.js" and comment["line"] == 2
    assert comment["body"].startswith("**Critical** · **Possible Bug** · **Quick win**\n\n"
                                      "**The lookup misses the constant.**")
    assert "```suggestion\nTWO\n```" in comment["body"]
    assert "<!-- pr-agent:finding v1 severity=critical category=possible-bug -->\n\n<!-- pr-agent-dedup:" \
        in comment["body"]
    body = kwargs["body"]
    assert body.startswith("**Actionable comments posted: 1**")
    assert "Outside diff range comments (1)" in body
    assert "**Minor** · **Tests** · **Quick win**" in body
    assert "Review comments at @app.js:\n- Line 2: The lookup misses the constant." in body
    provider.pr.create_issue_comment.assert_not_called()
    assert tool._output_published is True


@pytest.mark.asyncio
async def test_improve_non_committable_suggestion_uses_a_proposed_change(coderabbit):
    provider = _github()
    tool = _improver(provider)

    await tool.push_inline_code_suggestions({"code_suggestions": [_code_suggestion(existing_code="three")]})

    body = provider.pr.create_review.call_args.kwargs["comments"][0]["body"]
    assert "```suggestion" not in body
    assert "<summary>Proposed change</summary>" in body
    assert "```diff\n-three\n+TWO\n```" in body


@pytest.mark.asyncio
async def test_improve_suggestions_are_not_posted_again_on_a_second_run(coderabbit):
    first_provider = _github()
    await _improver(first_provider).push_inline_code_suggestions({"code_suggestions": [_code_suggestion()]})
    posted = [comment["body"] for comment in first_provider.pr.create_review.call_args.kwargs["comments"]]

    second_provider = _github(existing_bodies=posted)
    await _improver(second_provider).push_inline_code_suggestions(
        {"code_suggestions": [_code_suggestion(score=6, effort="larger change")]})

    kwargs = second_provider.pr.create_review.call_args.kwargs
    assert kwargs["comments"] == []
    assert kwargs["body"].startswith("**Actionable comments posted: 0**")
    assert "Already posted, not repeated:** 1" in kwargs["body"]


@pytest.mark.asyncio
async def test_improve_falls_back_to_inline_comments_when_the_review_fails(coderabbit):
    provider = _github()
    tool = _improver(provider)
    tool._publish_coderabbit_suggestions = MagicMock(return_value=False)
    provider.publish_code_suggestions = MagicMock(return_value=True)

    await tool.push_inline_code_suggestions({"code_suggestions": [_code_suggestion()]})

    provider.publish_code_suggestions.assert_called_once()
    assert provider.publish_code_suggestions.call_args.args[0][0]["body"].startswith("**Suggestion:**")


@pytest.mark.asyncio
async def test_improve_without_suggestions_posts_an_empty_review(coderabbit):
    provider = _github()
    tool = _improver(provider)

    await tool.publish_no_suggestions()

    kwargs = provider.pr.create_review.call_args.kwargs
    assert kwargs["comments"] == []
    assert kwargs["body"].startswith("**Actionable comments posted: 0**")
    provider.pr.create_issue_comment.assert_not_called()


def test_improve_mode_skips_the_summary_table(settings):
    settings.set("pr_code_suggestions.committable_code_suggestions", False)
    tool = _improver(_github())
    assert tool._uses_summarized_output() is True
    settings.set("pr_code_suggestions.output_style", "coderabbit")
    assert tool._uses_summarized_output() is False


@pytest.mark.asyncio
async def test_improve_mode_off_keeps_the_classic_inline_comments(settings):
    provider = _github()
    provider.publish_code_suggestions = MagicMock(return_value=True)

    await _improver(provider).push_inline_code_suggestions({"code_suggestions": [_code_suggestion()]})

    body = provider.publish_code_suggestions.call_args.args[0][0]["body"]
    assert body.startswith("**Suggestion:** Upper-case the constant")
    assert "pr-agent:finding" not in body
    provider.pr.create_review.assert_not_called()


# --- prompts ----------------------------------------------------------------------------------------

_PROMPTS = {
    "pr_reviewer_prompts.toml": ("pr_review_prompt", "issue_title"),
    "code_suggestions/pr_code_suggestions_prompts.toml": ("pr_code_suggestions_prompt", "suggestion_title"),
    "code_suggestions/pr_code_suggestions_prompts_not_decoupled.toml": (
        "pr_code_suggestions_prompt_not_decoupled", "suggestion_title"),
}
_MODE_BLOCK_RE = re.compile(r"\s*\{%- if coderabbit_style is defined and coderabbit_style %\}.*?\{%- endif %\}",
                            re.DOTALL)


@pytest.mark.parametrize("relative_path", sorted(_PROMPTS))
def test_prompts_ask_for_the_layout_fields_only_in_the_mode(relative_path):
    section, field = _PROMPTS[relative_path]
    environment = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    for part in ("system", "user"):
        template = get_settings().get(section)[part]
        off = environment.from_string(template).render(coderabbit_style=False)
        upstream = environment.from_string(_MODE_BLOCK_RE.sub("", template)).render(coderabbit_style=False)
        assert off == upstream
        assert field not in off and "effort:" not in off
    system = get_settings().get(section)["system"]
    on = environment.from_string(system).render(coderabbit_style=True)
    assert f"{field}: str" in on and "effort: str" in on
    assert f"{field}: |" in on
    if "label: |" in on:
        # New /improve fields come before the label, the YAML repair's last key.
        assert on.index(f"{field}: |") < on.index("label: |")


@pytest.mark.parametrize("output_style", ["coderabbit", ""])
def test_coderabbit_prompt_flag_is_always_in_the_vars(settings, monkeypatch, output_style):
    from pr_agent.tools import pr_code_suggestions as pr_code_suggestions_module
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    settings.set("config.output_style", output_style)
    provider = MagicMock()
    provider.get_languages.return_value = {}
    provider.get_files.return_value = []
    provider.get_pr_description.return_value = ("desc", [])
    provider.pr.title = "Title"
    provider.get_pr_branch.return_value = "branch"
    provider.get_commit_messages.return_value = ""
    for module in (pr_reviewer_module, pr_code_suggestions_module):
        monkeypatch.setattr(module, "get_git_provider_with_context", lambda _url: provider)
        monkeypatch.setattr(module, "get_main_pr_language", lambda _languages, _files: "Python")
        monkeypatch.setattr(module, "get_skills_context", lambda: "")
        monkeypatch.setattr(module, "build_repo_context", lambda _provider: "")

    reviewer = PRReviewer("https://example.invalid/pr/1", ai_handler=lambda: SimpleNamespace())
    improver = PRCodeSuggestions("https://example.invalid/pr/1", ai_handler=lambda: SimpleNamespace())

    assert reviewer.vars["coderabbit_style"] is bool(output_style)
    assert improver.vars["coderabbit_style"] is bool(output_style)
