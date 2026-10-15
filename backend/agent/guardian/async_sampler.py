"""Source Snapshot Luna sampling, pooled transports and owned first-delta drain."""
from __future__ import annotations
import asyncio
import json
from copy import deepcopy
from collections import deque
from contextlib import aclosing
from dataclasses import replace
from uuid import uuid4

import httpx

from backend.agent.guardian.async_config import CLASSIFIER_MODEL
from backend.agent.guardian.connection_pool import GuardianClientSetup, GuardianConnectionPool, _context_metadata, new_classification_id
from backend.llm.base import LLMSideCallContext, SideQueryOptions, _estimate_responses_window_tokens
from backend.llm.openai_usage import usage_info_from_openai
import time
from backend.services.llm_adapter_factory import create_session_llm
from backend.llm.openai_adapter import OpenAIAdapter


class GuardianSamplingError(RuntimeError):
    def __init__(self, kind, message, *, retryable=False):
        self.kind = kind
        self.retryable = retryable
        super().__init__(message)


def _response_failure(event):
    response = event.get("response") or {}
    error = event.get("error") or response.get("error") or {}
    message = "Luna Responses request failed: " + str(error.get("message", event["type"]))
    if event["type"] == "response.incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason", "unknown")
        return GuardianSamplingError("api", f"Incomplete response returned, reason: {reason}", retryable=True)
    # Source sse/responses_error.rs maps unknown response.failed codes to
    # Retryable, while these explicit policy/budget failures cannot be retried.
    non_retryable = {"context_length_exceeded", "insufficient_quota", "credit_balance_exhausted",
        "organization_spend_limit_exceeded", "project_spend_limit_exceeded", "usage_not_included",
        "cyber_policy", "bio_policy", "misalignment_policy_violation", "invalid_prompt"}
    retryable = event["type"] == "response.failed" and error.get("code") not in non_retryable
    return GuardianSamplingError("api", message, retryable=retryable)


def classifier_backend_route(parent_snapshot):
    runtime = parent_snapshot.model_runtime
    model = parent_snapshot.model_info
    if runtime is None or model is None or not runtime.history_notes_backend_enabled(model):
        return False
    auth = runtime.token_budget_auth_context(model)
    return bool(auth.supports_codex_backend_routes and auth.requires_openai_auth and not (
        auth.has_env_key or auth.has_experimental_bearer_token or auth.has_auth or auth.has_aws))


class LunaSampler:
    def __init__(self, parent_snapshot, *, conversation_id, llm_factory=create_session_llm):
        self.parent_snapshot = parent_snapshot
        self.conversation_id = conversation_id
        info = parent_snapshot.model_runtime.get_model(parent_snapshot.provider, CLASSIFIER_MODEL) if parent_snapshot.model_runtime is not None else None
        self.model_info = info
        self.compaction_hash = info.comp_hash if info is not None else None
        supported = int(info.context_window) if info is not None and info.context_window else 128000
        percent = int(info.extra.get("effective_context_window_percent", 95)) if info is not None else 100
        self.max_input_tokens = supported * percent // 100
        # The classifier uses its own model allowance, not the parent override.
        cfg = replace(parent_snapshot.config, llm=replace(parent_snapshot.config.llm, model=CLASSIFIER_MODEL, reasoning_effort="low", comp_hash=self.compaction_hash))
        self.llm = llm_factory(cfg, model_override=CLASSIFIER_MODEL, provider_override=parent_snapshot.provider,
            model_runtime=parent_snapshot.model_runtime)
        if not isinstance(self.llm, OpenAIAdapter):
            raise GuardianSamplingError("provider", "Guardian Luna classification requires the issuing Responses provider")
        self._config = cfg
        self._llm_factory = llm_factory
        self.adapters = [self.llm]
        self.connections = GuardianConnectionPool(self._client_setup)
        self.active = deque()
        self.scored = set()
        self.tasks = set()
        self.cancel_watchers = set()
        self.closed = False
        self.audit = []

    async def _client_setup(self, context, thread_id):
        runtime = self.parent_snapshot.model_runtime
        if self.llm.provider_adapter_spec is not None:
            await runtime.refresh_provider_auth(self.parent_snapshot.provider)
            spec = runtime.resolve_adapter_spec(self.parent_snapshot.provider, CLASSIFIER_MODEL)
            if spec != self.llm.provider_adapter_spec:
                adapter = self._llm_factory(self._config, model_override=CLASSIFIER_MODEL,
                    provider_override=self.parent_snapshot.provider, model_runtime=runtime)
                if not isinstance(adapter, OpenAIAdapter):
                    raise GuardianSamplingError("provider", "Guardian Luna classification requires the issuing Responses provider")
                self.llm = adapter
                self.adapters.append(adapter)
        uses_backend = classifier_backend_route(self.parent_snapshot)
        headers = {"session_id": context.session_id, "thread_id": thread_id,
            "x-openai-subagent": "guardian", "x-codex-window-id": f"{thread_id}:0",
            "x-openai-internal-codex-responses-lite": "true", "x-client-request-id": thread_id}
        if uses_backend:
            headers["x-codex-guardian"] = "classifier"
        endpoint = await self.llm.guardian_classification_connection_setup(headers=headers, metadata=_context_metadata(context))
        attribution = {"session_id", "thread_id", "x-openai-subagent", "x-codex-window-id",
            "x-openai-internal-codex-responses-lite", "x-client-request-id", "x-codex-guardian"}
        key = (endpoint["url"], endpoint["proxy"], context.session_id, uses_backend,
            tuple(sorted((name.lower(), value) for name, value in endpoint["headers"].items() if name.lower() not in attribution)))
        return GuardianClientSetup(self.llm, endpoint["url"], endpoint["proxy"], endpoint["headers"], key, uses_backend)

    async def prewarm(self, context):
        await self.connections.prewarm(context)

    def supports_parent_compaction(self, model_hash):
        return bool(model_hash and model_hash == self.compaction_hash)

    @staticmethod
    def prepare_input(evidence, *, instructions, checkpoint=None, history=None):
        items = list(history) if history is not None else [
            {"type": "additional_tools", "role": "developer", "tools": []},
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": instructions}]}]
        if history is None and checkpoint is not None:
            items.append(deepcopy(checkpoint.item))
        suffix = deepcopy(evidence)
        for item in suffix:
            if item["type"] == "message":
                for part in item["content"]:
                    if part["type"] == "input_image":
                        part.pop("detail", None)
        items.extend(suffix)
        prefixes = {"additional_tools": "at", "message": "msg", "agent_message": "amsg", "reasoning": "rs",
            "local_shell_call": "lsh", "function_call": "fc", "tool_search_call": "tsc", "function_call_output": "fco",
            "custom_tool_call": "ctc", "custom_tool_call_output": "ctco", "tool_search_output": "tso",
            "web_search_call": "ws", "image_generation_call": "ig", "compaction": "cmp", "context_compaction": "cmp"}
        for item in items:
            prefix = prefixes.get(item["type"])
            if item.get("id") is None and prefix is not None:
                item["id"] = f"{prefix}_{new_classification_id()}"
        return items

    async def sample_retained(self, prepared, *, ready, instructions, context, reasoning_effort="low"):
        from backend.agent.guardian.async_request import AnnotatedItem
        try:
            output = await self._execute(ready, (), instructions, context, None, reasoning_effort,
                prepared_items=[item.item for item in prepared.input], retained=True)
        except (GuardianSamplingError, httpx.HTTPError, json.JSONDecodeError):
            return None
        return [*prepared.input, *(AnnotatedItem(item) for item in output)] if output is not None else None

    async def sample(self, input_items, *, instructions, context, parent_compaction=None, reasoning_effort="low"):
        if self.closed:
            raise GuardianSamplingError("superseded", "Guardian classifier has stopped")
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        if len(self.active) == 16:
            oldest = next((task for task in self.active if task in self.scored), self.active[0])
            self.active.remove(oldest)
            oldest.cancel()
        task = asyncio.create_task(self._execute(ready, input_items, instructions, context, parent_compaction, reasoning_effort))
        self.active.append(task)
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        if context.cancel_event is not None:
            watcher = asyncio.create_task(context.cancel_event.wait())
            self.cancel_watchers.add(watcher)
            watcher.add_done_callback(self.cancel_watchers.discard)
            watcher.add_done_callback(lambda completed: task.cancel() if not completed.cancelled() else None)
            task.add_done_callback(lambda _: watcher.cancel())
        try:
            return await ready
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    def _finished(self, task):
        self.tasks.discard(task)
        self.scored.discard(task)
        if task in self.active:
            self.active.remove(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.audit.append({"status": "drain_failed", "error": str(error)})

    async def _execute(self, ready, evidence, instructions, context, checkpoint, effort, *, prepared_items=None, retained=False):
        turn_id = new_classification_id()
        items = prepared_items if prepared_items is not None else self.prepare_input(evidence, instructions=instructions, checkpoint=checkpoint)
        tokens = _estimate_responses_window_tokens(items)
        turn = context.run_context.llm_turn_context if context.run_context is not None else None
        record = {"id": uuid4().hex, "operation": "guardian_classifier", "query_source": "guardian_classifier",
            "provider": self.parent_snapshot.provider, "model": CLASSIFIER_MODEL, "status": "running", "attempts": 0,
            "parent_turn_id": context.turn_id}
        if turn is not None:
            turn.side_call_records.append(record)
        call_context = LLMSideCallContext(SideQueryOptions(operation="guardian_classifier", turn_id=turn_id,
            query_source="guardian_classifier", max_retries=2), record, turn)
        started = time.monotonic()
        try:
            if tokens > self.max_input_tokens - 256:
                raise GuardianSamplingError("input_budget", "Luna input exceeds the complete request budget")
            for attempt in range(3):
                try:
                    lease = await self.connections.lease(context)
                    async with lease:
                        record["attempts"] = attempt + 1
                        record.setdefault("transports", []).append(lease.mode)
                        classifier_thread = lease.thread_id
                        client_metadata = {"session_id": context.session_id, "thread_id": classifier_thread, "turn_id": turn_id,
                            "parent_turn_id": context.turn_id, "x-openai-subagent": "guardian", "x-codex-window-id": f"{classifier_thread}:0",
                            "ws_request_header_x_openai_internal_codex_responses_lite": "true"}
                        turn_metadata = {"session_id": context.session_id, "thread_id": classifier_thread,
                            "guardian_classifier_source_thread_id": self.conversation_id, "turn_id": turn_id,
                            "parent_turn_id": context.turn_id, "thread_source": "guardian_classifier", "turn_trigger": "guardian_classifier"}
                        root_turn = context.metadata.get("root_turn_id")
                        if root_turn is not None:
                            client_metadata["root_turn_id"] = root_turn
                            turn_metadata["root_turn_id"] = root_turn
                        client_metadata["x-codex-turn-metadata"] = json.dumps(turn_metadata, separators=(",", ":"))
                        request = {"model": CLASSIFIER_MODEL, "input": items, "tool_choice": "none", "parallel_tool_calls": False,
                            "reasoning": {"effort": effort, "context": "all_turns"}, "store": False, "stream": True,
                            "include": [], "prompt_cache_key": f"guardian-v2:{self.conversation_id}", "client_metadata": client_metadata}
                        if retained:
                            request["include"] = ["reasoning.encrypted_content"]
                        if lease.classifier_route:
                            if context.metadata.get("parent_response_id") is not None:
                                client_metadata["parent_response_id"] = context.metadata["parent_response_id"]
                        else:
                            request_policy = self.parent_snapshot.config.model_request_policy
                            if request_policy is None:
                                request_policy = self.parent_snapshot.llm.model_request_policy()
                            tier = request_policy.service_tier_for_request(request_policy.service_tier)
                            if tier is not None:
                                request["service_tier"] = tier
                        output = ""
                        retained_output = []
                        remaining_tokens = self.max_input_tokens - 256 - tokens
                        early_score = None
                        events = lease.events(request,
                            metadata={**call_context.request_metadata(), "query_source": "guardian_classifier", "parent_turn_id": context.turn_id}, cancel_event=context.cancel_event)
                        async with aclosing(events):
                            async for event in events:
                                kind = event.get("type")
                                if kind == "response.output_text.delta" and event.get("delta") and (early_score is None if retained else not ready.done()):
                                    delta = event["delta"]
                                    if len(delta.encode("utf-8")) > 8192:
                                        raise GuardianSamplingError("output_too_large", "Luna response exceeded the output limit")
                                    early_score = delta
                                    if not retained:
                                        self.scored.add(asyncio.current_task())
                                    if not ready.done():
                                        ready.set_result(delta)
                                elif kind == "response.output_item.done":
                                    item = event["item"]
                                    if retained and retained_output is not None:
                                        valid = ((item.get("type") == "message" and item.get("role") == "assistant"
                                            and all(part["type"] == "output_text" for part in item["content"]))
                                            or item.get("type") == "reasoning" and bool(item.get("encrypted_content")))
                                        item_tokens = _estimate_responses_window_tokens([item])
                                        if not valid or item_tokens > 10000 or item_tokens > remaining_tokens:
                                            retained_output = None
                                        else:
                                            remaining_tokens -= item_tokens
                                            retained_output.append(deepcopy(item))
                                    if item.get("type") == "message" and item.get("role") == "assistant":
                                        output += "".join(part["text"] for part in item["content"] if part["type"] == "output_text")
                                        if output and not retained:
                                            self.scored.add(asyncio.current_task())
                                        if len(output.encode("utf-8")) > 8192:
                                            raise GuardianSamplingError("output_too_large", "Luna response exceeded the output limit")
                                elif kind == "response.completed" or (kind == "response.incomplete"
                                        and (event["response"].get("incomplete_details") or {}).get("reason") == "interrupted"):
                                    raw_usage = event["response"].get("usage")
                                    if raw_usage is not None:
                                        lease.setup.adapter.record_non_stream_usage(usage_info_from_openai(raw_usage), provider=self.parent_snapshot.provider,
                                            model_id=CLASSIFIER_MODEL, input_includes_cache_read=True, context=call_context, raw_usage=raw_usage)
                                    record["status"] = "completed"
                                    if kind == "response.incomplete":
                                        record["interrupted"] = True
                                    lease.completed = True
                                    if not ready.done():
                                        if not output:
                                            raise GuardianSamplingError("missing_output", "Luna response did not contain assistant output")
                                        ready.set_result(output)
                                    if retained:
                                        return retained_output if (retained_output and output in {"high", "low"}
                                            and (early_score is None or early_score == output)) else None
                                    return
                                elif kind in {"response.failed", "response.incomplete", "error", "response.error"}:
                                    record["provider_error"] = deepcopy(event)
                                    raise _response_failure(event)
                        raise GuardianSamplingError("missing_output", "Luna response did not contain assistant output")
                except (GuardianSamplingError, httpx.TransportError, httpx.HTTPStatusError) as error:
                    retry = error.retryable if isinstance(error, GuardianSamplingError) else (
                        isinstance(error, httpx.TransportError) or error.response.status_code == 429 or error.response.status_code >= 500)
                    if ready.done() or not retry or attempt == 2:
                        raise
        except asyncio.CancelledError:
            record["status"] = "cancelled"
            if not ready.done():
                ready.set_exception(GuardianSamplingError("superseded", "Luna request was superseded by a newer classification"))
            raise
        except Exception as error:
            record["status"] = "failed"
            record["error"] = str(error)
            if not ready.done():
                ready.set_exception(error)
            raise
        finally:
            record["duration_ms"] = int((time.monotonic() - started) * 1000)
            self.audit.append(record)

    async def aclose(self):
        self.closed = True
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        watchers = tuple(self.cancel_watchers)
        for watcher in watchers:
            watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)
        await self.connections.aclose()
        for adapter in self.adapters:
            await adapter.aclose()
