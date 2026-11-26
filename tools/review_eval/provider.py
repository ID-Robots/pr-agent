"""A git provider that replays one commit of a cached clone and publishes nothing.

It is a LocalGitProvider whose head and base are fixed commits of a bare clone, so no checkout or
branch is needed. Every publish_* call is only recorded in memory on the replay target; nothing
leaves the process. publish_review_with_body is defined as well, so the output_style="coderabbit"
prompts are exercised exactly as on GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from git import Repo

from pr_agent.algo.file_filter import filter_ignored_with_status
from pr_agent.algo.language_handler import partition_files_by_language_filter
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import register_git_provider
from pr_agent.git_providers.git_provider import cache_languages
from pr_agent.git_providers.local_git_provider import LocalGitProvider, PullRequestMimic
from pr_agent.log import get_logger

PROVIDER_ID = "review_eval_replay"


@dataclass
class ReplayTarget:
    repo_path: Path
    head_sha: str
    diff_base_sha: str  # merge-base(head, pull request base): the reviewed diff starts here
    context_sha: str  # pull request base.sha: repository files (instructions) are read here
    repo_full_name: str = ""
    number: int = 0
    title: str = ""
    head_ref: str = ""
    base_ref: str = ""
    published: list[dict] = field(default_factory=list)


_TARGETS: dict[str, ReplayTarget] = {}


def register_target(target_id: str, target: ReplayTarget) -> None:
    _TARGETS[target_id] = target


def unregister_target(target_id: str) -> None:
    _TARGETS.pop(target_id, None)


class ReplayGitProvider(LocalGitProvider):
    def __init__(self, pr_url: str, incremental=False):
        target = _TARGETS.get(pr_url)
        if target is None:
            raise ValueError("No replay target is registered for this id")
        self.pr_url = pr_url
        self.target = target
        self.repo_path = Path(target.repo_path)
        self.repo = Repo(self.repo_path)
        self.head_branch_name = target.head_ref or target.head_sha[:7]
        self.target_branch_name = target.base_ref or target.context_sha[:7]
        self.diff_files = None
        self.pr = PullRequestMimic(self.get_pr_title(), self.get_diff_files())
        # Like the local provider: there is no hosted PR to attach inline comments to.
        get_settings().pr_reviewer.inline_code_comments = False

    def _record(self, method: str, **payload) -> None:
        self.target.published.append({"method": method, **payload})

    def get_pr_identity(self) -> dict:
        return {"url": "", "provider": PROVIDER_ID, "repo": self.target.repo_full_name,
                "number": str(self.target.number or ""), "head_sha": self.target.head_sha}

    def get_diff_files(self) -> list[FilePatchInfo]:
        cached_diff_files = getattr(self, "diff_files", None)
        if cached_diff_files is not None:
            return cached_diff_files

        head = self.repo.commit(self.target.head_sha)
        diffs = head.diff(self.repo.commit(self.target.diff_base_sha), create_patch=True, R=True)
        diff_files = []
        for diff_item in diffs:
            filename = diff_item.b_path or diff_item.a_path
            try:
                base_file = diff_item.a_blob.data_stream.read().decode("utf-8") if diff_item.a_blob else ""
                head_file = diff_item.b_blob.data_stream.read().decode("utf-8") if diff_item.b_blob else ""
                patch = diff_item.diff.decode("utf-8")
            except UnicodeDecodeError as e:
                get_logger().warning(f"Skipping non-UTF-8 file in replayed diff: {filename!r} ({e})")
                continue
            edit_type = EDIT_TYPE.MODIFIED
            if diff_item.new_file:
                edit_type = EDIT_TYPE.ADDED
            elif diff_item.deleted_file:
                edit_type = EDIT_TYPE.DELETED
            elif diff_item.renamed_file:
                edit_type = EDIT_TYPE.RENAMED
            diff_files.append(FilePatchInfo(
                base_file, head_file, patch, filename, edit_type=edit_type,
                old_filename=None if diff_item.a_path == diff_item.b_path else diff_item.a_path,
            ))
        self.diff_files, ignore_complete = filter_ignored_with_status(diff_files, platform="local")
        self.filtered_diff_file_names = (
            partition_files_by_language_filter(self.diff_files)[1] if ignore_complete else []
        )
        return self.diff_files

    @cache_languages
    def get_languages(self):
        tree = self.repo.commit(self.target.head_sha).tree
        return self._language_percentages(Path(item.path).name for item in tree.traverse() if item.type == "blob")

    def get_repo_file_content(self, file_path: str, from_default_branch: bool = False) -> str:
        """Read the file at the pull request base, never at the reviewed commit."""
        try:
            blob = self.repo.commit(self.target.context_sha).tree / file_path
        except KeyError:
            return ""
        if blob.type != "blob":
            return ""
        return blob.data_stream.read().decode("utf-8", errors="replace")

    def get_repo_context_ref(self, from_default_branch: bool = False) -> str | None:
        return self.target.context_sha

    def get_pr_description_full(self):
        commits = self.repo.iter_commits(f"{self.target.diff_base_sha}..{self.target.head_sha}")
        return " ".join(commit.message for commit in commits)

    def get_pr_title(self):
        return self.target.title or self.head_branch_name

    # --- publishing: record only -------------------------------------------------------------

    def publish_description(self, pr_title: str, pr_body: str):
        self._record("publish_description", title=pr_title, body=pr_body)

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        self._record("publish_comment", body=pr_comment, is_temporary=is_temporary)

    def publish_persistent_comment(self, pr_comment: str, *args, **kwargs):
        self._record("publish_persistent_comment", body=pr_comment)

    def publish_persistent_comment_full(self, pr_comment: str, *args, **kwargs):
        self._record("publish_persistent_comment_full", body=pr_comment)

    def publish_tool_failure(self, tool: str, message: str):
        self._record("publish_tool_failure", tool=tool, body=message)

    def publish_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str,
                               original_suggestion=None):
        self._record("publish_inline_comment", body=body, file=relevant_file, line=relevant_line_in_file)

    def publish_inline_comments(self, comments: list[dict]):
        self._record("publish_inline_comments", comments=list(comments))

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        self._record("publish_code_suggestions", suggestions=list(code_suggestions))
        return True

    def publish_code_suggestions_artifact(self, code_suggestions: list, artifact_footer: str = "",
                                          no_suggestions_message: str = "") -> bool:
        self._record("publish_code_suggestions_artifact", suggestions=list(code_suggestions))
        return True

    def publish_review_with_body(self, comments: list[dict], build_body) -> list[int]:
        self._record("publish_review_with_body", comments=list(comments), body=build_body([]))
        return []

    def publish_labels(self, labels):
        self._record("publish_labels", labels=list(labels or []))
        return True

    def edit_comment(self, comment, body: str):
        self._record("edit_comment", body=body)

    def reply_to_comment_from_comment_id(self, comment_id: int, body: str):
        self._record("reply_to_comment_from_comment_id", comment_id=comment_id, body=body)


def ensure_registered() -> None:
    """Make the provider selectable as config.git_provider; repeated calls are a no-op.

    Registered on first use rather than at import, so importing the harness leaves the provider
    registry unchanged.
    """
    register_git_provider(PROVIDER_ID, ReplayGitProvider)
