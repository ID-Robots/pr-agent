"""Benchmark profiles: a TOML file whose tables are PR-Agent settings sections.

Each `[section]` key is applied with get_settings().set("section.key", value) before every replayed
command. The one reserved table, `[review_eval]`, holds harness options instead of settings.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pr_agent.config_loader import get_settings, global_settings

RESERVED_TABLE = "review_eval"
SUPPORTED_COMMANDS = ("review", "improve")
# The harness sets these itself before every command; a profile may not change them.
HARNESS_KEYS = frozenset({"config.git_provider", "config.publish_output", "config.output_run_cost"})
_HARNESS_OPTIONS = frozenset({"description", "commands", "repo_instructions"})


@dataclass
class Profile:
    name: str
    path: Path
    description: str = ""
    commands: tuple[str, ...] = SUPPORTED_COMMANDS
    # True: add each repository's text from --repo-instructions to the extra_instructions settings.
    repo_instructions: bool = False
    settings: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _known_key(section: str, key: str) -> bool:
    values = global_settings.get(section, None)
    return hasattr(values, "keys") and key.lower() in {str(name).lower() for name in values.keys()}


def load_profile(path: str | Path) -> Profile:
    path = Path(path)
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    profile = Profile(name=path.stem, path=path)
    for section, table in data.items():
        if not isinstance(table, dict):
            raise ValueError(f"{path.name}: '{section}' must be a [section] table")
        if section == RESERVED_TABLE:
            unknown = set(table) - _HARNESS_OPTIONS
            if unknown:
                raise ValueError(f"{path.name}: unknown [review_eval] option(s): {', '.join(sorted(unknown))}")
            profile.description = str(table.get("description", ""))
            commands = table.get("commands", list(SUPPORTED_COMMANDS))
            if (not isinstance(commands, list) or not commands
                    or any(command not in SUPPORTED_COMMANDS for command in commands)):
                raise ValueError(f"{path.name}: [review_eval] commands must be a non-empty list of "
                                 f"{', '.join(SUPPORTED_COMMANDS)}")
            profile.commands = tuple(dict.fromkeys(commands))
            repo_instructions = table.get("repo_instructions", False)
            if not isinstance(repo_instructions, bool):
                raise ValueError(f"{path.name}: [review_eval] repo_instructions must be true or false")
            profile.repo_instructions = repo_instructions
            continue
        for key, value in table.items():
            dotted = f"{section}.{key}"
            if dotted.lower() in HARNESS_KEYS:
                raise ValueError(f"{path.name}: {dotted} is set by the harness and cannot be overridden")
            if not _known_key(section, key):
                profile.warnings.append(f"{dotted} is not a default PR-Agent setting; it is applied as given")
            profile.settings[dotted] = value
    return profile


def apply_profile(profile: Profile) -> None:
    settings = get_settings()
    for dotted, value in profile.settings.items():
        settings.set(dotted, value)
