from __future__ import annotations

import json
import re
from dataclasses import dataclass

from backend.llm.base import LLMAdapter, LLMMessage


@dataclass(frozen=True)
class PlanStep:
    title: str
    instruction: str


@dataclass(frozen=True)
class ExecutionPlan:
    summary: str
    steps: tuple[PlanStep, ...]


async def build_execution_plan(
    llm: LLMAdapter,
    user_message: str,
    *,
    max_steps: int = 4,
) -> ExecutionPlan:
    prompt = (
        "You are planning an agent workflow.\n"
        "Break the request into 2-4 concrete execution steps.\n"
        "Return valid JSON with this exact shape:\n"
        '{"summary":"short summary","steps":[{"title":"step title","instruction":"what to do"}]}\n'
        "Rules:\n"
        "- Keep steps ordered and action-oriented.\n"
        "- Keep titles short.\n"
        "- Keep instructions specific enough for an implementation agent.\n"
        f"- Do not exceed {max_steps} steps.\n\n"
        f"User request:\n{user_message}"
    )
    raw = await llm.simple_chat([LLMMessage(role="user", content=prompt)])
    payload = _parse_plan_payload(raw)

    summary = str(payload.get("summary", "")).strip() or "Execute the request in ordered steps."
    raw_steps = payload.get("steps", [])
    steps: list[PlanStep] = []
    if isinstance(raw_steps, list):
        for item in raw_steps[:max_steps]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            instruction = str(item.get("instruction", "")).strip()
            if title and instruction:
                steps.append(PlanStep(title=title, instruction=instruction))

    if not steps:
        steps = _fallback_steps(user_message, max_steps=max_steps)

    return ExecutionPlan(summary=summary, steps=tuple(steps[:max_steps]))


def render_execution_plan(plan: ExecutionPlan) -> str:
    lines = ["Execution plan", "", plan.summary.strip()]
    for index, step in enumerate(plan.steps, start=1):
        lines.append("")
        lines.append(f"{index}. {step.title}")
        lines.append(f"   {step.instruction}")
    return "\n".join(lines).strip()


def _parse_plan_payload(raw: str) -> dict[str, object]:
    text = raw.strip()
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1).strip()

    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            payload = json.loads(match.group(0))
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass

    return {}


def _fallback_steps(user_message: str, *, max_steps: int) -> list[PlanStep]:
    fragments = [
        line.strip(" -*\t")
        for line in user_message.splitlines()
        if line.strip()
    ]
    if len(fragments) >= 2:
        derived = [
            PlanStep(
                title=_short_title(fragment, index),
                instruction=fragment,
            )
            for index, fragment in enumerate(fragments[:max_steps], start=1)
        ]
        if derived:
            return derived

    return [
        PlanStep(
            title="Analyze request",
            instruction="Inspect the request and identify the concrete implementation work.",
        ),
        PlanStep(
            title="Execute changes",
            instruction="Apply the required backend or frontend changes for the request.",
        ),
        PlanStep(
            title="Verify outcome",
            instruction="Check the result and summarize the final outcome for the user.",
        ),
    ][:max_steps]


def _short_title(fragment: str, index: int) -> str:
    cleaned = re.sub(r"^\d+[.)]\s*", "", fragment).strip()
    if not cleaned:
        return f"Step {index}"
    words = cleaned.split()
    if len(words) <= 5:
        return cleaned[:60]
    return " ".join(words[:5])[:60]
