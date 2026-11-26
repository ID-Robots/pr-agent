"""Read the benchmark dataset: one pull request per JSON line, with its reference findings."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_PR_URL_RE = re.compile(r"^https?://[^/\s]+/([^/\s]+)/([^/\s]+)/pull/(\d+)")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
CRITICAL_OR_MAJOR = frozenset({"critical", "major"})


def optional_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


@dataclass
class Reference:
    """One reference finding; the location fields are refined from its review comment."""

    comment_id: int
    file: str
    line: int | None
    severity: str
    category: str
    label: str
    body: str
    # Filled from the review comment: the commit it was written on and its lines at that commit.
    commit: str | None = None
    start_line: int | None = None
    end_line: int | None = None

    @property
    def addressed(self) -> bool:
        return self.label.strip().lower() == "addressed"

    @property
    def critical_or_major(self) -> bool:
        return self.severity.strip().lower() in CRITICAL_OR_MAJOR


@dataclass
class PullRequestEntry:
    owner: str
    name: str
    number: int
    repo_label: str
    references: list[Reference]

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def key(self) -> str:
        return f"{self.full_name}#{self.number}"


def _parse_reference(raw: dict) -> Reference:
    comment_id = optional_int(raw.get("github_comment_id"))
    if comment_id is None:
        raise ValueError("reference without an integer github_comment_id")
    return Reference(
        comment_id=comment_id,
        file=str(raw.get("file") or ""),
        line=optional_int(raw.get("line")),
        severity=str(raw.get("severity") or ""),
        category=str(raw.get("category") or ""),
        label=str(raw.get("label") or ""),
        body=str(raw.get("body") or ""),
    )


def parse_entry(raw: dict) -> PullRequestEntry:
    """Build an entry from one dataset line; `repo` is owner/name, or `url` names the PR."""
    repo_label = str(raw.get("repo") or "")
    number = optional_int(raw.get("pr"))
    owner = name = ""
    match = _PR_URL_RE.match(str(raw.get("url") or ""))
    if match:
        owner, name = match.group(1), match.group(2)
        number = number if number is not None else int(match.group(3))
    if "/" in repo_label:
        owner, name = repo_label.split("/", 1)
    if not owner or not name or number is None:
        raise ValueError("entry needs repo as owner/name (or a pull request url) and a pr number")
    if not _NAME_RE.match(owner) or not _NAME_RE.match(name):
        raise ValueError(f"unsupported repository name {owner}/{name}")
    references = [_parse_reference(item) for item in raw.get("reference") or []]
    return PullRequestEntry(owner=owner, name=name, number=number, repo_label=repo_label or name,
                            references=references)


def load_dataset(path: str | Path) -> list[PullRequestEntry]:
    entries = []
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                entries.append(parse_entry(json.loads(line)))
            except (ValueError, TypeError, AttributeError) as error:
                raise ValueError(f"dataset line {line_number}: {error}") from error
    return entries


def _only_matches(entry: PullRequestEntry, selector: str) -> bool:
    repo, separator, number = selector.strip().rpartition("#")
    if not separator or not number.strip().isdigit() or int(number) != entry.number:
        return False
    repo = repo.strip().lower()
    return repo in {entry.full_name.lower(), entry.name.lower(), entry.repo_label.lower()}


def select_entries(entries: list[PullRequestEntry], only: list[str] | None = None,
                   limit: int | None = None) -> list[PullRequestEntry]:
    """Keep the entries --only names (repo#pr, repeatable), then the first --limit of them."""
    if only:
        unknown = [selector for selector in only if not any(_only_matches(e, selector) for e in entries)]
        if unknown:
            raise ValueError(f"--only matches no dataset entry: {', '.join(unknown)}")
        entries = [entry for entry in entries if any(_only_matches(entry, selector) for selector in only)]
    if limit is not None:
        entries = entries[:max(limit, 0)]
    return entries
