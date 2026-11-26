"""Write results.json and a readable report.md for one benchmark run."""

from __future__ import annotations

import json
from pathlib import Path

_SUMMARY_ROWS = (
    ("PRs replayed", "prs"),
    ("Review units replayed", "units"),
    ("Review units skipped", "units_skipped"),
    ("References scored", "references"),
    ("References skipped", "references_skipped"),
    ("Recall, addressed Critical/Major", "recall_addressed_critical_major"),
    ("Recall, all addressed", "recall_addressed"),
    ("Addressed Critical/Major caught", "addressed_critical_major_caught", "addressed_critical_major"),
    ("Addressed caught", "addressed_caught", "addressed"),
    ("Findings", "findings"),
    ("Findings per PR", "findings_per_pr"),
    ("Findings per review unit", "findings_per_unit"),
    ("Share of findings matching a reference", "share_findings_matching_reference"),
    ("Judge-rated precision (sample)", "precision_sampled"),
    ("Precision sample judged", "precision_judged"),
    ("Tokens per PR", "tokens_per_pr"),
    ("USD per PR", "usd_per_pr"),
    ("USD total", "usd"),
    ("AI calls (live / cached / dry)", "ai_calls", None),
    ("Model latency per unit (s)", "latency_seconds_per_unit"),
    ("Commands failed", "commands_failed", "commands"),
)

_REPO_COLUMNS = (
    ("Repo", None), ("PRs", "prs"), ("Units", "units"), ("Skipped", "units_skipped"),
    ("Recall C/M", "recall_addressed_critical_major"), ("Recall addressed", "recall_addressed"),
    ("Findings/PR", "findings_per_pr"), ("Matching", "share_findings_matching_reference"),
    ("Precision", "precision_sampled"), ("Tokens/PR", "tokens_per_pr"), ("USD/PR", "usd_per_pr"),
    ("Latency/unit (s)", "latency_seconds_per_unit"),
)


def _value(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") if value else "0"
    return str(value)


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _summary_value(totals: dict, row: tuple) -> str:
    if len(row) == 2:
        return _value(totals.get(row[1]))
    if row[2] is None:
        return f"{totals['ai_calls']} ({totals['live_calls']} / {totals['cached_calls']} / {totals['dry_calls']})"
    return f"{_value(totals.get(row[1]))} of {_value(totals.get(row[2]))}"


def render_report(results: dict) -> str:
    totals = results["scores"]["total"]
    run = results["run"]
    lines = [f"# Review benchmark: {run['profile']}", ""]
    if run.get("profile_description"):
        lines += [run["profile_description"], ""]
    lines += [
        f"- Dataset: `{run.get('dataset', '')}`, commands: {', '.join(run['commands'])}",
        f"- Judge: `{run['judge_model']}`, line window ±{run['line_window']}, "
        f"precision sample {run['precision_sample']} (seed {run['seed']})",
    ]
    if run["dry_run"]:
        lines.append("- **Dry run**: no model was called, so findings, recall and cost are empty by design; "
                     "the prompt token estimate is in results.json.")
    lines += ["", "## Totals", "", "| Metric | Value |", "| --- | --- |"]
    lines += [f"| {row[0]} | {_summary_value(totals, row)} |" for row in _SUMMARY_ROWS]
    if run["dry_run"]:
        lines.append(f"| Estimated prompt tokens (dry run) | {totals['estimated_prompt_tokens']} |")
    lines += ["", "## Per repository", "",
              "| " + " | ".join(name for name, _ in _REPO_COLUMNS) + " |",
              "|" + " --- |" * len(_REPO_COLUMNS)]
    for repo, scores in results["scores"]["per_repo"].items():
        cells = [_cell(repo)] + [_value(scores.get(key)) for _, key in _REPO_COLUMNS[1:]]
        lines.append("| " + " | ".join(cells) + " |")
    if results["skipped"]:
        lines += ["", "## Skipped", "", "| PR | Commit | References | Reason |", "| --- | --- | --- | --- |"]
        for item in results["skipped"]:
            commit = (item["commit"] or "")[:12] or "-"
            lines.append(f"| {_cell(item['pr_key'])} | {commit} | {len(item['reference_ids'])} | "
                         f"{_cell(item['reason'])} |")
    failed = [(unit["key"], name, command["error"]) for unit in results["units"]
              for name, command in unit["commands"].items() if not command["ok"]]
    if failed:
        lines += ["", "## Failed commands", "", "| Unit | Command | Error |", "| --- | --- | --- |"]
        lines += [f"| {_cell(key)} | {name} | {_cell(error)} |" for key, name, error in failed]
    judge = results.get("judge") or {}
    if judge:
        lines += ["", f"Judge calls: {judge.get('calls', 0)} (errors {judge.get('errors', 0)}, "
                      f"unparsed {judge.get('unparsed', 0)}), judge USD {_value(judge.get('usd'))}."]
    return "\n".join(lines) + "\n"


def write_outputs(out_dir: Path, results: dict) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path, report_path = out_dir / "results.json", out_dir / "report.md"
    results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    report_path.write_text(render_report(results), encoding="utf-8")
    return results_path, report_path
