"""Read-only access to the hosted pull requests: a cached `gh api` wrapper and a cached clone store.

Nothing here writes to the host. `gh api` is only ever called with a GET path, and git only fetches.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_GIT_TIMEOUT_SECONDS = 900
# Authenticate fetches through the gh login for this command only; no git config is changed.
_GH_CREDENTIALS = ("-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential")


class GhError(RuntimeError):
    """`gh api` failed; the message is the first line gh printed, never a credential."""


class GhNotFound(GhError):
    """The object does not exist (any more), e.g. a deleted review comment."""


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return "no output"


class GhClient:
    """`gh api <path>` with a JSON cache, so a rerun makes no request at all.

    Successful answers and "not found" answers are cached; other failures (access refused,
    network) are not, so a later run can retry them.
    """

    def __init__(self, cache_dir: str | Path, runner: Callable = subprocess.run):
        self.cache_dir = Path(cache_dir) / "gh"
        self.runner = runner
        self.requests = 0

    def _cache_file(self, path: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(path.encode('utf-8')).hexdigest()}.json"

    def api(self, path: str) -> dict:
        cache_file = self._cache_file(path)
        if cache_file.is_file():
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("status") == "not_found":
                raise GhNotFound(f"{path}: not found (cached)")
            return cached["data"]
        self.requests += 1
        try:
            completed = self.runner(["gh", "api", path], capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            raise GhError(f"gh api {path}: {type(error).__name__}") from error
        if completed.returncode != 0:
            message = _first_line(completed.stderr or completed.stdout)
            if "HTTP 404" in message or "Not Found" in message:
                self._store(cache_file, {"path": path, "status": "not_found"})
                raise GhNotFound(f"{path}: {message}")
            raise GhError(f"gh api {path}: {message}")
        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise GhError(f"gh api {path}: answer is not JSON") from error
        self._store(cache_file, {"path": path, "status": "ok", "data": data})
        return data

    @staticmethod
    def _store(cache_file: Path, payload: dict) -> None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, cache_file)


def default_remote_url(owner: str, name: str) -> str:
    return f"https://github.com/{owner}/{name}.git"


class GitStore:
    """Bare clones under <cache>/repos/<owner>/<name>.git that only ever fetch.

    Fetched commits are kept under refs/review-eval/<sha> so git gc does not prune them.
    """

    def __init__(self, cache_dir: str | Path, remote_url: Callable[[str, str], str] = default_remote_url,
                 runner: Callable = subprocess.run):
        self.root = Path(cache_dir) / "repos"
        self.remote_url = remote_url
        self.runner = runner

    def repo_path(self, owner: str, name: str) -> Path:
        if not _NAME_RE.match(owner) or not _NAME_RE.match(name):
            raise ValueError(f"unsupported repository name {owner}/{name}")
        return self.root / owner / f"{name}.git"

    def _git(self, path: Path, *args: str, timeout: int = _GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        return self.runner(["git", "-C", str(path), *args], capture_output=True, text=True, errors="replace",
                           timeout=timeout, check=False, env=env)

    def ensure_repo(self, owner: str, name: str) -> Path:
        path = self.repo_path(owner, name)
        if not (path / "HEAD").is_file():
            path.mkdir(parents=True, exist_ok=True)
            completed = self._git(path, "init", "--bare", "--quiet")
            if completed.returncode != 0:
                raise RuntimeError(f"git init failed: {_first_line(completed.stderr)}")
        return path

    def has_commit(self, path: Path, sha: str) -> bool:
        return self._git(path, "cat-file", "-e", f"{sha}^{{commit}}", timeout=60).returncode == 0

    def ensure_commits(self, owner: str, name: str, pr_number: int, shas: list[str]) -> tuple[Path, dict[str, str]]:
        """Fetch what is missing; return the clone and {sha: reason} for each unfetchable commit."""
        path = self.ensure_repo(owner, name)
        failures = {sha: "not a commit sha" for sha in shas if not _SHA_RE.match(sha or "")}
        missing = [sha for sha in dict.fromkeys(shas) if sha not in failures and not self.has_commit(path, sha)]
        if not missing:
            return path, failures
        url = self.remote_url(owner, name)
        # The PR head ref brings most review commits (and their history) in one fetch.
        self._git(path, *_GH_CREDENTIALS, "fetch", "--quiet", "--no-tags", url,
                  f"+refs/pull/{int(pr_number)}/head:refs/pull/{int(pr_number)}/head")
        for sha in missing:
            if self.has_commit(path, sha):
                continue
            completed = self._git(path, *_GH_CREDENTIALS, "fetch", "--quiet", "--no-tags", url,
                                  f"{sha}:refs/review-eval/{sha}")
            if completed.returncode != 0 or not self.has_commit(path, sha):
                failures[sha] = f"commit {sha[:12]} not fetchable: {_first_line(completed.stderr)}"
        return path, failures

    def merge_base(self, path: Path, first: str, second: str) -> str | None:
        completed = self._git(path, "merge-base", first, second, timeout=120)
        sha = completed.stdout.strip()
        return sha if completed.returncode == 0 and _SHA_RE.match(sha) else None

    def diff_is_empty(self, path: Path, base: str, head: str) -> bool:
        completed = self._git(path, "diff", "--quiet", base, head, timeout=300)
        if completed.returncode not in (0, 1):
            raise RuntimeError(f"git diff failed: {_first_line(completed.stderr)}")
        return completed.returncode == 0

    def show_file(self, path: Path, sha: str, file_path: str) -> str | None:
        completed = self._git(path, "show", f"{sha}:{file_path}", timeout=60)
        return completed.stdout if completed.returncode == 0 else None
