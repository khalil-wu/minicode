"""Source optional Decisions comparison: private measurements, never authorization."""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque

import httpx

from backend.agent.guardian.async_sampler import LunaSampler
from backend.llm.base import _estimate_responses_window_tokens
from backend.llm.proxy_policy import provider_proxy_url_for_base_url


URL = "https://api.openai.com/v1/decisions"
MODEL = "gpt-6-luna"
DEADLINE = 6.0
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024


class DecisionsError(RuntimeError):
    def __init__(self, kind, status=None):
        self.kind, self.status = kind, status
        # Source intentionally excludes credentials, request/response bodies,
        # and underlying transport text from comparison diagnostics.
        super().__init__(f"Decisions returned HTTP {status}" if kind == "http" else {
            "credentials": "Decisions credentials are missing or empty",
            "client_setup": "Decisions HTTP client setup failed",
            "unsupported_evidence": "Decisions cannot represent the complete classifier evidence",
            "input_too_large": "Decisions input exceeds the request limit",
            "transport_timeout": "Decisions request timed out",
            "decisions_transport": "Decisions transport failed",
            "response_too_large": "Decisions response exceeds the response limit",
            "invalid_output": "Decisions returned an invalid classification",
        }[kind])


def decisions_api_key(provider_id, base_url, *, read_env=os.environ.__getitem__, allow_openai_fallback=True):
    try:
        key = read_env("CODEX_GUARDIAN_DECISIONS_API_KEY")
    except KeyError:
        if not allow_openai_fallback or provider_id != "openai" or base_url is not None:
            raise DecisionsError("credentials") from None
        try:
            key = read_env("OPENAI_API_KEY")
        except KeyError:
            raise DecisionsError("credentials") from None
    if not key.strip():
        raise DecisionsError("credentials")
    return key


def request_body(instructions, evidence, checkpoint=None):
    if checkpoint is not None:
        raise DecisionsError("unsupported_evidence")
    rubric, messages, image_bytes = instructions, [], 0
    for item in evidence:
        if item.get("type") != "message":
            raise DecisionsError("unsupported_evidence")
        content = item["content"]
        # MiniCode has no host-owned TrustedTool attestation producer here.
        # A same-name wire/provider/MCP annotation cannot establish its authority.
        if item["role"] != "user":
            raise DecisionsError("unsupported_evidence")
        parts = []
        for part in content:
            if part["type"] == "input_text":
                parts.append({"type": "input_text", "text": part["text"]})
            elif part["type"] == "input_image" and part.get("image_url", "").startswith("data:"):
                image_bytes += len(part["image_url"].encode("utf-8"))
                if image_bytes > MAX_IMAGE_BYTES:
                    raise DecisionsError("input_too_large")
                parts.append({"type": "input_image", "image_url": part["image_url"], "detail": None})
            else:
                raise DecisionsError("unsupported_evidence")
        messages.append({"role": "user", "content": parts})
    return {"model": MODEL, "input": messages, "questions": [{"type": "choice", "name": "guardian_risk",
        "instructions": rubric, "choices": [{"value": "low"}, {"value": "high"}]}]}


def parse_answer(body):
    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, list) or len(answers) != 1:
        raise DecisionsError("invalid_output")
    answer = answers[0]
    if (not isinstance(answer, dict) or answer.get("type") != "choice" or answer.get("name") != "guardian_risk"
            or answer.get("choice") not in ("low", "high")):
        raise DecisionsError("invalid_output")
    return answer["choice"]


class DecisionsSampler:
    def __init__(self, api_key, *, url=URL, client=None, proxy_mode="inherit"):
        if not api_key.strip():
            raise DecisionsError("credentials")
        self.api_key, self.url = api_key, url
        self.owns_client = client is None
        if client is None:
            try:
                self.client = httpx.AsyncClient(proxy=provider_proxy_url_for_base_url(url, proxy_mode=proxy_mode) or None, trust_env=False)
            except (ValueError, httpx.InvalidURL):
                raise DecisionsError("client_setup") from None
        else:
            self.client = client
        self.slots = asyncio.Semaphore(16)
        self.active = deque()
        self.tasks = set()
        self.deadline = DEADLINE

    def spawn(self, instructions, evidence, *, checkpoint, max_input_tokens, unsupported=False):
        started = time.monotonic()
        try:
            if unsupported:
                raise DecisionsError("unsupported_evidence")
            body = request_body(instructions, evidence, checkpoint)
            items = LunaSampler.prepare_input(evidence, instructions=instructions, checkpoint=checkpoint)
            if _estimate_responses_window_tokens(items) > max_input_tokens - 256:
                raise DecisionsError("input_too_large")
        except DecisionsError as error:
            async def rejected(failure=error):
                return {"outcome": "skipped", "reason": failure.kind, "risk": None,
                    "duration_ms": int((time.monotonic() - started) * 1000)}
            task = asyncio.create_task(rejected())
        else:
            self.active = deque(task for task in self.active if not task.done())
            if len(self.active) == 16:
                self.active.popleft().cancel()
            task = asyncio.create_task(self._run(body, started))
            self.active.append(task)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _run(self, body, started):
        try:
            async with self.slots:
                async with asyncio.timeout(self.deadline):
                    risk = await self._request(body)
            return {"outcome": "success", "reason": "none", "risk": risk,
                "duration_ms": int((time.monotonic() - started) * 1000)}
        except DecisionsError as error:
            reason = (f"http_{error.status}" if error.status in {401, 403, 429} else
                "http_4xx" if error.status is not None and 400 <= error.status <= 499 else
                "http_5xx" if error.status is not None and 500 <= error.status <= 599 else
                "http_other" if error.status is not None else error.kind)
            return {"outcome": "failure", "reason": reason, "risk": None,
                "duration_ms": int((time.monotonic() - started) * 1000)}
        except TimeoutError:
            return {"outcome": "failure", "reason": "transport_timeout", "risk": None,
                "duration_ms": int((time.monotonic() - started) * 1000)}

    async def _request(self, body):
        try:
            async with self.client.stream("POST", self.url, headers={"Authorization": f"Bearer {self.api_key}"},
                    json=body, timeout=None) as response:
                if not response.is_success:
                    raise DecisionsError("http", response.status_code)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise DecisionsError("response_too_large")
                    data.extend(chunk)
                try:
                    answer = json.loads(data)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise DecisionsError("invalid_output") from None
                return parse_answer(answer)
        except httpx.HTTPError:
            raise DecisionsError("decisions_transport") from None

    async def aclose(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.owns_client:
            await self.client.aclose()


def comparison_record(decisions, baseline, duration_ms):
    risk = decisions["risk"]
    return {**decisions, "comparison": "unavailable" if baseline is None or risk is None else "agree" if baseline == risk else "disagree",
        "responses": baseline if baseline is not None else "unavailable",
        "decisions": risk if risk is not None else "unavailable",
        "responses_duration_ms": duration_ms if baseline is not None and risk is not None else None}
