"""--strict: a model failure is reported, never answered by the rule-based fallback.

`warroom serve --strict` / `demo --strict` build the Runtime with allow_fallback=False, which also sets
Settings.llm_fallback_to_mock=False. The commander, 問戰情室 and plan discussion then surface the
model's timeout / error / invalid output with the reason below instead of a rule plan or rule answer.
"""
from __future__ import annotations

STRICT_NOTE = "（strict 模式不使用規則備援）"


class TruncatedOutput(ValueError):
    """The model stopped at max_tokens (finish_reason == "length"): the JSON is cut, not malformed."""


class PromptTooLong(ValueError):
    """The commander's request does not fit the model's context window even after trimming the per-event context
    (issue #33), so nothing was sent. Carries the estimate and the trim steps already applied, for the audit."""

    def __init__(self, message: str, estimate: int | None = None, trimmed: list[str] | None = None):
        super().__init__(message)
        self.estimate = estimate
        self.trimmed = list(trimmed or [])


def failure_cause(e: BaseException) -> str:
    """timeout | truncated | too_long | invalid_output | error, for the LLM_CALL audit record."""
    if isinstance(e, TruncatedOutput):
        return "truncated"
    if isinstance(e, PromptTooLong):
        return "too_long"
    name, msg = type(e).__name__.lower(), str(e).lower()
    if "timeout" in name or "timed out" in msg:
        return "timeout"
    if name in ("validationerror", "jsondecodeerror", "valueerror"):
        return "invalid_output"
    return "error"


def strict_reason(e: BaseException, what: str = "計劃") -> str:
    """Plain Chinese reason shown on the timeline / reply, e.g. 模型逾時，未產生計劃（strict 模式不使用規則備援）."""
    head = {"timeout": "模型逾時", "truncated": "模型輸出超過長度上限", "too_long": "提示超過模型的 context 上限",
            "invalid_output": "模型輸出無效"}.get(failure_cause(e), f"模型呼叫失敗（{type(e).__name__}）")
    return f"{head}，未產生{what}{STRICT_NOTE}"
