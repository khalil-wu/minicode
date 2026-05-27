from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from backend.llm.base import LLMAdapter, LLMMessage

_DEFAULT_MAXSIZE = 128
_DEFAULT_TTL_SECONDS = 3600.0


@dataclass
class _CacheEntry:
    value: str
    expires_at: float


class LLMResponseCache:
    def __init__(
        self,
        *,
        maxsize: int = _DEFAULT_MAXSIZE,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._maxsize = maxsize
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()

    async def simple_chat(self, llm: LLMAdapter, messages: list[LLMMessage]) -> str:
        key = self._build_key(llm, messages)
        cached = self.get(key)
        if cached is not None:
            return cached

        response = (await llm.simple_chat(messages)).strip()
        if response:
            self.set(key, response)
        return response

    def get(self, key: str) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return entry.value

    def set(self, key: str, value: str) -> None:
        self._entries[key] = _CacheEntry(
            value=value,
            expires_at=time.monotonic() + self._ttl_seconds,
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)

    def _build_key(self, llm: LLMAdapter, messages: list[LLMMessage]) -> str:
        fingerprint = {
            "model": self._model_id(llm),
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                    "name": message.name,
                    "tool_call_id": message.tool_call_id,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": tool_call.arguments,
                        }
                        for tool_call in (message.tool_calls or [])
                    ],
                }
                for message in messages
            ],
        }
        raw = json.dumps(fingerprint, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _model_id(self, llm: LLMAdapter) -> str:
        settings = getattr(llm, "_settings", None)
        model = getattr(settings, "model", None)
        if isinstance(model, str) and model:
            return model

        direct_model = getattr(llm, "_model", None)
        if isinstance(direct_model, str) and direct_model:
            return direct_model

        adapters = getattr(llm, "adapters", None) or getattr(llm, "_adapters", None)
        if adapters:
            return "fallback:" + ",".join(self._model_id(adapter) for adapter in adapters)

        return type(llm).__name__


simple_chat_cache = LLMResponseCache()
