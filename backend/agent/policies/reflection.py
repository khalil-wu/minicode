"""Reflection policy: protocol and default implementation.

Defines ReflectionDecision, ReflectionPolicy (Protocol),
and DefaultReflectionPolicy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReflectionDecision:
    """Structured result of a reflection pass.

    verdict: "lgtm" means the draft is acceptable; "revise" means addendum
    should be appended to the reply.
    addendum: additional text to append when verdict is "revise".
    """

    verdict: Literal["lgtm", "revise"]
    addendum: str = ""

    def to_payload(self) -> str:
        """Serialize to a JSON string.

        Round-trip invariant:
            from_payload(d.to_payload()) == d for all valid d.
        """
        return json.dumps({"verdict": self.verdict, "addendum": self.addendum})

    @classmethod
    def from_payload(cls, payload: str) -> "ReflectionDecision":
        """Parse a JSON payload. Raises ValueError on malformed input.

        DefaultReflectionPolicy catches that ValueError and returns a
        no-op ReflectionDecision instead of raising.
        """
        stripped = payload.strip()
        data = json.loads(stripped)
        verdict = data.get("verdict")
        if verdict not in ("lgtm", "revise"):
            raise ValueError(f"invalid verdict: {verdict!r}")
        addendum = data.get("addendum", "")
        if not isinstance(addendum, str):
            raise ValueError("addendum must be a string")
        return cls(verdict=verdict, addendum=addendum)

    @classmethod
    def from_text(cls, payload: str) -> "ReflectionDecision":
        """Accept both structured JSON and legacy plain-text reviewer replies."""
        stripped = payload.strip()
        if not stripped:
            return cls(verdict="lgtm", addendum="")

        try:
            return cls.from_payload(stripped)
        except ValueError:
            lowered = stripped.lower()
            if lowered in {"lgtm", "looks good", "looks good to me"}:
                return cls(verdict="lgtm", addendum="")
            return cls(verdict="revise", addendum="")


class ReflectionPolicy(Protocol):
    """Protocol for reflection policies.

    A reflection policy examines the draft reply and optionally suggests
    a revision addendum via a single LLM call.
    """

    async def maybe_reflect(
        self,
        user_message: str,
        draft_reply: str,
        state: Any,
        llm: Any,
    ) -> ReflectionDecision | None: ...


def _build_reflection_tool_context(state: Any, limit: int = 5) -> str:
    """Build a summary of recent tool results for the reflection prompt.

    Produces one line per tool call, truncated to 240 chars per summary.
    Same shape as the helper previously inlined in loop.py.
    """
    lines: list[str] = []
    for tc in state.tool_calls[-limit:]:
        output = (tc.tool_output or "").strip().replace("\n", " ")
        if len(output) > 240:
            output = f"{output[:240]}..."
        lines.append(f"- {tc.tool_name} [{tc.status}]: {output}")
    return "\n".join(lines) if lines else "(none)"


class DefaultReflectionPolicy:
    """Default reflection policy: single-LLM-call structured JSON reflection.

    Off by default (returns None when settings.reflection_pass is False).
    When on, issues exactly one LLM call per draft and parses the response
    as a structured {verdict, addendum} JSON payload. On parse failure or
    LLM exception, degrades to a no-op decision (verdict="lgtm") — never raises.
    """

    def __init__(self, settings: Any) -> None:
        """Constructor injection of AgentSettings reference."""
        self._settings = settings

    async def maybe_reflect(
        self,
        user_message: str,
        draft_reply: str,
        state: Any,
        llm: Any,
    ) -> ReflectionDecision | None:
        """Evaluate the draft reply and optionally suggest a revision.

        Returns None (no reflection performed) when:
        - settings.reflection_pass is False
        - draft_reply is empty after stripping whitespace

        Otherwise issues exactly one LLM call and returns a ReflectionDecision.
        """
        if not self._settings.reflection_pass:
            return None

        if not draft_reply.strip():
            return None

        tool_context = _build_reflection_tool_context(state)

        prompt = (
            "You are a reflection assistant. Review the draft reply below and decide "
            "whether it is acceptable or needs revision.\n\n"
            "Respond with ONLY a JSON object in this exact format:\n"
            '{"verdict": "lgtm", "addendum": ""}\n'
            "or\n"
            '{"verdict": "revise", "addendum": "<text to append>"}\n\n'
            "Rules:\n"
            '- verdict must be exactly "lgtm" or "revise"\n'
            '- If verdict is "lgtm", addendum must be ""\n'
            '- If verdict is "revise", addendum contains the correction or addition to append\n'
            "- Do not repeat the draft in the addendum; only include new/corrected content\n\n"
            f"User request:\n{user_message}\n\n"
            f"Draft reply:\n{draft_reply}\n\n"
            f"Recent tool results:\n{tool_context}"
        )

        try:
            from backend.llm.base import LLMMessage

            response = await llm.simple_chat([LLMMessage(role="user", content=prompt)])
            first_decision = ReflectionDecision.from_text(response)
            if first_decision.verdict != "revise" or first_decision.addendum:
                return first_decision

            fix_prompt = (
                "The review found an issue but did not provide an addendum. "
                "Write only the concise text that should be appended to the draft reply.\n\n"
                f"User request:\n{user_message}\n\n"
                f"Draft reply:\n{draft_reply}\n\n"
                f"Review finding:\n{response.strip()}"
            )
            addendum = (await llm.simple_chat([LLMMessage(role="user", content=fix_prompt)])).strip()
            if addendum and not addendum.startswith("\n"):
                addendum = f"\n\n{addendum}"
            return ReflectionDecision(verdict="revise", addendum=addendum)
        except Exception as exc:
            logger.warning("reflection.parse_failed: %s", exc)
            return ReflectionDecision(verdict="lgtm", addendum="")


_MUTATION_TOOL_NAMES = {"write_file", "edit_file", "run_command"}
_WEB_TOOL_NAMES = {"web_search", "web_fetch"}


def _turn_is_high_risk(state: Any) -> bool:
    """A turn is high-risk when it mutated the workspace or made web claims.

    These are exactly the drafts where an unverified correctness/grounding
    error is most expensive: a false "I edited the file" or a confident claim
    built on a stale snippet. Low-risk conversational turns skip the heavier
    adversarial pass and fall back to the single-pass review.
    """
    for tc in getattr(state, "tool_calls", None) or []:
        name = getattr(tc, "tool_name", "")
        status = getattr(tc, "status", "")
        if name in _MUTATION_TOOL_NAMES and status == "success":
            return True
        if name in _WEB_TOOL_NAMES and status == "success":
            return True
    return False


class MultiPerspectiveReflectionPolicy:
    """Reflection that adversarially reviews high-risk drafts only.

    For high-risk turns (a successful mutation or web lookup happened) it runs
    a single adversarial review along two named dimensions — correctness and
    grounding — and only revises when the reviewer reports a concrete defect.
    Low-risk conversational turns skip reflection entirely, so the default-on
    configuration adds zero cost outside the risky case.

    Gated by ``settings.reflection_pass``. Never raises: any LLM/parse failure
    degrades to a no-op ``lgtm`` decision.
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    async def maybe_reflect(
        self,
        user_message: str,
        draft_reply: str,
        state: Any,
        llm: Any,
    ) -> ReflectionDecision | None:
        if not self._settings.reflection_pass:
            return None
        if not draft_reply.strip():
            return None
        if not _turn_is_high_risk(state):
            # Low-risk conversational turn: skip reflection entirely. Charging an
            # extra LLM call to every chat turn is the wrong default; the
            # adversarial pass below is reserved for turns where an unverified
            # error is expensive (mutations / web claims).
            return None

        tool_context = _build_reflection_tool_context(state)
        prompt = (
            "You are an adversarial reviewer. Find concrete defects in the draft "
            "reply below along TWO dimensions, then decide if it must be revised.\n\n"
            "Dimensions:\n"
            "1. Correctness — does the reply actually satisfy the user's request? "
            "Are there wrong claims, missed requirements, or unsupported conclusions?\n"
            "2. Grounding — is every factual/mutation claim backed by a successful "
            "tool result above? Flag any 'I edited/created X' or confident fact that "
            "the tool results do not support.\n\n"
            "Respond with ONLY a JSON object in this exact format:\n"
            '{"verdict": "lgtm", "addendum": ""}\n'
            "or\n"
            '{"verdict": "revise", "addendum": "<concise correction to append>"}\n\n'
            "Rules:\n"
            '- verdict is exactly "lgtm" or "revise"\n'
            '- Only "revise" for a concrete, defensible defect — not style nits\n'
            '- addendum is the correction/caveat to append, never a repeat of the draft\n\n'
            f"User request:\n{user_message}\n\n"
            f"Draft reply:\n{draft_reply}\n\n"
            f"Recent tool results:\n{tool_context}"
        )

        try:
            from backend.llm.base import LLMMessage

            response = await llm.simple_chat([LLMMessage(role="user", content=prompt)])
            decision = ReflectionDecision.from_text(response)
            if decision.verdict != "revise" or decision.addendum:
                return decision

            fix_prompt = (
                "The review found an issue but did not provide an addendum. "
                "Write only the concise text that should be appended to the draft reply.\n\n"
                f"User request:\n{user_message}\n\n"
                f"Draft reply:\n{draft_reply}\n\n"
                f"Review finding:\n{response.strip()}"
            )
            addendum = (await llm.simple_chat([LLMMessage(role="user", content=fix_prompt)])).strip()
            if addendum and not addendum.startswith("\n"):
                addendum = f"\n\n{addendum}"
            return ReflectionDecision(verdict="revise", addendum=addendum)
        except Exception as exc:
            logger.warning("reflection.multi.parse_failed: %s", exc)
            return ReflectionDecision(verdict="lgtm", addendum="")
