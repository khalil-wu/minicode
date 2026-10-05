from __future__ import annotations

import asyncio
import io
import json
import wave
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, UploadFile

from backend.api import routes_voice as voice


@pytest.fixture
def voice_settings(monkeypatch):
    payload = {
        "provider": "custom",
        "custom": {"display_name": "Workspace speech", "wire_api": "chat", "base_url": "https://speech.example/v1", "has_api_key": True, "headers": {"Authorization": "secret-test-key"}},
        "openai": {"display_name": "OpenAI", "wire_api": "responses", "base_url": "https://api.openai.com/v1", "has_api_key": False, "headers": {}},
        "anthropic": {"display_name": "Anthropic", "base_url": "https://api.anthropic.com", "has_api_key": True, "headers": {}},
    }
    voice._checks.clear()
    monkeypatch.setattr(voice, "get_llm_settings_payload", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr(voice, "load_config_layer_stack", lambda: SimpleNamespace(effective_config=lambda: {"test": "saved-config"}))
    return payload


def test_voice_status_distinguishes_compatibility_configuration_and_observed_success(voice_settings):
    compatible = voice.voice_service_status("custom", "asr-test")
    assert compatible["can_attempt"] is True and compatible["last_check"] is None
    assert "未确认" in compatible["reason"]
    assert not voice.voice_service_status("anthropic", "asr-test")["compatible"]
    assert not voice.voice_service_status("openai", "asr-test")["configured"]
    assert not voice.voice_service_status("custom", "")["can_attempt"]
    assert "secret-test-key" not in json.dumps(compatible)


@pytest.mark.asyncio
async def test_explicit_voice_check_sends_a_valid_silent_wav_and_records_only_the_actual_response(voice_settings, monkeypatch):
    calls = []
    class Adapter:
        async def transcribe_audio(self, data, **kwargs):
            calls.append(kwargs)
            with wave.open(io.BytesIO(data)) as audio:
                assert audio.getnchannels() == 1 and audio.getframerate() == 16000
                assert audio.getnframes() == 16000 and set(audio.readframes(16000)) == {0}
            return ""
        async def aclose(self):
            calls.append("closed")
    monkeypatch.setattr(voice, "build_provider_adapter", lambda *args, **kwargs: Adapter())
    response = await voice.check_voice_service(voice.VoiceCheckRequest(provider="custom", model="asr-test"))
    result = json.loads("".join([part async for part in response.body_iterator]))
    assert result["last_check"]["ok"] is True
    assert calls == [{"filename": "connection-check.wav", "model": "asr-test"}, "closed"]
    assert voice.voice_service_status("custom", "asr-test")["last_check"]["ok"] is True
    assert voice.voice_service_status("custom", "different-model")["last_check"] is None


@pytest.mark.asyncio
async def test_cancelled_voice_check_closes_the_provider_without_publishing_success(voice_settings, monkeypatch):
    started, closed = asyncio.Event(), asyncio.Event()
    class Adapter:
        async def transcribe_audio(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        async def aclose(self):
            closed.set()
    monkeypatch.setattr(voice, "build_provider_adapter", lambda *args, **kwargs: Adapter())
    response = await voice.check_voice_service(voice.VoiceCheckRequest(provider="custom", model="asr-test"))
    task = asyncio.create_task(anext(response.body_iterator))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert voice.voice_service_status("custom", "asr-test")["last_check"] is None


@pytest.mark.asyncio
async def test_audio_is_not_sent_if_the_configured_destination_changed_while_recording(voice_settings, monkeypatch):
    calls = []
    monkeypatch.setattr(voice, "build_provider_adapter", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(HTTPException) as error:
        await voice.transcribe_voice(audio=UploadFile(io.BytesIO(b"audio"), filename="recording.webm"),
            provider="custom", model="asr-test", language="zh", vocabulary="", expected_endpoint="https://previous.example/v1")
    assert error.value.status_code == 409 and calls == []
