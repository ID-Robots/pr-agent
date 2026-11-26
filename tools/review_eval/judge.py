"""The LLM judge: same-issue and validity verdicts through the caching handler, at temperature 0."""

from __future__ import annotations

from collections.abc import Callable

from tools.review_eval.scorer import (
    SAME_ISSUE_SYSTEM,
    VALIDITY_SYSTEM,
    parse_verdict,
    same_issue_prompt,
    validity_prompt,
)


class LLMJudge:
    def __init__(self, model: str, handler_factory: Callable):
        self.model = model
        self.handler_factory = handler_factory
        self.calls = 0
        self.errors = 0
        self.unparsed = 0

    async def _ask(self, system: str, user: str, field: str) -> bool | None:
        self.calls += 1
        try:
            answer, _ = await self.handler_factory().chat_completion(model=self.model, system=system, user=user,
                                                                     temperature=0.0)
        except Exception:
            self.errors += 1
            return None
        verdict = parse_verdict(answer, field)
        if verdict is None:
            self.unparsed += 1
        return verdict

    async def same_issue(self, reference: dict, finding: dict) -> bool | None:
        return await self._ask(SAME_ISSUE_SYSTEM, same_issue_prompt(reference, finding), "same_issue")

    async def is_valid(self, finding: dict, code_context: str) -> bool | None:
        return await self._ask(VALIDITY_SYSTEM, validity_prompt(finding, code_context), "valid")
