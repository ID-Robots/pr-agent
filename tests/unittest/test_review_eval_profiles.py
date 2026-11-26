"""Every shipped review_eval profile loads, names real settings and uses Anthropic model ids."""

import re
from pathlib import Path

import pytest

from pr_agent.algo import MAX_TOKENS
from pr_agent.config_loader import global_settings
from tools.review_eval.profile import load_profile

ROOT = Path(__file__).resolve().parents[2]
PROFILES_DIR = ROOT / "tools" / "review_eval" / "profiles"
PROFILES = sorted(PROFILES_DIR.glob("*.toml"))
CANDIDATES = {"more_findings", "low_score_threshold", "strong_prompt", "full_file_context", "second_pass",
              "opus_big_prs", "repo_instructions"}
# Read by PR-Agent but only documented as a commented-out line in configuration.toml.
COMMENTED_DEFAULTS = {"config.model_reasoning"}
# Baseline settings a candidate replaces on purpose; every other baseline setting must stay.
REPLACED_BASELINE_KEYS = {"opus_big_prs": {"config.model"}}


def _default_keys(section: str) -> set[str]:
    values = global_settings.get(section, None)
    return {str(name).lower() for name in values.keys()} if hasattr(values, "keys") else set()


def test_baseline_and_every_candidate_have_a_profile():
    assert {path.stem for path in PROFILES} == CANDIDATES | {"baseline"}


@pytest.mark.parametrize("path", PROFILES, ids=lambda path: path.stem)
def test_profile_keys_exist_in_the_default_settings(path):
    profile = load_profile(path)
    configuration = (ROOT / "pr_agent" / "settings" / "configuration.toml").read_text(encoding="utf-8")
    assert path.read_text(encoding="utf-8").startswith("# ")
    assert profile.settings
    for dotted in profile.settings:
        section, key = dotted.split(".", 1)
        if dotted in COMMENTED_DEFAULTS:
            assert re.search(rf"^#\s*{re.escape(key)}\s*=", configuration, re.MULTILINE), dotted
        else:
            assert key.lower() in _default_keys(section), f"{path.name}: {dotted} is not a PR-Agent setting"


@pytest.mark.parametrize("path", PROFILES, ids=lambda path: path.stem)
def test_profile_models_are_anthropic_ids(path):
    settings = load_profile(path).settings
    models = [settings["config.model"], *settings.get("config.fallback_models", [])]
    models += [settings[key] for key in ("config.model_reasoning", "config.model_weak") if key in settings]
    models += [rule["model"] for rule in settings.get("model_routing.rules", [])]
    for model in models:
        assert model.startswith("anthropic/") and model in MAX_TOKENS, f"{path.name}: {model}"


@pytest.mark.parametrize("name", sorted(CANDIDATES))
def test_candidates_keep_the_baseline_settings(name):
    baseline = load_profile(PROFILES_DIR / "baseline.toml")
    profile = load_profile(PROFILES_DIR / f"{name}.toml")
    for key, value in baseline.settings.items():
        if key not in REPLACED_BASELINE_KEYS.get(name, set()):
            assert profile.settings.get(key) == value, f"{name}: {key}"
    assert profile.commands == baseline.commands
    assert profile.repo_instructions is (name == "repo_instructions")
