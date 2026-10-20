from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from starlette.datastructures import Headers

from backend.api import routes_voice as voice
from backend.services.dictation_history import DictationHistory


@pytest.fixture
def voice_settings(monkeypatch, tmp_path):
    payload = {
        "provider": "custom",
        "custom": {"display_name": "Workspace speech", "wire_api": "chat", "base_url": "https://speech.example/v1", "has_api_key": True, "headers": {"Authorization": "secret-test-key"}},
        "openai": {"display_name": "OpenAI", "wire_api": "responses", "base_url": "https://api.openai.com/v1", "has_api_key": False, "headers": {}},
        "anthropic": {"display_name": "Anthropic", "base_url": "https://api.anthropic.com", "has_api_key": True, "headers": {}},
    }
    voice._checks.clear()
    voice._transcribing_recordings.clear()
    monkeypatch.setattr(voice, "dictation_history", DictationHistory(tmp_path / "dictation"))
    monkeypatch.setattr(voice, "get_llm_settings_payload", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(voice, "load_config_layer_stack", lambda: SimpleNamespace(effective_config=lambda: {"test": "saved-config"}))
    return payload


async def save_audio(data=b"original-audio", status="saved"):
    return await voice.save_voice_recording(audio=UploadFile(io.BytesIO(data), filename="recording.webm", headers=Headers({"content-type": "audio/webm"})),
        duration_ms=1450, status=status, language="zh", conversation_id="original-chat", workspace_root="C:/original")


def request(**changes):
    return voice.RecordingTranscriptionRequest(provider="custom", model="asr-test", expected_endpoint="https://speech.example/v1", dictionary=["  MiniCode  ", "项目,不拆分", ""], **changes)


async def result(response):
    return json.loads("".join([part async for part in response.body_iterator]))


def test_voice_status_requires_actual_compatible_provider_configuration(voice_settings):
    compatible = voice.voice_service_status("custom", "asr-test")
    assert compatible["can_attempt"] is True and compatible["last_check"] is None
    assert not voice.voice_service_status("anthropic", "asr-test")["compatible"]
    assert not voice.voice_service_status("openai", "asr-test")["configured"]
    assert not voice.voice_service_status("custom", "")["can_attempt"]
    assert "secret-test-key" not in json.dumps(compatible)
    voice._checks[("custom", "https://speech.example/v1", "asr-test")] = {"ok": False, "at": 1, "error": "actual provider error"}
    assert voice.voice_service_status("custom", "asr-test")["reason"] == "最近一次听写未成功。"
    voice._checks[("custom", "https://speech.example/v1", "asr-test")]["ok"] = True
    assert voice.voice_service_status("custom", "asr-test")["reason"] == "最近一次听写成功。"


@pytest.mark.asyncio
async def test_history_keeps_last_twenty_actual_audio_files_across_restart(voice_settings, tmp_path):
    unrelated = tmp_path / "authored.txt"
    unrelated.write_text("keep this user file", encoding="utf-8")
    records = [await save_audio(f"audio-{index}".encode(), "interrupted" if index == 20 else "cancelled") for index in range(21)]
    restarted = DictationHistory(voice.dictation_history.root)
    assert [record["id"] for record in restarted.list()] == [record["id"] for record in reversed(records[1:])]
    assert len(list(restarted.root.glob("*.audio"))) == 20
    assert restarted.read_audio(records[-1]["id"])[1] == b"audio-20"
    assert restarted.list()[0]["status"] == "interrupted"
    with pytest.raises(KeyError):
        restarted.read_audio(records[0]["id"])
    with pytest.raises(KeyError):
        restarted.read_audio("../authored.txt")
    assert unrelated.read_text(encoding="utf-8") == "keep this user file"


@pytest.mark.asyncio
async def test_failed_transcription_retries_same_saved_audio_and_id_using_dictionary(voice_settings, monkeypatch):
    record = await save_audio()
    calls = []
    class Adapter:
        async def transcribe_audio(self, data, **kwargs):
            calls.append((data, kwargs))
            if len(calls) == 1:
                raise httpx.ConnectError("fixture network interruption")
            return "Recovered transcript"
        async def aclose(self):
            calls.append("closed")
    monkeypatch.setattr(voice, "build_provider_adapter", lambda *args, **kwargs: Adapter())
    first = await result(await voice.transcribe_voice_recording(record["id"], request()))
    assert first == {"error": "fixture network interruption"}
    stored, audio = voice.dictation_history.read_audio(record["id"])
    assert audio == b"original-audio" and stored["status"] == "failed" and stored["text"] == ""
    second = await result(await voice.transcribe_voice_recording(record["id"], request()))
    assert second == {"text": "Recovered transcript"}
    stored, audio = voice.dictation_history.read_audio(record["id"])
    assert stored["status"] == "completed" and stored["text"] == "Recovered transcript"
    assert len(voice.dictation_history.list()) == 1
    assert calls == [(b"original-audio", {"filename": "recording.webm", "model": "asr-test", "language": "zh", "prompt": "MiniCode\n项目,不拆分"}), "closed",
        (b"original-audio", {"filename": "recording.webm", "model": "asr-test", "language": "zh", "prompt": "MiniCode\n项目,不拆分"}), "closed"]


@pytest.mark.asyncio
async def test_cancelled_transcription_releases_adapter_and_retains_original_audio(voice_settings, monkeypatch):
    record = await save_audio()
    started, closed = asyncio.Event(), asyncio.Event()
    class Adapter:
        async def transcribe_audio(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        async def aclose(self):
            closed.set()
    monkeypatch.setattr(voice, "build_provider_adapter", lambda *args, **kwargs: Adapter())
    response = await voice.transcribe_voice_recording(record["id"], request())
    task = asyncio.create_task(anext(response.body_iterator))
    await started.wait()
    with pytest.raises(HTTPException) as duplicate:
        await voice.transcribe_voice_recording(record["id"], request())
    assert duplicate.value.status_code == 409
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and not voice._transcribing_recordings
    stored, audio = voice.dictation_history.read_audio(record["id"])
    assert audio == b"original-audio" and stored["status"] == "interrupted"


@pytest.mark.asyncio
async def test_changed_endpoint_is_not_used_to_retry_saved_audio(voice_settings, monkeypatch):
    record = await save_audio()
    calls = []
    monkeypatch.setattr(voice, "build_provider_adapter", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(HTTPException) as error:
        await voice.transcribe_voice_recording(record["id"], request().model_copy(update={"expected_endpoint": "https://previous.example/v1"}))
    assert error.value.status_code == 409 and calls == []
    assert voice.dictation_history.read_audio(record["id"])[1] == b"original-audio"


@pytest.mark.asyncio
async def test_http_save_download_and_delete_operate_only_on_the_same_archive_id(voice_settings):
    app = FastAPI()
    app.include_router(voice.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        saved = await client.post("/api/voice/recordings", files={"audio": ("clip.webm", b"exact original bytes", "audio/webm")},
            data={"duration_ms": "970", "language": "ja", "status": "cancelled", "conversation_id": "owner", "workspace_root": "C:/owner"})
        assert saved.status_code == 200
        record = saved.json()
        assert record["status"] == "cancelled" and record["duration_ms"] == 970
        listing = await client.get("/api/voice/recordings")
        assert listing.json() == {"recordings": [record]}
        downloaded = await client.get(f'/api/voice/recordings/{record["id"]}/audio')
        assert downloaded.content == b"exact original bytes" and record["id"] in downloaded.headers["content-disposition"]
        assert (await client.delete(f'/api/voice/recordings/{record["id"]}')).status_code == 200
        assert (await client.get(f'/api/voice/recordings/{record["id"]}/audio')).status_code == 404
        assert (await client.get("/api/voice/recordings")).json() == {"recordings": []}
        assert list(voice.dictation_history.root.glob("*.audio")) == []


@pytest.mark.asyncio
async def test_deleting_a_transcribing_recording_cannot_be_undone_by_its_late_result(voice_settings, monkeypatch):
    record = await save_audio()
    started, finish = asyncio.Event(), asyncio.Event()
    class Adapter:
        async def transcribe_audio(self, *args, **kwargs):
            started.set()
            await finish.wait()
            return "late transcript"
        async def aclose(self):
            pass
    monkeypatch.setattr(voice, "build_provider_adapter", lambda *args, **kwargs: Adapter())
    response = await voice.transcribe_voice_recording(record["id"], request())
    task = asyncio.create_task(result(response))
    await started.wait()
    voice.delete_voice_recording(record["id"])
    finish.set()
    assert await task == {"text": "late transcript"}
    assert voice.dictation_history.list() == []


@pytest.mark.asyncio
async def test_unstarted_response_does_not_reserve_a_recording_or_open_a_provider(voice_settings, monkeypatch):
    record = await save_audio()
    adapters = []
    monkeypatch.setattr(voice, "build_provider_adapter", lambda *args, **kwargs: adapters.append(args))
    response = await voice.transcribe_voice_recording(record["id"], request())
    await response.body_iterator.aclose()
    assert adapters == [] and not voice._transcribing_recordings
    assert voice.dictation_history.read_audio(record["id"])[1] == b"original-audio"

@pytest.mark.asyncio
async def test_provider_audio_size_failure_cannot_prevent_local_recovery_storage(voice_settings):
    audio = b"x" * (25 * 1024 * 1024 + 1)
    record = await save_audio(audio)
    stored, actual = voice.dictation_history.read_audio(record["id"])
    assert actual == audio and stored["status"] == "saved"
