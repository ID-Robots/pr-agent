# Review benchmark (`tools/review_eval`)

An offline replay benchmark for `/review` and `/improve`. For each historical pull request in a
dataset it rebuilds the code exactly as a reference reviewer saw it, runs both PR-Agent commands on
that code **without publishing anything**, and scores PR-Agent's findings against the reference
findings.

The harness is not part of the `pr_agent` package and is not shipped with it.

## What is measured

The input is a JSONL dataset, one pull request per line:

```json
{"repo": "owner/name", "pr": 12, "url": "https://github.com/owner/name/pull/12",
 "reference": [{"github_comment_id": 123, "file": "src/a.py", "line": 40, "severity": "Major",
                "category": "bug", "label": "addressed", "body": "..."}]}
```

`label` is `addressed` (a human fixed it, so it was a real issue), `noise` or `pending`.

1. **Review units.** For every reference, `gh api repos/<owner>/<repo>/pulls/comments/<id>` gives the
   commit the comment was written on (`original_commit_id`) and its path and lines at that commit
   (`original_start_line`/`original_line`, preferred to the dataset line). The references of a pull
   request are grouped by that commit: each distinct commit is one review unit.
2. **Reviewed diff.** The base is `base.sha` from `gh api repos/<owner>/<repo>/pulls/<n>`, never the
   live base-branch tip (for a merged pull request that tip already contains the commit and the diff
   would be empty). The reviewed diff is `merge-base(commit, base.sha)..commit`, never the merged final
   diff, where the bugs are already fixed. Repository files PR-Agent reads for context come from
   `base.sha`; the PR description is the title plus the commit messages of the reviewed range.
3. **Replay.** `PRAgent().handle_request(<unit>, ["review"])` and then `["improve"]` run in process,
   each in its own copy of the settings, on a replay git provider (a `LocalGitProvider` over the cached
   clone) whose every `publish_*` call is only recorded in memory. The harness forces
   `config.publish_output=false` and `config.output_run_cost=true`, and reads the structured output of
   each command from a per-command JSON file. Review `key_issues_to_review` and `/improve`
   `code_suggestions` become findings `{source, file, start_line, end_line, text, severity | score}`.
4. **Scoring.** A reference is caught when a finding of the same review unit is in the same file, its
   line range overlaps the reference lines widened by 10 lines each way, and an LLM judge
   (`--judge-model`) says both describe the same issue. A finding without a valid line range is kept
   and counted but never matches; a file-level reference (no lines) can be matched by any finding in
   its file. A seeded sample of findings (`--precision-sample N`, `--seed`) is rated by the judge as
   valid or not, with the code around the finding.
5. **Report**, per repository and in total: recall on addressed Critical/Major references, recall on
   all addressed references, findings per PR and per review unit, share of findings that match any
   reference (addressed, noise or pending), judge-rated precision on the sample, tokens and USD per
   PR, model latency per unit, and every skipped unit with its reason. Judge tokens and cost are
   reported separately from the replay cost.

A reference whose comment was deleted, whose commit cannot be fetched or whose diff is empty is
skipped with that reason in `report.md` and `results.json`; it is never scored silently.

## Running it

Run every command from the repository root, with the dataset in the git-ignored `.eval-data/`
folder (`.eval-data/dev.jsonl`, `.eval-data/holdout.jsonl`). `.eval-data/` is never committed: the
dataset, the generated repository instructions, the results and the caches all stay there.

### Dry run

gh and git only: no model call, no key and no cost. Use it to check access and the reviewed diffs:

```bash
PYTHONPATH=. uv run python -m tools.review_eval --dataset .eval-data/dev.jsonl \
  --profile tools/review_eval/profiles/baseline.toml --out .eval-data/results/baseline-dry --dry-run --limit 2
```

Any profile works the same way: `--profile tools/review_eval/profiles/<profile>.toml
--out .eval-data/results/<profile>-dry --dry-run --limit 2`.

### Dev runs, one per profile

A real run calls the model through LiteLLM with the Anthropic key in `ANTHROPIC__KEY`:

```bash
export ANTHROPIC__KEY=...   # never commit or print it

PYTHONPATH=. uv run python -m tools.review_eval --dataset .eval-data/dev.jsonl \
  --profile tools/review_eval/profiles/baseline.toml --out .eval-data/results/baseline
PYTHONPATH=. uv run python -m tools.review_eval --dataset .eval-data/dev.jsonl \
  --profile tools/review_eval/profiles/more_findings.toml --out .eval-data/results/more_findings
PYTHONPATH=. uv run python -m tools.review_eval --dataset .eval-data/dev.jsonl \
  --profile tools/review_eval/profiles/low_score_threshold.toml --out .eval-data/results/low_score_threshold
PYTHONPATH=. uv run python -m tools.review_eval --dataset .eval-data/dev.jsonl \
  --profile tools/review_eval/profiles/strong_prompt.toml --out .eval-data/results/strong_prompt
PYTHONPATH=. uv run python -m tools.review_eval --dataset .eval-data/dev.jsonl \
  --profile tools/review_eval/profiles/full_file_context.toml --out .eval-data/results/full_file_context
PYTHONPATH=. uv run python -m tools.review_eval --dataset .eval-data/dev.jsonl \
  --profile tools/review_eval/profiles/second_pass.toml --out .eval-data/results/second_pass
PYTHONPATH=. uv run python -m tools.review_eval --dataset .eval-data/dev.jsonl \
  --profile tools/review_eval/profiles/opus_big_prs.toml --out .eval-data/results/opus_big_prs

# repo_instructions: generate the instructions from the dev set once, then run the profile with them
PYTHONPATH=. uv run python -m tools.review_eval.repo_instructions --dataset .eval-data/dev.jsonl \
  --out .eval-data/repo_instructions.toml
PYTHONPATH=. uv run python -m tools.review_eval --dataset .eval-data/dev.jsonl \
  --profile tools/review_eval/profiles/repo_instructions.toml --repo-instructions .eval-data/repo_instructions.toml \
  --out .eval-data/results/repo_instructions
```

Keep `--judge-model` at its default for every profile so the scores stay comparable. The cache is
shared: gh answers and clones are fetched once, and a model call whose prompt a previous run already
sent is served from the cache for free (`low_score_threshold` sends exactly the baseline prompts).

### Holdout confirmation

Compare the profile with the best dev result (`<best>` below) against the baseline on the holdout
set:

```bash
PYTHONPATH=. uv run python -m tools.review_eval --dataset .eval-data/holdout.jsonl \
  --profile tools/review_eval/profiles/baseline.toml --out .eval-data/results/baseline-holdout
PYTHONPATH=. uv run python -m tools.review_eval --dataset .eval-data/holdout.jsonl \
  --profile tools/review_eval/profiles/<best>.toml --out .eval-data/results/<best>-holdout
```

When `<best>` is `repo_instructions`, add `--repo-instructions .eval-data/repo_instructions.toml`:
reuse the file generated from the dev set and never generate one from the holdout set.

Options:

| Option | Meaning |
| --- | --- |
| `--dataset PATH` | JSONL dataset (required) |
| `--profile PATH` | profile TOML (default `profiles/baseline.toml`) |
| `--out DIR` | results folder (default `results/<profile>` beside the dataset) |
| `--cache-dir DIR` | cache folder (default `cache/` beside the dataset), shared by all profiles |
| `--limit N` | replay only the first N pull requests (after `--only`) |
| `--only REPO#PR` | replay only this pull request, `owner/name#12` or `name#12`; repeatable |
| `--dry-run` | never call a model: record the prompts and a token estimate, return empty answers |
| `--judge-model MODEL` | judge model (default `anthropic/claude-sonnet-5-5`); keep it fixed across profiles |
| `--precision-sample N`, `--seed S` | size and seed of the precision sample (default 20 and 0) |
| `--log-level LEVEL` | PR-Agent log level (default `WARNING`) |
| `--repo-instructions PATH` | per-repository instructions for a profile with `repo_instructions = true` |

A run that is not a dry run stops at once when `ANTHROPIC__KEY` is not set. `--out` and
`--cache-dir` are refused when they are inside a git work tree that does not ignore them, so results
and caches cannot be committed by accident. Calls are priced from the cost map bundled with LiteLLM
(`LITELLM_LOCAL_MODEL_COST_MAP=True`, as in the Docker images).

## Outputs

- `<out>/report.md`: the tables above.
- `<out>/results.json`: run settings, scores (`total` and `per_repo`), judge usage, every unit with
  its commands' metrics, findings and references (without their bodies), skipped units, and the
  precision sample.
- `<out>/raw/<unit>/<command>.json`: each command's structured output; in a dry run also
  `<command>.prompts.jsonl` with the prompts that would have been sent.

## Profiles

A profile is a TOML file in `profiles/`. Every table is a PR-Agent settings section; each key is
applied with `get_settings().set("<section>.<key>", value)` before every replayed command, on top of
the PR-Agent defaults. The reserved `[review_eval]` table holds harness options:

```toml
[review_eval]
description = "What this profile changes"   # shown in report.md
commands = ["review", "improve"]           # which commands to replay (default both)
repo_instructions = false                  # true: needs --repo-instructions PATH (default false)

[config]
model = "anthropic/claude-sonnet-5-5"

[pr_reviewer]
num_max_findings = 6
```

`config.git_provider`, `config.publish_output` and `config.output_run_cost` belong to the harness
and are rejected in a profile. A key that is not among the PR-Agent defaults is applied anyway with a
warning, so a typo shows up on the console. The repository's own `.pr_agent.toml` is not applied:
the profile alone decides the settings.

`baseline.toml` is PR-Agent's defaults with `config.model = "anthropic/claude-sonnet-5-5"`, an
Anthropic-only `config.fallback_models` and `config.output_style = "coderabbit"`. Every other
profile is the baseline plus one candidate change, named in its header comment:

| Profile | Candidate | Settings on top of the baseline |
| --- | --- | --- |
| `baseline` | none | (see above) |
| `more_findings` | more findings per review and per chunk | `pr_reviewer.num_max_findings = 6` and `pr_code_suggestions.num_code_suggestions_per_chunk = 6` (defaults 3 and 3), `pr_reviewer.enable_large_pr_chunking = true` so a big pull request gets several review chunks |
| `low_score_threshold` | lower suggestion score threshold | `pr_code_suggestions.suggestions_score_threshold = 1`: already the default and the floor, so a no-op today (see [Score filters](#score-filters)) |
| `strong_prompt` | stronger review prompt | `pr_reviewer.extra_instructions` and `pr_code_suggestions.extra_instructions`: only concrete, verifiable defects, each with file and line, no style remarks |
| `full_file_context` | whole changed files instead of hunks | `config.patch_full_file_context = true`, `config.max_model_tokens = 200000` (at the default 32000 a diff over the budget loses all extra lines) |
| `second_pass` | focused second review pass | `pr_reviewer.second_pass_focus = "security, concurrency, error handling"` (one more `/review` call per unit) |
| `opus_big_prs` | Opus for big pull requests | `config.model = "anthropic/claude-opus-5-5"`; `[model_routing]` on with one rule sending a pull request of at most 10 files and 40000 patch characters to `anthropic/claude-sonnet-5-5`; `config.model_reasoning = "anthropic/claude-sonnet-5-5"` so the `/improve` self-reflection stays on the baseline model (routing only replaces the regular tier) |
| `repo_instructions` | per-repository instructions | `[review_eval] repo_instructions = true`: each repository's generated text is added to both `extra_instructions` settings (see [Per-repository instructions](#per-repository-instructions)) |

`config.patch_full_file_context` and `pr_reviewer.second_pass_focus` are PR-Agent settings that are
off by default (`false` and `""`). Loading `opus_big_prs` warns that `config.model_reasoning` is not
a default setting: `configuration.toml` documents it only as a commented-out line, and PR-Agent reads
it all the same. Cost per pull request rises with `full_file_context` (whole files, up to 200000
prompt tokens per call), `opus_big_prs` (Opus prices on big pull requests), `second_pass` and
`more_findings` (extra review calls).

### Score filters

What filters `/improve` suggestions by score, checked for `low_score_threshold`:

- On the path the harness reads (`config.publish_output = false`), a suggestion is dropped by score
  only in `filter_suggestions_by_score_threshold`: a score below
  `pr_code_suggestions.suggestions_score_threshold`, read with a floor of 1 (0 and below act as 1).
  Self-reflection gives a wrong suggestion 0, so those are always dropped; no setting keeps them.
- `pr_code_suggestions.score_on_reflection_failure` (default 7) is the score given when
  self-reflection fails; it drops those suggestions only when set below the threshold.
- The coderabbit rendering filters nothing by score: it maps the score to a severity (9-10
  Critical, 7-8 Major, 3-6 Minor, 0-2 Trivial), moves a suggestion that cannot be anchored in the
  diff to the review body and skips one that is already posted. It only runs when publishing, as do
  `pr_code_suggestions.dual_publishing_score_threshold` (default 0, off) and the publish-time
  line-range check, so none of them changes what the harness scores.
- `pr_code_suggestions.max_suggestions_per_file` (default 0, no cap) limits the count, not the score.

So at today's defaults no lower threshold exists to try; the profile pins the floor explicitly and
measures the same as the baseline.

### Per-repository instructions

`python -m tools.review_eval.repo_instructions --dataset PATH --out PATH` counts, per repository,
the categories and severities of the references labelled `addressed` and turns them into a short
`extra_instructions` text: how many findings were fixed, the five categories fixed most often with
their counts, the counts per severity, and "look for these kinds of defect first". Only category
names, severity names and counts are used; no comment body, file name or other dataset text. The
output is a TOML file keyed by the repository's short name, so it names the dataset's repositories:
write it under `.eval-data/` (the tool refuses a folder git does not ignore) and never commit it.

With `[review_eval] repo_instructions = true` and `--repo-instructions PATH`, every command of a
pull request runs with its repository's text added to `pr_reviewer.extra_instructions` and
`pr_code_suggestions.extra_instructions` (after the profile's own text, if any). A repository missing
from the file is replayed with the profile alone and logged; `results.json` records the file name in
`run.repo_instructions` and `repo_instructions: true|false` per unit. Such a profile refuses to run
without the flag, and any other profile ignores the flag with a warning.

Generate the file from the dev set only and reuse it for the holdout run. The instructions come from
the same references the dev run is scored against, so dev numbers for this profile are optimistic;
only the holdout run measures it fairly.

## Cache layout

Everything under `--cache-dir` (default `.eval-data/cache/`) is reused by every later run and every
profile:

- `gh/<sha256 of the api path>.json`: `gh api` answers, including "not found" for deleted comments
  (access errors are not cached, so they are retried).
- `repos/<owner>/<name>.git`: bare clones that only fetch `refs/pull/<n>/head` and the needed
  commits (kept under `refs/review-eval/<sha>`); fetches authenticate through `gh auth git-credential`
  for that command only.
- `llm/<key[:2]>/<key>.json`: one model call per file, keyed by
  `sha256(model, system, user, temperature)` with the prompt's "Today's Date" line left out of the
  key. A hit returns the stored answer and replays its stored usage, cost and latency, so a rerun
  reports the same numbers with zero model calls. The judge uses the same cache.

Delete `llm/` to force fresh model answers; delete `gh/` to refetch pull request metadata.

## Tests

`tests/unittest/test_review_eval_harness.py` and `tests/unittest/test_review_eval_scorer.py` run
fully offline: `gh` and the model are fakes and git runs in a temporary folder. The end-to-end test
refuses every socket connection and every GitHub provider construction while both commands run.
`tests/unittest/test_review_eval_profiles.py` loads every profile and checks that each key is a
PR-Agent setting and that every model is an `anthropic/` id; `tests/unittest/test_review_eval_repo_instructions.py`
tests the generator and the `--repo-instructions` hook on synthetic datasets.
