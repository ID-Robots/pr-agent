"""config.patch_full_file_context: /review and /improve send each changed file whole.

With the key off the diff builders keep the capped extra-line context. With it on, a file with
several hunks is rendered once from its first line to its last, numbered hunks keep the real
new-file line numbers, patch_extension_skip_types still applies, and a diff over the token budget
still falls back to the compressed diff.
"""

import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.algo import pr_processing, token_budget
from pr_agent.algo.pr_processing import full_file_context_kwargs, get_pr_diff, get_pr_multi_diffs
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS = ("config.patch_full_file_context", "pr_code_suggestions.decouple_hunks")
_LINES = 40
_CHANGED = (10, 30)


class _WordTokenHandler:
    prompt_tokens = 100

    @staticmethod
    def count_tokens(text):
        return len(text.split())


class _Provider:
    def __init__(self, files):
        self.files = files

    def get_diff_files(self):
        return self.files

    def get_languages(self):
        return {"Python": 100}

    def get_filtered_diff_file_names(self):
        return []


def _base_lines():
    lines = [f"value_{number} = {number}" for number in range(1, _LINES + 1)]
    lines[4] = "def handler():"  # a section header that dynamic context would stop at
    return lines


def _head_lines():
    lines = _base_lines()
    for number in _CHANGED:
        lines[number - 1] = f"value_{number} = {number} * 2"
    return lines


def _hunk(number):
    base, head = _base_lines(), _head_lines()
    start = number - 3
    body = [f" {line}" for line in base[start - 1:number - 1]]
    body += [f"-{base[number - 1]}", f"+{head[number - 1]}"]
    body += [f" {line}" for line in base[number:number + 3]]
    return [f"@@ -{start},7 +{start},7 @@ def handler():", *body]


def _file(filename="app.py"):
    patch_lines = [line for number in _CHANGED for line in _hunk(number)]
    return FilePatchInfo(
        base_file="\n".join(_base_lines()) + "\n",
        head_file="\n".join(_head_lines()) + "\n",
        patch="\n".join(patch_lines),
        filename=filename,
        edit_type=EDIT_TYPE.MODIFIED,
    )


def _new_hunk_lines(rendered):
    """Return (line number, text) for every line of the rendered __new hunk__ sections."""
    numbered = []
    for section in rendered.split("__new hunk__")[1:]:
        for line in section.split("__old hunk__")[0].splitlines():
            if match := re.match(r"^(\d+) [ +](.*)$", line):
                numbered.append((int(match.group(1)), match.group(2)))
    return numbered


@pytest.fixture
def budget():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    with (
        patch.object(token_budget, "get_max_tokens", lambda model, **kwargs: 100_000),
        patch.object(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}]),
    ):
        yield
    restore_settings(snapshot)


def _render(builder, provider, **kwargs):
    if builder == "get_pr_diff":
        return get_pr_diff(provider, _WordTokenHandler(), "model", add_line_numbers_to_hunks=True, **kwargs)
    chunks = get_pr_multi_diffs(provider, _WordTokenHandler(), "model", add_line_numbers=True, **kwargs)
    assert len(chunks) == 1
    return chunks[0]


@pytest.mark.parametrize("builder", ["get_pr_diff", "get_pr_multi_diffs"])
def test_off_keeps_the_capped_hunk_context(budget, builder):
    rendered = _render(builder, _Provider([_file()]))

    assert rendered == _render(builder, _Provider([_file()]), full_file_context=False)
    numbers = [number for number, _ in _new_hunk_lines(rendered)]
    assert 1 not in numbers
    assert _LINES not in numbers
    assert 20 not in numbers


@pytest.mark.parametrize("builder", ["get_pr_diff", "get_pr_multi_diffs"])
def test_a_two_hunk_file_is_rendered_whole_once_with_real_line_numbers(budget, builder):
    rendered = _render(builder, _Provider([_file()]), full_file_context=True)

    assert _new_hunk_lines(rendered) == list(enumerate(_head_lines(), start=1))
    for number in _CHANGED:
        assert f"{number} +value_{number} = {number} * 2" in rendered
        assert rendered.count(f"-value_{number} = {number}\n") == 1
    assert rendered.count("## File: 'app.py'") == 1


@pytest.mark.parametrize("builder", ["get_pr_diff", "get_pr_multi_diffs"])
def test_skip_types_still_get_no_extra_lines(budget, builder):
    rendered = _render(builder, _Provider([_file("notes.md")]), full_file_context=True)

    numbers = [number for number, _ in _new_hunk_lines(rendered)]
    assert numbers == [*range(7, 14), *range(27, 34)]


def test_a_full_file_diff_over_the_budget_falls_back_to_the_compressed_diff(budget):
    full = _render("get_pr_diff", _Provider([_file()]), full_file_context=True)
    hunks_only = get_pr_diff(_Provider([_file()]), _WordTokenHandler(), "model",
                             add_line_numbers_to_hunks=True, disable_extra_lines=True)
    fits_hunks_only = (_WordTokenHandler.prompt_tokens + pr_processing.OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD
                       + (_WordTokenHandler.count_tokens(full) + _WordTokenHandler.count_tokens(hunks_only)) // 2)

    with patch.object(token_budget, "get_max_tokens", lambda model, **kwargs: fits_hunks_only):
        rendered = _render("get_pr_diff", _Provider([_file()]), full_file_context=True)

    assert _WordTokenHandler.count_tokens(rendered) < _WordTokenHandler.count_tokens(full)
    assert [number for number, _ in _new_hunk_lines(rendered)] == [*range(7, 14), *range(27, 34)]


@pytest.mark.parametrize(("value", "expected"), [
    (None, {}), (False, {}), ("false", {}), (0, {}),
    (True, {"full_file_context": True}), ("true", {"full_file_context": True}), (1, {"full_file_context": True}),
])
def test_the_setting_is_read_as_a_boolean(budget, value, expected):
    if value is not None:
        get_settings().set("config.patch_full_file_context", value)

    assert full_file_context_kwargs() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_review_passes_the_setting_to_get_pr_diff(budget, enabled):
    get_settings().set("config.patch_full_file_context", enabled)
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.ai_handler = SimpleNamespace()
    reviewer.token_handler = MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.vars = {}
    reviewer._raw_prompt_vars = None
    reviewer._get_prediction = MagicMock(side_effect=AssertionError("no model call expected"))

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value="") as diff:
        await reviewer._prepare_prediction("model")

    assert ("full_file_context" in diff.call_args.kwargs) is enabled
    if enabled:
        assert diff.call_args.kwargs["full_file_context"] is True


class _Stop(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("decouple_hunks", [True, False])
@pytest.mark.parametrize("enabled", [False, True])
async def test_improve_passes_the_setting_to_get_pr_multi_diffs(budget, enabled, decouple_hunks):
    get_settings().set("config.patch_full_file_context", enabled)
    get_settings().set("pr_code_suggestions.decouple_hunks", decouple_hunks)
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = MagicMock()
    tool.ai_handler = SimpleNamespace()
    tool.vars = {"diff": "", "diff_no_line_numbers": ""}
    tool.pr_code_suggestions_prompt_system = "system"
    tool.pr_code_suggestions_prompt_user = "user"
    fake_budget = SimpleNamespace(token_handler=_WordTokenHandler(), require_input_capacity=lambda *a, **k: None)

    with (
        patch("pr_agent.tools.pr_code_suggestions.AttemptTokenBudget.for_prompt_attempt", return_value=fake_budget),
        patch("pr_agent.tools.pr_code_suggestions.get_pr_multi_diffs", side_effect=_Stop) as multi_diffs,
        pytest.raises(_Stop),
    ):
        await tool.prepare_prediction_main("model")

    assert ("full_file_context" in multi_diffs.call_args.kwargs) is enabled
    if enabled:
        assert multi_diffs.call_args.kwargs["full_file_context"] is True
