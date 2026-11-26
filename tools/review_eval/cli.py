"""Command line: python -m tools.review_eval --dataset PATH [--profile PATH] [--dry-run] ..."""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import subprocess
import sys
from functools import partial
from pathlib import Path

from starlette_context import request_cycle_context

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.run_details import get_run_details, init_run_details
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.log import setup_logger
from tools.review_eval.dataset import load_dataset, select_entries
from tools.review_eval.github import GhClient, GitStore, default_remote_url
from tools.review_eval.handlers import CachingAIHandler, CallStats, DryRunAIHandler, LLMCache
from tools.review_eval.judge import LLMJudge
from tools.review_eval.profile import Profile, load_profile
from tools.review_eval.replay import replay_unit
from tools.review_eval.repo_instructions import load_repo_instructions, unit_settings
from tools.review_eval.report import write_outputs
from tools.review_eval.scorer import LINE_WINDOW, aggregate, code_excerpt, judge_precision, match_unit
from tools.review_eval.units import ReviewUnit, build_units

DEFAULT_PROFILE = Path(__file__).parent / "profiles" / "baseline.toml"
DEFAULT_JUDGE_MODEL = "anthropic/claude-sonnet-5-5"
KEY_ENV = "ANTHROPIC__KEY"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.review_eval",
        description="Replay /review and /improve on historical pull requests without publishing anything, "
                    "and score the findings against reference findings.")
    parser.add_argument("--dataset", required=True, type=Path, help="JSONL dataset, one pull request per line")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="profile TOML (default: baseline)")
    parser.add_argument("--out", type=Path, help="results folder (default: results/<profile> beside the dataset)")
    parser.add_argument("--cache-dir", type=Path, help="cache folder (default: cache/ beside the dataset)")
    parser.add_argument("--limit", type=int, help="replay only the first N pull requests")
    parser.add_argument("--only", action="append", metavar="REPO#PR",
                        help="replay only this pull request (owner/name#123 or name#123); repeatable")
    parser.add_argument("--dry-run", action="store_true",
                        help="never call a model: record the prompts and their token estimate")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL, help="model of the same-issue judge")
    parser.add_argument("--precision-sample", type=int, default=20, metavar="N",
                        help="findings the judge rates for precision (default: 20)")
    parser.add_argument("--seed", type=int, default=0, help="seed of the precision sample (default: 0)")
    parser.add_argument("--log-level", default="WARNING", help="PR-Agent log level (default: WARNING)")
    parser.add_argument("--repo-instructions", type=Path, metavar="PATH",
                        help="per-repository extra_instructions TOML (python -m tools.review_eval.repo_instructions), "
                             "for a profile that sets [review_eval] repo_instructions = true")
    return parser


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def check_output_dir(path: Path) -> str | None:
    """Refuse a folder inside a git work tree unless git ignores it; return the reason, or None."""
    target = path.resolve()
    existing = target
    while not existing.exists():
        existing = existing.parent
    if not existing.is_dir():
        return f"{path} is not a folder"
    toplevel = _git("-C", str(existing), "rev-parse", "--show-toplevel")
    if toplevel.returncode != 0:
        return None
    root = Path(toplevel.stdout.strip()).resolve()
    relative = os.path.relpath(target, root)
    if relative != "." and not relative.startswith("..") and _git(
            "-C", str(root), "check-ignore", "-q", "--", relative + "/").returncode == 0:
        return None
    return (f"{path} is inside a git work tree and git does not ignore it; "
            f"use a git-ignored folder such as .eval-data/")


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return path.name


def _unit_record(unit: ReviewUnit, replayed: dict) -> dict:
    findings = []
    for command in replayed["commands"].values():
        findings.extend(command["findings"])
    return {
        "key": unit.key, "pr_key": unit.entry.key, "repo": unit.entry.full_name, "pr": unit.entry.number,
        "commit": unit.commit, "base_sha": unit.base_sha, "merge_base": unit.merge_base,
        "commands": {name: {"ok": command["ok"], "error": command["error"], "metrics": command["metrics"],
                            "findings": len(command["findings"])}
                     for name, command in replayed["commands"].items()},
        "files_not_reviewed": (replayed["commands"].get("review") or {}).get("files_not_reviewed", []),
        "published": [call["method"] for call in replayed["published"]],
        "findings": findings,
        "references": [{"comment_id": reference.comment_id, "file": reference.file,
                        "start_line": reference.start_line, "end_line": reference.end_line,
                        "severity": reference.severity, "category": reference.category,
                        "label": reference.label, "body": reference.body} for reference in unit.references],
        "_repo_path": unit.repo_path,
    }


def _public_unit(unit: dict) -> dict:
    """Drop what stays in memory only: the reference bodies (dataset content) and the clone path."""
    public = {key: value for key, value in unit.items() if not key.startswith("_")}
    public["references"] = [{key: value for key, value in reference.items() if key != "body"}
                            for reference in unit["references"]]
    return public


async def run_benchmark(entries, profile: Profile, *, out_dir: Path, cache_dir: Path, dry_run: bool,
                        judge_model: str, precision_sample: int, seed: int, gh_runner=subprocess.run,
                        git_runner=subprocess.run, remote_url=default_remote_url,
                        model_handler=LiteLLMAIHandler, log=print, repo_instructions: dict | None = None) -> dict:
    gh = GhClient(cache_dir, runner=gh_runner)
    git = GitStore(cache_dir, remote_url=remote_url, runner=git_runner)
    llm_cache = LLMCache(cache_dir)

    def make_handler(stats: CallStats):
        if dry_run:
            return partial(DryRunAIHandler, stats=stats, inner_factory=model_handler)
        return partial(CachingAIHandler, cache=llm_cache, stats=stats, inner_factory=model_handler)

    units, skipped = [], []
    for position, entry in enumerate(entries, start=1):
        built, skipped_units = build_units(entry, gh, git)
        skipped.extend(item.as_dict() for item in skipped_units)
        for item in skipped_units:
            log(f"[{position}/{len(entries)}] {item.pr_key} skipped: {item.reason}")
        extra = unit_settings(repo_instructions, entry, profile.settings) if repo_instructions is not None else {}
        if repo_instructions is not None and not extra:
            log(f"[{position}/{len(entries)}] {entry.key}: no repository instructions, profile settings only")
        for unit in built:
            replayed = await replay_unit(unit, profile, make_handler, out_dir / "raw", extra)
            units.append(_unit_record(unit, replayed) | {"repo_instructions": bool(extra)})
            summary = "; ".join(f"{name} {'ok' if command['ok'] else 'FAILED'}, {len(command['findings'])} findings"
                                for name, command in replayed["commands"].items())
            log(f"[{position}/{len(entries)}] {unit.key}: {summary}")

    judge_stats = CallStats()
    judge = None if dry_run else LLMJudge(
        judge_model, partial(CachingAIHandler, cache=llm_cache, stats=judge_stats, inner_factory=model_handler))

    def read_context(unit: dict, finding: dict) -> str:
        content = git.show_file(unit["_repo_path"], unit["commit"], finding["file"]) if finding["file"] else None
        return code_excerpt(content, finding["start_line"], finding["end_line"])

    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        get_settings().set("config.output_run_cost", True)
        details = init_run_details()
        for unit in units:
            await match_unit(unit, judge)
        precision = await judge_precision(units, judge, precision_sample, seed, read_context)
        details = get_run_details() or details

    return {
        "schema_version": 1,
        "run": {
            "profile": profile.name, "profile_path": _display_path(profile.path),
            "profile_description": profile.description, "profile_settings": profile.settings,
            "commands": list(profile.commands), "dry_run": dry_run, "judge_model": judge_model,
            "precision_sample": precision_sample, "seed": seed, "line_window": LINE_WINDOW,
            "pull_requests_selected": len(entries), "gh_requests": gh.requests,
        },
        "scores": aggregate(units, skipped, precision),
        "judge": {
            "calls": judge.calls if judge else 0, "errors": judge.errors if judge else 0,
            "unparsed": judge.unparsed if judge else 0, "live_calls": judge_stats.live_calls,
            "cached_calls": judge_stats.cached_calls, "prompt_tokens": details.prompt_tokens,
            "completion_tokens": details.completion_tokens, "total_tokens": details.total_tokens,
            "usd": float(details.total_cost_usd), "latency_seconds": round(judge_stats.model_seconds, 3),
        },
        "units": [_public_unit(unit) for unit in units],
        "skipped": skipped,
        "precision_sample": precision,
    }


def main(argv=None, **overrides) -> int:
    """Run the benchmark; `overrides` (gh_runner, git_runner, remote_url, model_handler) are for tests."""
    args = build_parser().parse_args(argv)
    setup_logger(args.log_level)

    def fail(message: str) -> int:
        print(f"review_eval: {message}", file=sys.stderr)
        return 2

    if not args.dataset.is_file():
        return fail(f"dataset not found: {args.dataset}")
    try:
        profile = load_profile(args.profile)
    except (OSError, ValueError) as error:
        return fail(f"profile: {error}")
    for warning in profile.warnings:
        print(f"review_eval: warning: {warning}", file=sys.stderr)
    repo_instructions = None
    if profile.repo_instructions:
        if args.repo_instructions is None:
            return fail(f"profile {profile.name} uses per-repository instructions: pass --repo-instructions PATH")
        try:
            repo_instructions = load_repo_instructions(args.repo_instructions)
        except (OSError, ValueError) as error:
            return fail(f"repo instructions: {error}")
    elif args.repo_instructions is not None:
        print(f"review_eval: warning: --repo-instructions ignored, profile {profile.name} does not set "
              f"[review_eval] repo_instructions = true", file=sys.stderr)
    out_dir = args.out or args.dataset.parent / "results" / profile.name
    cache_dir = args.cache_dir or args.dataset.parent / "cache"
    for folder in (out_dir, cache_dir):
        problem = check_output_dir(folder)
        if problem:
            return fail(problem)
    if not args.dry_run and not os.environ.get(KEY_ENV):
        return fail(f"{KEY_ENV} is not set; export it for a real run, or pass --dry-run")
    try:
        entries = select_entries(load_dataset(args.dataset), args.only, args.limit)
    except (OSError, ValueError) as error:
        return fail(str(error))

    def log(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    results = asyncio.run(run_benchmark(
        entries, profile, out_dir=out_dir, cache_dir=cache_dir, dry_run=args.dry_run, judge_model=args.judge_model,
        precision_sample=args.precision_sample, seed=args.seed, log=log, repo_instructions=repo_instructions,
        **overrides))
    results["run"]["dataset"] = args.dataset.name
    results["run"]["repo_instructions"] = args.repo_instructions.name if repo_instructions is not None else None
    results_path, report_path = write_outputs(out_dir, results)
    totals = results["scores"]["total"]
    log(f"review_eval: {totals['units']} units replayed, {totals['units_skipped']} skipped, "
        f"{totals['findings']} findings; wrote {results_path} and {report_path}")
    return 0
