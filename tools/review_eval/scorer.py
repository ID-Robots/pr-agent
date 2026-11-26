"""Score replayed findings against the reference findings and aggregate the numbers.

A reference is caught when a finding of the same review unit is in the same file, its line range
overlaps the reference lines widened by LINE_WINDOW each way, and the judge says both describe the
same issue. A finding without a valid line range is kept and counted, but never matches. A reference
without lines (a file-level comment) can be matched by any finding in its file.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from typing import Protocol

LINE_WINDOW = 10
_BODY_LIMIT = 6000
_CONTEXT_LINES = 15

SAME_ISSUE_SYSTEM = """\
You compare two code review comments on the same pull request. Decide whether they point out the \
same underlying problem in the code (the same defect, risk or missing handling), even when they word \
it differently or propose different fixes. Two comments about different problems that touch the same \
lines are not the same issue.
Reply with one JSON object and nothing else: {"same_issue": true or false, "reason": "<one sentence>"}"""

VALIDITY_SYSTEM = """\
You check one code review finding against the code it refers to. Decide whether the finding describes \
a real problem in this code that a careful reviewer would want fixed: a bug, a security issue, wrong \
behavior, or a concrete robustness or maintainability defect. A finding that is wrong about the code, \
speculative without a concrete scenario, or purely stylistic is not valid.
Reply with one JSON object and nothing else: {"valid": true or false, "reason": "<one sentence>"}"""


class Judge(Protocol):
    async def same_issue(self, reference: dict, finding: dict) -> bool | None: ...

    async def is_valid(self, finding: dict, code_context: str) -> bool | None: ...


def normalise_path(path) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def location_match(finding: dict, reference: dict, window: int = LINE_WINDOW) -> bool:
    if not finding.get("valid_range") or normalise_path(finding.get("file")) != normalise_path(reference.get("file")):
        return False
    if not normalise_path(reference.get("file")):
        return False
    start, end = reference.get("start_line"), reference.get("end_line")
    if end is None:
        return True
    start = end if start is None else start
    return finding["start_line"] <= end + window and finding["end_line"] >= start - window


def _lines(item: dict) -> str:
    start, end = item.get("start_line"), item.get("end_line")
    if start is None and end is None:
        return "the whole file"
    if start is None or start == end:
        return f"line {end if start is None else start}"
    return f"lines {start}-{end}"


def same_issue_prompt(reference: dict, finding: dict) -> str:
    body = str(reference.get("body") or "")[:_BODY_LIMIT]
    return (f"File: {normalise_path(reference.get('file'))}\n\n"
            f"Reference comment ({_lines(reference)}):\n<<<\n{body}\n>>>\n\n"
            f"Candidate comment ({_lines(finding)}):\n<<<\n{finding.get('text', '')}\n>>>\n")


def validity_prompt(finding: dict, code_context: str) -> str:
    return (f"File: {normalise_path(finding.get('file'))}\n\n"
            f"Finding ({_lines(finding)}):\n<<<\n{finding.get('text', '')}\n>>>\n\n"
            f"Code at the reviewed commit, with line numbers:\n<<<\n{code_context}\n>>>\n")


def parse_verdict(answer, field: str) -> bool | None:
    text = str(answer or "")
    match = re.search(rf'"{field}"\s*:\s*(true|false)', text, re.IGNORECASE)
    if match:
        return match.group(1).lower() == "true"
    first = re.match(r"\s*(yes|no|true|false)\b", text, re.IGNORECASE)
    if first:
        return first.group(1).lower() in {"yes", "true"}
    return None


def code_excerpt(content: str | None, start: int | None, end: int | None, around: int = _CONTEXT_LINES) -> str:
    if not content:
        return "(file not available)"
    lines = content.splitlines()
    if start is None:
        start, end = 1, min(len(lines), 2 * around)
    first, last = max(1, start - around), min(len(lines), (end or start) + around)
    return "\n".join(f"{number} {lines[number - 1]}" for number in range(first, last + 1))


async def match_unit(unit: dict, judge: Judge | None, window: int = LINE_WINDOW) -> None:
    """Mark caught references and matching findings of one replayed unit in place."""
    for finding in unit["findings"]:
        finding["matched_references"] = []
    for reference in unit["references"]:
        reference["candidates"] = 0
        reference["matched_findings"] = []
        for index, finding in enumerate(unit["findings"]):
            if not location_match(finding, reference, window):
                continue
            reference["candidates"] += 1
            verdict = await judge.same_issue(reference, finding) if judge is not None else None
            if verdict:
                reference["matched_findings"].append(index)
                finding["matched_references"].append(reference["comment_id"])
        reference["caught"] = bool(reference["matched_findings"])


def sample_findings(units: list[dict], size: int, seed: int) -> list[tuple[int, int]]:
    population = [(u, f) for u, unit in enumerate(units) for f in range(len(unit["findings"]))]
    if size <= 0 or not population:
        return []
    return sorted(random.Random(seed).sample(population, min(size, len(population))))


async def judge_precision(units: list[dict], judge: Judge | None, size: int, seed: int,
                          read_context: Callable[[dict, dict], str]) -> list[dict]:
    records = []
    for unit_index, finding_index in sample_findings(units, size, seed):
        unit, finding = units[unit_index], units[unit_index]["findings"][finding_index]
        verdict = None
        if judge is not None:
            verdict = await judge.is_valid(finding, read_context(unit, finding))
        records.append({"unit": unit["key"], "repo": unit["repo"], "finding": finding_index, "valid": verdict})
    return records


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _empty_bucket() -> dict:
    return {"prs": set(), "units": 0, "units_skipped": 0, "references": 0, "references_skipped": 0,
            "addressed": 0, "addressed_caught": 0, "addressed_critical_major": 0,
            "addressed_critical_major_caught": 0, "findings": 0, "findings_review": 0, "findings_improve": 0,
            "findings_without_range": 0, "findings_matching": 0, "precision_judged": 0, "precision_valid": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "usd": 0.0, "ai_calls": 0,
            "live_calls": 0, "cached_calls": 0, "dry_calls": 0, "estimated_prompt_tokens": 0,
            "latency_seconds": 0.0, "wall_seconds": 0.0, "commands": 0, "commands_failed": 0}


def _finish(bucket: dict) -> dict:
    prs = len(bucket.pop("prs"))
    units = bucket["units"]
    result = {"prs": prs, **bucket}
    result.update({
        "recall_addressed": _ratio(bucket["addressed_caught"], bucket["addressed"]),
        "recall_addressed_critical_major": _ratio(bucket["addressed_critical_major_caught"],
                                                  bucket["addressed_critical_major"]),
        "findings_per_pr": _ratio(bucket["findings"], prs),
        "findings_per_unit": _ratio(bucket["findings"], units),
        "share_findings_matching_reference": _ratio(bucket["findings_matching"], bucket["findings"]),
        "precision_sampled": _ratio(bucket["precision_valid"], bucket["precision_judged"]),
        "tokens_per_pr": _ratio(bucket["total_tokens"], prs),
        "usd_per_pr": _ratio(bucket["usd"], prs),
        "latency_seconds_per_unit": _ratio(bucket["latency_seconds"], units),
        "usd": round(bucket["usd"], 6),
        "latency_seconds": round(bucket["latency_seconds"], 3),
        "wall_seconds": round(bucket["wall_seconds"], 3),
    })
    return result


def aggregate(units: list[dict], skipped: list[dict], precision: list[dict]) -> dict:
    """Return {"total": {...}, "per_repo": {repo: {...}}} for the scored units."""
    buckets: dict[str, dict] = {}

    def targets(repo: str):
        return (buckets.setdefault("__total__", _empty_bucket()), buckets.setdefault(repo, _empty_bucket()))

    for unit in units:
        for bucket in targets(unit["repo"]):
            bucket["prs"].add(unit["pr_key"])
            bucket["units"] += 1
            for reference in unit["references"]:
                bucket["references"] += 1
                if reference["label"].strip().lower() != "addressed":
                    continue
                critical_or_major = reference["severity"].strip().lower() in {"critical", "major"}
                bucket["addressed"] += 1
                bucket["addressed_caught"] += int(reference["caught"])
                bucket["addressed_critical_major"] += int(critical_or_major)
                bucket["addressed_critical_major_caught"] += int(critical_or_major and reference["caught"])
            for finding in unit["findings"]:
                bucket["findings"] += 1
                bucket[f"findings_{finding['source']}"] += 1
                bucket["findings_without_range"] += int(not finding["valid_range"])
                bucket["findings_matching"] += int(bool(finding.get("matched_references")))
            for command in unit["commands"].values():
                metrics = command["metrics"]
                bucket["commands"] += 1
                bucket["commands_failed"] += int(not command["ok"])
                for name in ("prompt_tokens", "completion_tokens", "total_tokens", "ai_calls", "live_calls",
                             "cached_calls", "dry_calls", "estimated_prompt_tokens"):
                    bucket[name] += metrics[name]
                for name in ("usd", "latency_seconds", "wall_seconds"):
                    bucket[name] += metrics[name]
    for item in skipped:
        for bucket in targets(item["repo"]):
            bucket["units_skipped"] += 1
            bucket["references_skipped"] += len(item["reference_ids"])
    for record in precision:
        if record["valid"] is None:
            continue
        for bucket in targets(record["repo"]):
            bucket["precision_judged"] += 1
            bucket["precision_valid"] += int(record["valid"])
    total = buckets.pop("__total__", _empty_bucket())
    return {"total": _finish(total), "per_repo": {repo: _finish(bucket) for repo, bucket in sorted(buckets.items())}}
