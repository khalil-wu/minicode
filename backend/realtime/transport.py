"""Source V1/V2/V3 WebSocket and WebRTC/existing-call sideband consumers."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from backend.llm.proxy_policy import provider_httpx_proxy_kwargs, provider_proxy_url_for_base_url
from backend.agent.guardian.truncation import truncate_guardian_text
from backend.realtime.protocol import Start, V1_VOICES, V2_VOICES, context_chunks, input_text, text_message
from backend.application_network import application_network_policy, application_http_client, application_websocket_connect


def websocket_url(base: str, version: str, model: str | None = None, call_id: str | None = None) -> str:
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise ValueError("unsupported realtime endpoint scheme")
    path = parsed.path
    if version == "v3":
        if path in {"", "/", "/v1", "/v1/"}:
            path = "/v1/live"
        elif path.rstrip("/").endswith("/realtime"):
            path = path.rstrip("/")[:-len("realtime")] + "live"
        elif path.endswith("/live/"):
            path = path.rstrip("/")
    elif path in {"", "/"}:
        path = "/v1/realtime"
    elif path.endswith("/v1"):
        path += "/realtime"
    elif path.endswith("/v1/"):
        path += "realtime"
    elif path.endswith("/realtime/"):
        path = path.rstrip("/")
    query = list(parse_qsl(parsed.query))
    if version == "v1":
        query.append(("intent", "quicksilver"))
    if model is not None:
        query.append(("model", model))
    if call_id is not None:
        if version == "v3":
            if call_id in {".", ".."}:
                raise ValueError("invalid realtime call id")
            path = path.rstrip("/") + "/" + quote(call_id, safe="")
        else:
            query.append(("call_id", call_id))
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    return urlunsplit((scheme, parsed.netloc, path, urlencode(query), ""))


@dataclass(frozen=True)
class RealtimeConfig:
    base_url: str
    headers: dict[str, str] = field(repr=False)
    proxy_mode: str = "inherit"
    version: str = "v2"
    model: str | None = None
    voice: str | None = None
    session_type: str = "conversational"
    call_base_url: str | None = None
    enabled: bool = True
    prompt: str = ""
    configured: bool = True
    startup_context: str | None = None
    start_instructions: str | None = None
    network_policy: object = field(default_factory=application_network_policy, repr=False, compare=False)

    def __post_init__(self):
        if self.version not in {"v1", "v2", "v3"}:
            raise ValueError("realtime.version must be v1, v2 or v3")
        if self.session_type not in {"conversational", "transcription"}:
            raise ValueError("realtime.session_type must be conversational or transcription")

    def capabilities(self) -> dict:
        configured = self.configured
        transports = ["websocket"]
        if self.version in {"v1", "v3"}:
            if self.session_type == "conversational":
                transports.append("webrtc")
            transports.append("existingCall")
        return {"enabled": self.enabled, "configured": configured,
            "transports": transports if configured and self.enabled else [],
            "version": self.version, "model": self.model or ("gpt-live-1-codex" if self.version == "v3" else "gpt-realtime-1.5"),
            "inputSampleRate": 24000, "inputChannels": 1,
            "transportVersions": {"websocket": ["v1", "v2", "v3"],
                "webrtc": ["v1", "v3"] if self.session_type == "conversational" else [], "existingCall": ["v1", "v3"]}}


BACKGROUND_DESCRIPTION = "Send a user request to the background agent. Use this as the default action. Do not rephrase the user's ask or rewrite it in your own words; pass along the user's own words. If the background agent is idle, this starts a new task and returns the final result to the user. If the background agent is already working on a task, this sends the request as guidance to steer that previous task. If the user asks to do something next, later, after this, or once current work finishes, call this tool so the work is actually queued instead of merely promising to do it later."


def session_config(start: Start, config: RealtimeConfig, version: str, instructions: str) -> dict:
    voice = start.voice or config.voice or ("marin" if version == "v2" else "cove")
    allowed = V2_VOICES if version == "v2" else V1_VOICES
    if voice not in allowed:
        raise ValueError(f"voice {voice!r} is not supported by realtime {version}")
    if version == "v3":
        value = {"instructions": instructions, "audio": {"output": {"voice": voice}}, "delegation": {"type": "client"}}
        if start.delegation_ack_filler is not None:
            value["delegation"]["ack_filler"] = start.delegation_ack_filler
        if start.initial_items:
            value["initial_items"] = [{"type": "message", "role": item.role,
                "content": [{"type": "output_text" if item.role == "assistant" else "input_text", "text": item.text}]} for item in start.initial_items]
        return value
    audio = {"input": {"format": {"type": "audio/pcm", "rate": 24000}}, "output": {"voice": voice}}
    if version == "v1":
        return {"type": "quicksilver", "instructions": instructions, "audio": audio}
    if config.session_type == "transcription":
        return {"type": "transcription", "audio": {"input": {**audio["input"],
            "transcription": {"model": "gpt-4o-mini-transcribe"}, "turn_detection": {"type": "server_vad"}}}}
    audio["input"].update(noise_reduction={"type": "near_field"}, transcription={"model": "gpt-4o-mini-transcribe"},
        turn_detection={"type": "server_vad", "interrupt_response": True, "create_response": True, "silence_duration_ms": 500})
    audio["output"]["format"] = {"type": "audio/pcm", "rate": 24000}
    return {"type": "realtime", "instructions": instructions, "output_modalities": [start.output_modality], "audio": audio,
        "tools": [{"type": "function", "name": "background_agent", "description": BACKGROUND_DESCRIPTION,
            "parameters": {"type": "object", "properties": {"prompt": {"type": "string", "description": "The user request to delegate to the background agent."}}, "required": ["prompt"], "additionalProperties": False}},
            {"type": "function", "name": "remain_silent", "description": "Call this when the best response is to say nothing. Use it instead of speaking after hidden system/control messages, after background agent updates in silent modes, or whenever acknowledging aloud would be distracting. This tool has no user-visible effect.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}], "tool_choice": "auto"}


class RealtimeConnection:
    def __init__(self, config: RealtimeConfig, start: Start, instructions: str):
        self.config, self.start = config, start
        self.version = start.version or (config.version if start.transport.type == "websocket" else "v1")
        if not config.enabled:
            raise ValueError("realtime_conversation is disabled")
        if start.output_modality == "text" and self.version != "v2":
            raise ValueError("text realtime output modality requires realtime v2")
        if self.version != "v3" and start.initial_items:
            raise ValueError("initial realtime items require realtime v3")
        if start.initial_items and (len(start.initial_items) > 128 or sum((len(item.text.encode()) + 3) // 4 for item in start.initial_items) > 8192):
            raise ValueError("initial realtime items exceed 128 items or 8192 estimated tokens")
        if start.transport.type != "websocket" and self.version == "v2":
            raise ValueError("AVAS realtime calls require realtime v1 or v3")
        if start.transport.type == "webrtc" and config.session_type != "conversational":
            raise ValueError("AVAS realtime calls require conversational realtime")
        if start.transport.type == "existingCall" and (start.include_startup_context is not False or start.prompt is not None
                or start.initial_items or start.model is not None or start.voice is not None or start.delegation_ack_filler is not None):
            raise ValueError("existing realtime calls do not support session configuration options")
        if start.transport.type == "existingCall" and "prompt" in start.model_fields_set:
            raise ValueError("existing realtime calls do not support session configuration options")
        for mode_instructions in (start.realtime_start_instructions, start.realtime_end_instructions):
            if mode_instructions is not None and (len(mode_instructions.encode()) + 3) // 4 > 8192:
                raise ValueError("realtime mode instructions must not exceed 8192 estimated tokens")
        session_voice = replace(config, voice=None) if start.transport.type == "webrtc" and start.version is None else config
        self.session = {} if start.transport.type == "existingCall" else session_config(start, session_voice, self.version, instructions)
        self.socket = None
        self.sdp = None
        self.session_id = start.realtime_session_id or start.thread_id
        self.response_active = False
        self.response_pending = False
        self.headers = dict(config.headers)
        self.headers["x-session-id"] = self.session_id
        self.headers["originator"] = "minicode-desktop"
        if self.version in {"v1", "v3"}:
            self.headers["openai-alpha"] = "quicksilver=v2" if self.version == "v3" else "quicksilver=v1"

    async def open(self):
        call_id = self.start.transport.call_id if self.start.transport.type == "existingCall" else None
        if self.start.transport.type == "webrtc":
            base = self.config.call_base_url or self.config.base_url
            parsed = urlsplit(base)
            endpoint = urlunsplit(({"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme), parsed.netloc,
                parsed.path.rstrip("/") + ("/live" if self.version == "v3" else "/realtime/calls"), parsed.query, ""))
            model = self.start.model or self.config.model or ("gpt-live-1-codex" if self.version == "v3" else "gpt-realtime-1.5")
            session = {**self.session, "model": model}
            async with application_http_client(endpoint, policy=self.config.network_policy, proxy_mode=self.config.proxy_mode) as client:
                response = await client.post(endpoint, headers=self.headers,
                    files={"sdp": (None, self.start.transport.sdp), "session": (None, json.dumps(session))})
                response.raise_for_status()
                self.sdp = response.text
                call_id = urlsplit(response.headers["location"]).path.rstrip("/").rsplit("/", 1)[-1]
        model = None if call_id is not None else self.start.model or self.config.model or ("gpt-live-1-codex" if self.version == "v3" else "gpt-realtime-1.5")
        url = websocket_url(self.config.base_url, self.version, model, call_id)
        self.socket = await application_websocket_connect(url, connect, policy=self.config.network_policy,
            additional_headers=self.headers,
            proxy_resolver=lambda destination: provider_proxy_url_for_base_url(destination, proxy_mode=self.config.proxy_mode) or None)
        try:
            if self.start.transport.type != "existingCall" and not (self.start.transport.type == "webrtc" and self.version == "v3"):
                await self.send({"type": "session.update", "session": self.session})
            if self.start.transport.type == "websocket" and self.version == "v3":
                first = json.loads(await self.socket.recv())
                if first.get("type") != "session.started":
                    raise ValueError("frameless realtime session received an event before session.started")
                self.session_id = first["session"]["id"]
        except BaseException:
            await self.socket.close()
            raise
        return self

    async def send(self, message: dict):
        await self.socket.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

    async def audio(self, frame):
        await self.send({"type": "input_audio.append" if self.version == "v3" else "input_audio_buffer.append", "audio": frame.data})

    async def text(self, text: str, role: str):
        if self.version == "v2" and role == "user" and text and not text.startswith("[USER] "):
            text = "[USER] " + text
        for part in context_chunks(text) if self.version == "v3" else [text]:
            await self.send(text_message(self.version, part, role))

    async def request_response(self):
        if self.response_active:
            self.response_pending = True
            return
        self.response_active = True
        self.response_pending = False
        await self.send({"type": "response.create"})

    async def response_finished(self):
        self.response_active = False
        if self.response_pending:
            await self.request_response()

    async def handoff(self, identity: str, text: str, *, final: bool):
        if self.version == "v2" and text and not text.startswith("[BACKEND] "):
            text = "[BACKEND] " + text
        text = truncate_guardian_text(text, 1000)
        channel = None
        if self.version == "v3":
            mode = self.start.codex_response_handoff_mode
            channel = "commentary" if mode == "commentary" or (mode == "bemTags" and not final) else "speakable" if mode == "bemTags" else None
            if mode == "bemTags":
                prefixes = self.start.codex_response_handoff_channel_prefixes or {}
                for name, default in (("analysis", "[ANALYSIS]"), ("commentary", "[COMMENTARY]"), ("final", "[FINAL]")):
                    if any(prefix and text.startswith(prefix) for prefix in prefixes.get(name, [default])):
                        channel = "speakable" if name == "final" else "commentary"
                        break
        if self.start.codex_responses_as_items:
            prefix = self.start.codex_response_item_prefix
            content = truncate_guardian_text((prefix + "\n\n" if prefix else "") + text, 1000)
            if self.version == "v3":
                for part in context_chunks(content):
                    await self.send({"type": "session.context.append", **({"channel": channel} if channel is not None else {}), "content": input_text(part)})
            else:
                await self.send(text_message(self.version, content, "developer"))
        elif self.version == "v3":
            for part in context_chunks(text):
                await self.send({"type": "delegation.context.append", "delegation_item_id": identity,
                    **({"channel": channel} if channel is not None else {}), "content": input_text(part)})
        elif self.version == "v1":
            if final:
                await self.send({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": identity, "output": text}})
            else:
                await self.send({"type": "conversation.handoff.append", "handoff_id": identity, "output_text": text})
        else:
            await self.send(text_message(self.version, text, "user"))
        if self.version == "v2" and final:
            await self.send({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": identity,
                "output": "Background agent finished. Use the preceding [BACKEND] messages as the result."}})
            await self.request_response()

    async def speech(self, text: str):
        if not text.strip():
            return
        text = truncate_guardian_text(("[BACKEND] " if self.version == "v2" and not text.startswith("[BACKEND] ") else "") + text, 1000)
        if self.version == "v3":
            for part in context_chunks(text):
                await self.send({"type": "session.context.append", "channel": "speakable", "content": input_text(part)})
        elif self.version == "v1":
            await self.send({"type": "conversation.handoff.append", "handoff_id": "codex", "output_text": text})
        else:
            await self.send(text_message(self.version, text, "user"))
            await self.request_response()

    async def close(self):
        if self.socket is not None:
            try:
                if self.version == "v3":
                    await self.send({"type": "session.close"})
            except ConnectionClosed:
                pass  # The original peer has already acknowledged transport closure.
            await self.socket.close()
