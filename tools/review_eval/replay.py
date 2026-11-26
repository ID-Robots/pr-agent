"""Replay /review and /improve on one review unit, in process, and normalise what they found.

Each command runs through PRAgent.handle_request inside its own request_cycle_context copy of the
settings, with the replay provider selected and publishing off. The tool's structured output goes to
a per-command JSON file (the review_json_output_path context variable, as the CLI's --json-output).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

from starlette_context import request_cycle_context

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.review_json_output import review_json_output_path
from pr_agent.algo.run_details import command_failed, get_run_details, init_run_details
from pr_agent.config_loader import get_settings, global_settings
from tools.review_eval.handlers import CallStats
from tools.review_eval.profile import Profile, apply_profile
from tools.review_eval.provider import (
    PROVIDER_ID,
    ReplayTarget,
    ensure_registered,
    register_target,
    unregister_target,
)
from tools.review_eval.units import ReviewUnit

FORCED_SETTINGS = {
    "config.git_provider": PROVIDER_ID,
    "config.publish_output": False,
    "config.publish_output_progress": False,
    "config.output_run_cost": True,
    "push_outputs.enable": False,
}


def _line(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _text(*parts) -> str:
    return "\n".join(str(part).strip() for part in parts if isinstance(part, (str, int, float)) and str(part).strip())


def make_finding(source: str, file, start, end, text: str, severity=None, score=None) -> dict:
    start_line, end_line = _line(start), _line(end)
    if start_line is not None and end_line is None:
        end_line = start_line
    valid = start_line is not None and end_line is not None and 1 <= start_line <= end_line
    return {"source": source, "file": str(file or "").strip(), "start_line": start_line, "end_line": end_line,
            "valid_range": valid, "text": text, "severity": severity, "score": score}


def review_findings(data: dict) -> list[dict]:
    issues = ((data or {}).get("review") or {}).get("key_issues_to_review") or []
    findings = []
    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, dict):
            continue
        text = _text(issue.get("issue_header"), issue.get("issue_title"), issue.get("issue_content"))
        severity = str(issue["severity"]).strip() if issue.get("severity") else None
        findings.append(make_finding("review", issue.get("relevant_file"), issue.get("start_line"),
                                     issue.get("end_line"), text, severity=severity))
    return findings


def improve_findings(data: dict) -> list[dict]:
    suggestions = (data or {}).get("code_suggestions") or []
    findings = []
    for suggestion in suggestions if isinstance(suggestions, list) else []:
        if not isinstance(suggestion, dict):
            continue
        text = _text(suggestion.get("one_sentence_summary"), suggestion.get("suggestion_content"))
        findings.append(make_finding("improve", suggestion.get("relevant_file"),
                                     suggestion.get("relevant_lines_start"), suggestion.get("relevant_lines_end"),
                                     text, score=_line(suggestion.get("score"))))
    return findings


_NORMALISERS = {"review": review_findings, "improve": improve_findings}


def _metrics(details, stats: CallStats) -> dict:
    return {
        "prompt_tokens": getattr(details, "prompt_tokens", 0),
        "completion_tokens": getattr(details, "completion_tokens", 0),
        "total_tokens": getattr(details, "total_tokens", 0),
        "cache_read_tokens": getattr(details, "cache_read_tokens", 0),
        "cache_creation_tokens": getattr(details, "cache_creation_tokens", 0),
        "ai_calls": getattr(details, "num_ai_calls", 0),
        "usd": float(getattr(details, "total_cost_usd", 0) or 0),
        "cost_status": getattr(details, "cost_status", "unavailable") if details is not None else "unavailable",
        "model_used": getattr(details, "model_used", None),
        "wall_seconds": round(details.duration_seconds, 3) if details is not None else 0.0,
        "latency_seconds": round(stats.model_seconds, 3),
        "live_calls": stats.live_calls,
        "cached_calls": stats.cached_calls,
        "dry_calls": stats.dry_calls,
        "estimated_prompt_tokens": stats.estimated_prompt_tokens,
    }


def unit_target_id(unit: ReviewUnit) -> str:
    return f"review-eval/{unit.entry.full_name}/pull/{unit.entry.number}/{unit.commit[:12]}"


async def run_command(target_id: str, command: str, profile: Profile, handler_factory: Callable,
                      stats: CallStats, json_path: Path, unit_settings: dict | None = None) -> dict:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.unlink(missing_ok=True)
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        settings = get_settings()
        apply_profile(profile)
        # Settings for this unit's repository only (per-repository instructions), over the profile.
        for key, value in (unit_settings or {}).items():
            settings.set(key, value)
        for key, value in FORCED_SETTINGS.items():
            settings.set(key, value)
        token = review_json_output_path.set(str(json_path))
        # A failure before the tool installs its own collector must not report a previous command.
        init_run_details()
        try:
            result = await PRAgent(ai_handler=handler_factory).handle_request(target_id, [command])
            details = get_run_details()
            failed = command_failed()
        finally:
            review_json_output_path.reset(token)
    record = {"command": command, "ok": result is True and not failed, "error": None,
              "metrics": _metrics(details, stats), "findings": [], "files_not_reviewed": []}
    if not json_path.is_file():
        record["ok"] = False
        record["error"] = "no structured output" if result is True else "command failed"
        return record
    data = json.loads(json_path.read_text(encoding="utf-8"))
    record["findings"] = _NORMALISERS[command](data)
    if command == "review":
        record["files_not_reviewed"] = list((data.get("coverage") or {}).get("files_not_reviewed") or [])
    if not record["ok"] and record["error"] is None:
        record["error"] = "command failed"
    return record


async def replay_unit(unit: ReviewUnit, profile: Profile, make_handler: Callable[[CallStats], Callable],
                      raw_dir: Path, unit_settings: dict | None = None) -> dict:
    """Run the profile's commands on one unit; return per-command records and the published calls.

    `unit_settings` are applied after the profile in every command of this unit.
    """
    ensure_registered()
    target_id = unit_target_id(unit)
    target = ReplayTarget(repo_path=unit.repo_path, head_sha=unit.commit, diff_base_sha=unit.merge_base,
                          context_sha=unit.base_sha, repo_full_name=unit.entry.full_name, number=unit.entry.number,
                          title=unit.title, head_ref=unit.head_ref, base_ref=unit.base_ref)
    register_target(target_id, target)
    unit_dir = raw_dir / f"{unit.entry.owner}__{unit.entry.name}__{unit.entry.number}__{unit.commit[:12]}"
    commands = {}
    try:
        for command in profile.commands:
            stats = CallStats()
            commands[command] = await run_command(target_id, command, profile, make_handler(stats), stats,
                                                  unit_dir / f"{command}.json", unit_settings)
            if stats.prompts:
                with open(unit_dir / f"{command}.prompts.jsonl", "w", encoding="utf-8") as handle:
                    handle.writelines(json.dumps(prompt, ensure_ascii=False) + "\n" for prompt in stats.prompts)
    finally:
        unregister_target(target_id)
    return {"commands": commands, "published": target.published}
