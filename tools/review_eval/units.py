"""Turn a dataset entry into review units: one per commit the reference comments were written on.

The reviewed diff of a unit is merge-base(commit, base.sha)..commit, where base.sha comes from the
pull request itself. The live base-branch tip is never used: for a merged pull request it already
contains the commit, so that diff would be empty. Whatever cannot be replayed is returned as a
skipped unit with its reason, so it is reported and never scored silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tools.review_eval.dataset import PullRequestEntry, Reference, optional_int
from tools.review_eval.github import GhClient, GhError, GhNotFound, GitStore


@dataclass
class ReviewUnit:
    entry: PullRequestEntry
    commit: str
    base_sha: str
    merge_base: str
    repo_path: Path
    title: str = ""
    base_ref: str = ""
    head_ref: str = ""
    references: list[Reference] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.entry.key}@{self.commit[:12]}"


@dataclass
class SkippedUnit:
    pr_key: str
    repo: str
    pr: int
    commit: str | None
    reference_ids: list[int]
    reason: str

    def as_dict(self) -> dict:
        return {"pr_key": self.pr_key, "repo": self.repo, "pr": self.pr, "commit": self.commit,
                "reference_ids": self.reference_ids, "reason": self.reason}


def _refine_reference(reference: Reference, comment: dict) -> None:
    """Take the path and lines the comment had at the commit it was written on."""
    reference.commit = str(comment.get("original_commit_id") or "") or None
    reference.file = str(comment.get("path") or reference.file)
    end_line = optional_int(comment.get("original_line"))
    if end_line is None and comment.get("subject_type") != "file":
        end_line = reference.line
    start_line = optional_int(comment.get("original_start_line")) or end_line
    if start_line is not None and end_line is not None and start_line > end_line:
        start_line, end_line = end_line, start_line
    reference.start_line, reference.end_line = start_line, end_line


def build_units(entry: PullRequestEntry, gh: GhClient, git: GitStore) -> tuple[list[ReviewUnit], list[SkippedUnit]]:
    repo = entry.full_name
    skipped: list[SkippedUnit] = []

    def skip(commit, references, reason):
        skipped.append(SkippedUnit(entry.key, repo, entry.number, commit,
                                   [reference.comment_id for reference in references], reason))

    if not entry.references:
        skip(None, [], "no reference findings")
        return [], skipped
    try:
        pull = gh.api(f"repos/{repo}/pulls/{entry.number}")
    except GhError as error:
        skip(None, entry.references, f"pull request not readable: {error}")
        return [], skipped
    base_sha = str((pull.get("base") or {}).get("sha") or "")
    if not base_sha:
        skip(None, entry.references, "pull request has no base sha")
        return [], skipped

    by_commit: dict[str, list[Reference]] = {}
    for reference in entry.references:
        try:
            comment = gh.api(f"repos/{repo}/pulls/comments/{reference.comment_id}")
        except GhNotFound:
            skip(None, [reference], "review comment deleted")
            continue
        except GhError as error:
            skip(None, [reference], f"review comment not readable: {error}")
            continue
        _refine_reference(reference, comment)
        if not reference.commit:
            skip(None, [reference], "review comment has no original_commit_id")
            continue
        by_commit.setdefault(reference.commit, []).append(reference)
    if not by_commit:
        return [], skipped

    try:
        repo_path, failures = git.ensure_commits(entry.owner, entry.name, entry.number, [base_sha, *by_commit])
    except (OSError, RuntimeError, ValueError) as error:
        for commit, references in by_commit.items():
            skip(commit, references, f"clone not available: {error}")
        return [], skipped
    if base_sha in failures:
        for commit, references in by_commit.items():
            skip(commit, references, f"base {failures[base_sha]}")
        return [], skipped

    units = []
    for commit, references in by_commit.items():
        if commit in failures:
            skip(commit, references, failures[commit])
            continue
        merge_base = git.merge_base(repo_path, commit, base_sha)
        if merge_base is None:
            skip(commit, references, "no merge base between the commit and the pull request base")
            continue
        try:
            empty = git.diff_is_empty(repo_path, merge_base, commit)
        except RuntimeError as error:
            skip(commit, references, str(error))
            continue
        if empty:
            skip(commit, references, "empty diff")
            continue
        units.append(ReviewUnit(
            entry=entry, commit=commit, base_sha=base_sha, merge_base=merge_base, repo_path=repo_path,
            title=str(pull.get("title") or ""), base_ref=str((pull.get("base") or {}).get("ref") or ""),
            head_ref=str((pull.get("head") or {}).get("ref") or ""), references=references,
        ))
    return units, skipped
