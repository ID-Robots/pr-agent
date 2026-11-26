"""pr_reviewer.second_pass_focus: one more /review model call for concrete defects in named areas.

The second call goes through the same ai_handler and model attempt as the first. Its key issues are
merged after the first pass's without repeats, and it may add up to num_max_findings of its own.
Every other field of the review comes from the first pass. A failed, truncated or unparsable second
pass is logged and the first-pass review is published; the command never fails because of it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_reviewer as pr_reviewer_module
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS = (
    "config.model",
    "config.fallback_models",
    "config.publish_output",
    "config.is_auto_command",
    "config.propagate_tool_errors",
    "openai.deployment_id",
    "openai.fallback_deployments",
    "pr_reviewer.persistent_comment",
    "pr_reviewer.persistent_finding_state",
    "pr_reviewer.num_max_findings",
    "pr_reviewer.second_pass_focus",
)


def _issue(header, content, line):
    return (f"    - relevant_file: |\n        app.py\n      issue_header: |\n        {header}\n"
            f"      issue_content: |\n        {content}\n      start_line: {line}\n      end_line: {line + 1}\n")


_OFF_BY_ONE = _issue("Possible Bug", "The loop skips the last item.", 10)
_RACE = _issue("Race Condition", "Two workers update the counter without a lock.", 30)
_LEAK = _issue("Resource Leak", "The file handle stays open when parsing fails.", 40)
_SWALLOW = _issue("Error Handling", "The retry swallows the timeout and returns None.", 50)

_FIRST = f"review:\n  score: 80\n  key_issues_to_review:\n{_OFF_BY_ONE}  security_concerns: |\n    No\n"
_SECOND = (f"review:\n  score: 10\n  key_issues_to_review:\n{_OFF_BY_ONE}{_RACE}"
           "  security_concerns: |\n    SQL injection in the search query\n")


class _RenderingBudget:
    """Render the shipped review prompts the way the real budget does, without clipping."""

    def fit_prompt_variable(self, variables, name, optional_text, **_kwargs):
        environment = SandboxedEnvironment(undefined=StrictUndefined)
        rendered = dict(variables, **{name: optional_text})
        settings = get_settings()
        return SimpleNamespace(
            optional_text=optional_text,
            system_prompt=environment.from_string(settings.pr_review_prompt.system).render(rendered),
            user_prompt=environment.from_string(settings.pr_review_prompt.user).render(rendered),
        )


@pytest.fixture
def review_settings():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    settings = get_settings()
    settings.set("config.model", "primary-model")
    settings.set("config.fallback_models", [])
    settings.set("openai.deployment_id", None)
    settings.set("openai.fallback_deployments", [])
    settings.set("pr_reviewer.num_max_findings", 3)
    yield settings
    restore_settings(snapshot)


def _build_reviewer(monkeypatch, responses):
    """Run the real PRReviewer.__init__ so the prompt renders from the shipped vars."""
    provider = MagicMock()
    provider.is_supported.return_value = True
    provider.get_languages.return_value = {}
    provider.get_files.return_value = ["app.py"]
    provider.get_commit_messages.return_value = ""
    provider.get_pr_description.return_value = ("desc", [])
    provider.should_publish_review_as_thread.return_value = False
    provider.supports_review_comment_identity.return_value = False
    monkeypatch.setattr(pr_reviewer_module, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(pr_reviewer_module, "get_main_pr_language", lambda languages, files: "Python")
    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", AsyncMock())
    monkeypatch.setattr(pr_reviewer_module, "get_pr_diff", lambda *args, **kwargs: "diff text")

    reviewer = PRReviewer("https://example/pr/1", ai_handler=lambda: SimpleNamespace(main_pr_language=None))
    reviewer.ai_handler = SimpleNamespace(chat_completion=AsyncMock(side_effect=responses))
    reviewer.token_handler = MagicMock()
    reviewer._review_attempt_budget = lambda model: _RenderingBudget()
    monkeypatch.setattr(
        pr_reviewer_module,
        "fit_related_tickets_to_prompt_budget",
        lambda _pr, raw_vars, _system, _user, _model, **_kwargs: (raw_vars, reviewer.token_handler),
    )
    return reviewer


def _system_prompts(reviewer):
    return [call.kwargs["system"] for call in reviewer.ai_handler.chat_completion.await_args_list]


def _headers(review):
    return [issue["issue_header"].strip() for issue in review["key_issues_to_review"]]


@pytest.mark.asyncio
async def test_off_by_default_makes_one_model_call(monkeypatch, review_settings):
    reviewer = _build_reviewer(monkeypatch, [(_FIRST, "stop")])

    await reviewer._prepare_prediction("primary-model")

    assert reviewer.ai_handler.chat_completion.await_count == 1
    assert reviewer.prediction == _FIRST
    assert reviewer.prediction_data is None
    assert "Focused second pass" not in _system_prompts(reviewer)[0]


@pytest.mark.asyncio
async def test_second_pass_merges_new_findings_without_repeats(monkeypatch, review_settings):
    review_settings.set("pr_reviewer.second_pass_focus", "security, concurrency")
    reviewer = _build_reviewer(monkeypatch, [(_FIRST, "stop"), (_SECOND, "stop")])
    plain = _build_reviewer(monkeypatch, [(_FIRST, "stop")])
    review_settings.set("pr_reviewer.second_pass_focus", "")
    await plain._prepare_prediction("primary-model")
    review_settings.set("pr_reviewer.second_pass_focus", "security, concurrency")

    await reviewer._prepare_prediction("primary-model")

    calls = reviewer.ai_handler.chat_completion.await_args_list
    assert [call.kwargs["model"] for call in calls] == ["primary-model", "primary-model"]
    first_system, second_system = _system_prompts(reviewer)
    assert first_system == _system_prompts(plain)[0]
    assert "Focused second pass" in second_system
    assert "concrete defects in these areas: security, concurrency." in second_system
    assert calls[0].kwargs["user"] == calls[1].kwargs["user"]

    review = reviewer.prediction_data["review"]
    assert _headers(review) == ["Possible Bug", "Race Condition"]
    # every field but the key issues comes from the first pass
    assert review["score"] == 80
    assert review["security_concerns"].strip() == "No"
    assert reviewer.prediction == _FIRST


@pytest.mark.asyncio
async def test_second_pass_adds_at_most_num_max_findings(monkeypatch, review_settings):
    review_settings.set("pr_reviewer.second_pass_focus", "error handling")
    review_settings.set("pr_reviewer.num_max_findings", 2)
    second = f"review:\n  key_issues_to_review:\n{_RACE}{_LEAK}{_SWALLOW}"
    reviewer = _build_reviewer(monkeypatch, [(_FIRST, "stop"), (second, "stop")])

    await reviewer._prepare_prediction("primary-model")

    assert _headers(reviewer.prediction_data["review"]) == ["Possible Bug", "Race Condition", "Resource Leak"]


@pytest.mark.asyncio
async def test_second_pass_with_nothing_new_leaves_the_review_untouched(monkeypatch, review_settings):
    review_settings.set("pr_reviewer.second_pass_focus", "security")
    second = f"review:\n  key_issues_to_review:\n{_OFF_BY_ONE}"
    reviewer = _build_reviewer(monkeypatch, [(_FIRST, "stop"), (second, "stop")])

    await reviewer._prepare_prediction("primary-model")

    assert reviewer.ai_handler.chat_completion.await_count == 2
    assert reviewer.prediction == _FIRST
    assert reviewer.prediction_data is None


@pytest.mark.asyncio
@pytest.mark.parametrize("second_response", [
    RuntimeError("provider unavailable"),
    ("this is: [not yaml", "stop"),
    ("review: {}\n", "stop"),
    (f"review:\n  key_issues_to_review:\n{_RACE}", "length"),
    ("review:\n  key_issues_to_review:\n    - just a string\n", "stop"),
], ids=["raises", "garbage", "empty-review", "truncated", "no-issue-objects"])
async def test_a_failed_second_pass_keeps_the_first_pass_review(monkeypatch, review_settings, second_response):
    review_settings.set("pr_reviewer.second_pass_focus", "security")
    review_settings.set("config.publish_output", True)
    review_settings.set("config.is_auto_command", False)
    review_settings.set("config.propagate_tool_errors", False)
    review_settings.set("pr_reviewer.persistent_comment", False)
    review_settings.set("pr_reviewer.persistent_finding_state", False)
    reviewer = _build_reviewer(monkeypatch, [(_FIRST, "stop"), second_response])
    markdown = MagicMock(return_value="rendered review")
    monkeypatch.setattr(pr_reviewer_module, "convert_to_markdown_v2", markdown)
    monkeypatch.setattr(pr_reviewer_module, "github_action_output", MagicMock())

    await reviewer.run()

    assert reviewer.ai_handler.chat_completion.await_count == 2
    assert _headers(markdown.call_args.args[0]["review"]) == ["Possible Bug"]
    reviewer.git_provider.publish_comment.assert_any_call("rendered review")
    published = [str(call) for call in reviewer.git_provider.publish_comment.call_args_list]
    assert not any("Failed to review PR" in body for body in published)


@pytest.mark.parametrize(("value", "expected"), [
    ("", ""), ("   ", ""), (None, ""), (False, ""), (True, ""), (0, ""), ("false", ""), ("none", ""),
    (" security ", "security"),
    ("security, concurrency, error handling", "security, concurrency, error handling"),
    (["security", " concurrency ", ""], "security, concurrency"),
])
def test_the_focus_setting_forms(review_settings, value, expected):
    review_settings.set("pr_reviewer.second_pass_focus", value)

    assert PRReviewer._second_pass_focus() == expected
