"""Model handlers for the replay: a caching wrapper around the real handler, and a dry-run handler.

The caching handler keys every call by hash(model, system, user, temperature). A hit returns the
stored answer and replays the stored usage, cost and latency into the run details, so a rerun
reports the same numbers while making no model call. The dry-run handler never calls a model: it
records the prompts with a token estimate and returns an empty answer that both tools accept.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.run_details import get_run_details, init_run_details
from pr_agent.algo.token_handler import TokenEncoder

# The review and suggestion prompts carry today's date; leave it out of the key so a rerun on
# another day still hits the cache.
_DATE_RE = re.compile(r"(Today's Date: )\d{4}-\d{2}-\d{2}")
# Optional handler hooks the token budget reads; forwarded so prompts are budgeted as in a real run.
_FORWARDED_HOOKS = frozenset({"normalize_request_prompts", "get_output_token_reserve", "get_output_token_limit"})
_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens",
                 "cache_creation_tokens", "num_ai_calls", "known_cost_call_count")

DRY_RUN_REVIEW_ANSWER = """\
review:
  estimated_effort_to_review_[1-5]: 1
  relevant_tests: |
    No
  key_issues_to_review: []
  security_concerns: |
    No
"""
DRY_RUN_SUGGESTIONS_ANSWER = "code_suggestions: []\n"


def dry_run_answer(system: str) -> str:
    """An empty answer of the shape the prompt asks for: a review, or a list of suggestions."""
    return DRY_RUN_REVIEW_ANSWER if "$PRReview" in (system or "") else DRY_RUN_SUGGESTIONS_ANSWER


def cache_key(model: str, system: str, user: str, temperature: float, img_path: str | None = None) -> str:
    payload = [str(model), _DATE_RE.sub(r"\1<date>", system or ""), _DATE_RE.sub(r"\1<date>", user or ""),
               float(temperature)]
    if img_path:
        payload.append(str(img_path))
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass
class CallStats:
    """Calls one command made through the harness handlers."""

    live_calls: int = 0
    cached_calls: int = 0
    dry_calls: int = 0
    model_seconds: float = 0.0
    estimated_prompt_tokens: int = 0
    prompts: list[dict] = field(default_factory=list)


class LLMCache:
    """One JSON file per call under <cache>/llm/<key[:2]>/<key>.json."""

    def __init__(self, cache_dir: str | Path):
        self.root = Path(cache_dir) / "llm"

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, key: str, entry: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, path)


def _usage_of(details) -> dict:
    usage = {name: int(getattr(details, name, 0) or 0) for name in _USAGE_FIELDS}
    usage["total_cost_usd"] = str(getattr(details, "total_cost_usd", Decimal(0)))
    usage["model_costs_usd"] = {model: str(cost) for model, cost in getattr(details, "model_costs_usd", {}).items()}
    return usage


def replay_usage(usage: dict) -> None:
    """Add a stored call's counters to the current run, as if the call had just happened."""
    details = get_run_details()
    if details is None or not usage:
        return
    for name in _USAGE_FIELDS:
        setattr(details, name, getattr(details, name) + int(usage.get(name, 0) or 0))
    details.total_cost_usd += Decimal(str(usage.get("total_cost_usd", "0")))
    for model, cost in (usage.get("model_costs_usd") or {}).items():
        details.model_costs_usd[model] = details.model_costs_usd.get(model, Decimal(0)) + Decimal(str(cost))


class _HarnessHandler(BaseAiHandler):
    def __init__(self, *, stats: CallStats | None = None, inner_factory=LiteLLMAIHandler):
        self.stats = stats if stats is not None else CallStats()
        self._inner_factory = inner_factory
        self._inner = None

    @property
    def deployment_id(self):
        return None

    def _inner_handler(self):
        if self._inner is None:
            self._inner = self._inner_factory()
        return self._inner

    def __getattr__(self, name):
        if name in _FORWARDED_HOOKS:
            hook = getattr(self._inner_handler(), name, None)
            if hook is not None:
                return hook
        raise AttributeError(name)


class CachingAIHandler(_HarnessHandler):
    def __init__(self, *, cache: LLMCache, stats: CallStats | None = None, inner_factory=LiteLLMAIHandler):
        super().__init__(stats=stats, inner_factory=inner_factory)
        self.cache = cache

    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2,
                              img_path: str = None):
        key = cache_key(model, system, user, temperature, img_path)
        entry = self.cache.get(key)
        if entry is not None:
            self.stats.cached_calls += 1
            self.stats.model_seconds += float(entry.get("latency_seconds", 0.0))
            replay_usage(entry.get("usage") or {})
            return entry["response"], entry.get("finish_reason")

        inner = self._inner_handler()
        if hasattr(self, "main_pr_language"):
            inner.main_pr_language = self.main_pr_language

        async def call():
            # A child task gets its own collector, so this call's usage is measured on its own
            # and concurrent calls of the same command cannot mix into it.
            details = init_run_details()
            started = time.monotonic()
            response, finish_reason = await inner.chat_completion(model=model, system=system, user=user,
                                                                  temperature=temperature, img_path=img_path)
            return response, finish_reason, time.monotonic() - started, details

        response, finish_reason, latency, details = await asyncio.create_task(call())
        usage = _usage_of(details)
        self.stats.live_calls += 1
        self.stats.model_seconds += latency
        self.cache.put(key, {"model": model, "temperature": temperature, "response": response,
                             "finish_reason": finish_reason, "usage": usage, "latency_seconds": latency})
        replay_usage(usage)
        return response, finish_reason


class DryRunAIHandler(_HarnessHandler):
    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2,
                              img_path: str = None):
        try:
            tokens = len(TokenEncoder.get_token_encoder().encode(f"{system}\n{user}", disallowed_special=()))
        except Exception:
            tokens = (len(system or "") + len(user or "")) // 4
        self.stats.dry_calls += 1
        self.stats.estimated_prompt_tokens += tokens
        self.stats.prompts.append({"model": model, "temperature": temperature, "estimated_tokens": tokens,
                                   "system": system, "user": user})
        return dry_run_answer(system), "stop"
