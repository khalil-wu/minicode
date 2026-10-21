"""One realtime service belongs to its original thread, never its visible tab."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging

from websockets.exceptions import ConnectionClosed

from backend.ids import new_uuid7
from backend.agent.turn_state import FINAL_TEXT_SOURCES
from backend.realtime.protocol import Start, transcript_tail_fragment
from backend.realtime.transport import RealtimeConfig, RealtimeConnection

logger = logging.getLogger(__name__)


@dataclass
class Delegation:
    identity: str
    assistant_message_id: str
    user_message_id: str
    turn_id: str | None = None
    final_sent: bool = False
    final_text: str = ""
    final_item_id: str = ""


class RealtimeRuntime:
    def __init__(self, config: RealtimeConfig, start: Start, *, emit, commit, delegate, record_tail, track_admission, startup_context: str, active_changed):
        self.config, self.params = config, start
        self.emit, self.commit, self.delegate = emit, commit, delegate
        self.active_changed = active_changed
        self.desktop_client = None
        self.record_tail, self.track_admission = record_tail, track_admission
        from backend.realtime.prompts import backend_prompt
        instructions = backend_prompt(start, config)
        if start.include_startup_context is not False:
            instructions = "\n\n".join(part for part in (instructions, startup_context) if part)
        self.connection = RealtimeConnection(config, start, instructions)
        self.task: asyncio.Task | None = None
        self.active = False
        self.context_active = False
        self.closing = False
        self.started = False
        self.ended = False
        self.transcripts: dict[str, dict] = {}
        self.delegations: list[Delegation] = []
        self.admissions: set[asyncio.Task] = set()
        self.write_lock = asyncio.Lock()
        self.close_lock = asyncio.Lock()
        self.last_user_item_id = ""
        self.last_handoff_user_item_id = ""
        self.transcript_tail: list[dict] = []

    def launch(self):
        self.active = True
        self.active_changed()
        self.task = asyncio.create_task(self._run(), name=f"realtime:{self.params.thread_id}")
        from backend.async_cleanup import _consume_task_result
        self.task.add_done_callback(_consume_task_result)
        self.task.add_done_callback(lambda _done: self.active_changed())

    async def _send(self, kind, **fields):
        await self.emit({"type": f"thread/realtime/{kind}", "threadId": self.params.thread_id,
            "conversation_id": self.params.thread_id, **fields})

    async def _item(self, item: dict):
        await self.commit(item)
        await self._send("item/completed", item=item, timestamp=datetime.now(timezone.utc).isoformat())

    def _record(self, kind: str, **fields):
        return {"id": str(new_uuid7()), "realtimeSessionId": self.connection.session_id, "type": kind, **fields}

    async def _run(self):
        outcome, reason = "ended", "transport_closed"
        try:
            await self.connection.open()
            self.started = True
            self.context_active = True
            await self._item(self._record("realtimeSessionStarted"))
            await self._send("started", realtimeSessionId=self.connection.session_id, version=self.connection.version)
            if self.connection.sdp is not None:
                await self._send("sdp", sdp=self.connection.sdp)
            async for frame in self.connection.socket:
                if not isinstance(frame, str):
                    raise ValueError("realtime transport expected a JSON text frame")
                await self._receive(json.loads(frame))
        except asyncio.CancelledError:
            reason = "requested"
            raise
        except ConnectionClosed as exc:
            reason = "transport_closed"
            if exc.code not in {1000, 1001}:
                outcome = "failed"
                reason = "error"
                await self._send("error", message=str(exc))
        except Exception as exc:
            outcome, reason = "failed", "error"
            logger.warning("Realtime transport failed for thread %s: %s", self.params.thread_id, type(exc).__name__)
            await self._send("error", message=str(exc))
        finally:
            self.active = False
            self.active_changed()
            await self.connection.close()
            try:
                await self._finish(outcome, reason)
            except Exception:
                self.context_active = False
                logger.exception("Failed to save realtime transcript before closure")
                await self._send("error", message="failed to save the realtime transcript before closure")
                await self._send("closed", reason="error")

    async def _finish(self, outcome: str, reason: str | None):
        if self.ended:
            return
        self.ended = True
        for role, item in tuple(self.transcripts.items()):
            if item["text"]:
                await self._item(item)
                self.transcript_tail.append(item)
        self.transcripts.clear()
        if self.params.flush_transcript_tail_on_session_end and self.transcript_tail:
            tail = "\n".join(f"{item['role']}: {item['text']}" for item in self.transcript_tail)
            await self.record_tail(transcript_tail_fragment(tail), str(new_uuid7()))
            self.transcript_tail.clear()
        self.context_active = False
        if self.started:
            await self._item(self._record("realtimeSessionClosed", outcome=outcome))
        await self._send("closed", reason=reason)

    async def _transcript(self, role: str, text: str, *, done: bool):
        item = self.transcripts.get(role)
        if item is None:
            item = self._record("transcriptSegment", role=role, text="")
            self.transcripts[role] = item
            await self._send("item/started", item=dict(item))
        if done:
            item["text"] = text
            await self._send("transcript/done", role=role, text=text)
            await self._item(item)
            self.transcripts.pop(role, None)
            self.transcript_tail.append(item)
            if role == "user":
                self.last_user_item_id = item["id"]
        else:
            item["text"] += text
            await self._send("item/transcript/delta", itemId=item["id"], delta=text)
            await self._send("transcript/delta", role=role, delta=text)

    async def _receive(self, event: dict):
        kind = event["type"]
        version = self.connection.version
        if kind == "session.updated" or (version == "v3" and kind == "session.started"):
            self.connection.session_id = event.get("session", {}).get("id", self.connection.session_id)
        elif (version == "v3" and kind == "output_audio.delta") or (version == "v2" and kind in {"response.output_audio.delta", "response.audio.delta"}) or (version == "v1" and kind == "conversation.output_audio.delta"):
            audio = {"data": event.get("audio", event.get("delta", event.get("data"))),
                "sampleRate": event.get("sample_rate", 24000), "numChannels": event.get("channels", event.get("num_channels", 1))}
            for source, target in (("samples_per_channel", "samplesPerChannel"), ("item_id", "itemId")):
                if source in event:
                    audio[target] = event[source]
            await self._send("outputAudio/delta", audio=audio)
        elif version == "v3" and kind in {"input_transcript.added", "output_transcript.added"}:
            await self._transcript("user" if kind.startswith("input") else "assistant", event["item"]["text"], done=False)
        elif version == "v3" and kind == "turn.done":
            role, text = event["turn"]["role"], event["turn"]["transcript"]
            if role in {"user", "assistant"}:
                await self._transcript(role, text, done=True)
        elif version != "v3" and (kind == "conversation.item.input_audio_transcription.delta" or (version == "v1" and kind == "conversation.input_transcript.delta")):
            await self._transcript("user", event["delta"], done=False)
        elif version != "v3" and (kind == "conversation.item.input_audio_transcription.completed" or (version == "v1" and kind == "conversation.input_transcript.turn_marked")):
            await self._transcript("user", event["transcript"], done=True)
        elif version != "v3" and (kind in {"response.output_audio_transcript.delta", "response.output_text.delta"} or (version == "v1" and kind == "conversation.output_transcript.delta")):
            await self._transcript("assistant", event["delta"], done=False)
        elif version != "v3" and (kind == "response.output_audio_transcript.done" or (version == "v2" and kind == "response.output_text.done")):
            await self._transcript("assistant", event.get("transcript", event.get("text", "")), done=True)
        elif version != "v3" and (kind == "conversation.item.added" or (version == "v2" and kind == "conversation.item.created")):
            await self._send("itemAdded", item=event["item"])
        elif version == "v1" and kind == "conversation.handoff.requested":
            await self._handoff(event["handoff_id"], event["input_transcript"], event)
        elif version == "v3" and kind == "delegation.created" and event["item"].get("type") == "delegation" and event["item"].get("target") == "client":
            item = event["item"]
            prompt = "".join(part["text"] for part in item["content"] if part["type"] == "input_text")
            await self._handoff(item["id"], prompt, event)
        elif version == "v2" and kind == "conversation.item.done":
            item = event["item"]
            if item.get("type") == "function_call" and item.get("name") == "background_agent":
                arguments = item.get("arguments", "")
                try:
                    decoded = json.loads(arguments)
                except json.JSONDecodeError:
                    decoded = None
                prompt = next((decoded[key].strip() for key in ("input_transcript", "input", "text", "prompt", "query")
                    if isinstance(decoded, dict) and isinstance(decoded.get(key), str) and decoded[key].strip()), arguments)
                await self._handoff(item.get("call_id") or item["id"], prompt, event)
            elif item.get("type") == "function_call" and item.get("name") == "remain_silent":
                await self.connection.send({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": item.get("call_id") or item["id"], "output": "{}"}})
        elif kind == "error":
            error = event["error"]
            message = error["message"] if isinstance(error, dict) else str(error)
            if version == "v2" and message.startswith("Conversation already has an active response in progress:"):
                self.connection.response_active = self.connection.response_pending = True
            else:
                await self._send("error", message=message)
        elif version == "v2" and kind == "response.created":
            self.connection.response_active = True
        elif version == "v2" and kind in {"response.done", "response.cancelled"}:
            await self.connection.response_finished()
        elif version == "v2" and kind == "input_audio_buffer.speech_started":
            await self._send("itemAdded", item=event)
        else:
            logger.debug("Realtime event type %s has no Source projection", kind)

    async def _handoff(self, identity: str, prompt: str, original: dict):
        if any(item.identity == identity for item in self.delegations):
            return
        await self._send("itemAdded", item={"type": "handoff_requested", "handoffId": identity,
            "inputTranscript": prompt, "source": original})
        if self.params.client_managed_handoffs:
            return
        user_item = self.transcripts.get("user")
        user_id = user_item["id"] if user_item is not None else self.last_user_item_id
        delegation = Delegation(identity, "assistant_" + str(new_uuid7()), user_id or "user_" + str(new_uuid7()))
        self.last_handoff_user_item_id = user_id
        self.delegations.append(delegation)
        self.transcript_tail.clear()
        self._admit(prompt, delegation)

    def _admit(self, prompt, delegation):
        async def admit():
            await self.delegate(prompt, delegation)
        task = asyncio.create_task(admit(), name=f"realtime-delegation:{delegation.identity}")
        self.track_admission(task)
        self.admissions.add(task)
        def settled(done):
            self.admissions.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.error("Realtime delegation admission failed", exc_info=done.exception())
        task.add_done_callback(settled)

    async def observe_backend_event(self, kind: str, data: dict):
        if not self.active or not self.started or self.closing:
            return
        item = data.get("item", {})
        message_id = str(data.get("message_id") or data.get("assistant_message_id") or "")
        turn_id = str(data.get("turn_id") or data.get("run_id") or "")
        for delegation in self.delegations:
            if delegation.final_sent:
                continue
            if message_id == delegation.assistant_message_id and turn_id:
                delegation.turn_id = turn_id
            if delegation.turn_id is None or turn_id != delegation.turn_id:
                continue
            if kind == "agent_message.delta" and data.get("source") == "commentary":
                text = str(data.get("delta") or data.get("content") or "")
                if text:
                    async with self.write_lock:
                        await self.connection.handoff(delegation.identity, text, final=False)
            elif kind == "agent.progress" and self.params.backend_reasoning_status and self.connection.version == "v3":
                text = str(data.get("message") or "").encode("utf-8")[:256].decode("utf-8", errors="ignore")
                if text:
                    await self.connection.send({"type": "delegation.context.append", "delegation_item_id": delegation.identity,
                        "channel": "commentary", "content": [{"type": "input_text", "text": "[STATUS] " + text}]})
            elif kind == "item.completed" and item.get("type") == "agent_message" and item.get("source") in FINAL_TEXT_SOURCES:
                text = str(item.get("text") or "")
                if text:
                    delegation.final_text, delegation.final_item_id = text, item["id"]
            elif kind == "done" and data.get("status") in {"completed", "partial"}:
                if delegation.final_text:
                    delegation.final_sent = True
                    async with self.write_lock:
                        await self.connection.handoff(delegation.identity, delegation.final_text, final=True)
                    await self._item(self._record("bemItemPromoted", turnId=turn_id,
                        itemId=delegation.final_item_id, presentation={"type": "wholeItem"}))

    async def append_audio(self, audio):
        if not self.started or not self.active:
            raise ValueError("thread realtime transport is not ready")
        async with self.write_lock:
            await self.connection.audio(audio)

    async def append_text(self, text, role):
        if not self.started or not self.active:
            raise ValueError("thread realtime transport is not ready")
        async with self.write_lock:
            await self.connection.text(text, role)

    async def append_speech(self, text):
        if not self.started or not self.active:
            raise ValueError("thread realtime transport is not ready")
        async with self.write_lock:
            await self.connection.speech(text)

    async def aclose(self):
        async with self.close_lock:
            self.closing = True
            if self.task is not None and not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            self.active = False
            self.active_changed()
