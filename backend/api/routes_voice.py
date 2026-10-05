"""Explicitly configured dictation; provider compatibility is not availability."""
from __future__ import annotations

import io
import json
import time
import wave
from typing import Literal

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.config import get_llm_settings_payload, load_config_layer_stack
from backend.services.llm_adapter_factory import build_provider_adapter

router = APIRouter()
# These are observations from this backend run, not a promise that a remote
# service remains online. The UI shows the observation time explicitly.
_checks: dict[tuple[str, str, str], dict] = {}


def voice_service_status(provider: str = "", model: str = "", *, settings_data: dict | None = None) -> dict:
    settings = get_llm_settings_payload(settings_data)
    selected = provider or settings["provider"]
    if selected not in {"openai", "anthropic", "custom"}:
        raise HTTPException(status_code=422, detail="请选择已配置的语音服务商。")
    section = settings[selected]
    wire = "anthropic" if selected == "anthropic" else section["wire_api"]
    compatible = wire in {"chat", "responses"}
    endpoint = section["base_url"]
    has_auth = section["has_api_key"] or any(key.lower() == "authorization" for key in section.get("headers", {}))
    configured = bool(endpoint) and (has_auth or (selected == "custom" and not section.get("auth_header")))
    if not compatible:
        reason = "此服务使用 Messages 接口，当前不能用于听写。"
    elif not configured:
        reason = "请先在模型设置中配置此服务的地址和认证。"
    elif not model.strip():
        reason = "请选择服务商，并填写其支持的转录模型。"
    else:
        reason = "配置已就绪，尚未确认远端转录接口是否可用。"
    check = _checks.get((selected, endpoint, model.strip()))
    if check and compatible and configured:
        reason = "检测结果记录最近一次转录请求；更换服务配置后请重新检测。"
    return {
        "provider": selected, "label": section.get("display_name") or {"openai": "OpenAI", "anthropic": "Anthropic", "custom": "自定义服务"}[selected],
        "endpoint": endpoint, "compatible": compatible, "configured": configured,
        "can_attempt": compatible and configured and bool(model.strip()), "reason": reason,
        "last_check": check,
    }


def _require_service(provider: str, model: str, settings_data: dict) -> dict:
    service = voice_service_status(provider, model, settings_data=settings_data)
    if not service["can_attempt"]:
        raise HTTPException(status_code=422, detail=service["reason"])
    return service


@router.get("/api/voice/status")
def get_voice_status(provider: str = Query(""), model: str = Query("")):
    return voice_service_status(provider, model)


class VoiceCheckRequest(BaseModel):
    provider: Literal["", "openai", "anthropic", "custom"] = ""
    model: str = Field(min_length=1, max_length=200)


@router.post("/api/voice/check")
async def check_voice_service(request: VoiceCheckRequest):
    settings_data = load_config_layer_stack().effective_config()
    service = _require_service(request.provider, request.model, settings_data)
    recording = io.BytesIO()
    with wave.open(recording, "wb") as sample:
        sample.setnchannels(1)
        sample.setsampwidth(2)
        sample.setframerate(16000)
        sample.writeframes(b"\x00\x00" * 16000)
    adapter = build_provider_adapter(service["provider"], model_override=request.model, settings_snapshot=settings_data)
    async def verify():
        try:
            await adapter.transcribe_audio(recording.getvalue(), filename="connection-check.wav", model=request.model)
            check = {"ok": True, "at": time.time(), "error": ""}
        except (ValueError, NotImplementedError, httpx.HTTPError) as exc:
            check = {"ok": False, "at": time.time(), "error": str(exc)}
        finally:
            await adapter.aclose()
        _checks[(service["provider"], service["endpoint"], request.model)] = check
        yield json.dumps({**service, "last_check": check}, ensure_ascii=False)
    return StreamingResponse(verify(), media_type="application/json", headers={"Cache-Control": "no-store"})


@router.post("/api/voice/transcribe")
async def transcribe_voice(
    audio: UploadFile = File(...), model: str = Form(..., min_length=1, max_length=200),
    provider: str = Form(""), language: str = Form(""), vocabulary: str = Form(""), expected_endpoint: str = Form(...),
):
    settings_data = load_config_layer_stack().effective_config()
    service = _require_service(provider, model, settings_data)
    if service["endpoint"] != expected_endpoint:
        raise HTTPException(status_code=409, detail="录音期间听写服务地址发生变化，请确认配置后重新开始。")
    data = await audio.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="录音超过 25 MiB，请缩短后重试。")
    adapter = build_provider_adapter(service["provider"], model_override=model, settings_snapshot=settings_data)

    async def transcribe():
        try:
            text = await adapter.transcribe_audio(data, filename=audio.filename or "recording.webm", model=model, language=language, prompt=vocabulary)
            _checks[(service["provider"], service["endpoint"], model)] = {"ok": True, "at": time.time(), "error": ""}
            yield json.dumps({"text": text}, ensure_ascii=False)
        except (ValueError, NotImplementedError, httpx.HTTPError) as exc:
            _checks[(service["provider"], service["endpoint"], model)] = {"ok": False, "at": time.time(), "error": str(exc)}
            yield json.dumps({"error": str(exc)}, ensure_ascii=False)
        finally:
            await adapter.aclose()

    return StreamingResponse(transcribe(), media_type="application/json", headers={"Cache-Control": "no-store"})
