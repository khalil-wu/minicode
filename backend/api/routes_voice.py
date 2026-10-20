"""Explicitly configured dictation; provider compatibility is not availability."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Literal

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from backend.config import get_llm_settings_payload, load_config_layer_stack
from backend.services.llm_adapter_factory import build_provider_adapter
from backend.services.dictation_history import dictation_history

router = APIRouter()
_transcribing_recordings: set[str] = set()
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
        reason = "最近一次听写成功。" if check["ok"] else "最近一次听写未成功。"
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


@router.get("/api/voice/recordings")
def list_voice_recordings():
    return {"recordings": dictation_history.list()}


@router.post("/api/voice/recordings")
async def save_voice_recording(
    audio: UploadFile = File(...), language: str = Form(""),
    duration_ms: int = Form(..., ge=0),
    status: Literal["saved", "cancelled", "interrupted"] = Form("saved"),
    conversation_id: str = Form(""), workspace_root: str = Form(""),
):
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=422, detail="录音没有音频内容。")
    return dictation_history.create(data, mime_type=audio.content_type or "audio/webm", language=language,
        duration_ms=duration_ms, status=status, conversation_id=conversation_id, workspace_root=workspace_root)


def _recording_audio(recording_id: str) -> tuple[dict, bytes]:
    try:
        return dictation_history.read_audio(recording_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="录音已被删除或不在最近 20 条记录中。") from None


@router.get("/api/voice/recordings/{recording_id}/audio")
def download_voice_recording(recording_id: str):
    record, audio = _recording_audio(recording_id)
    extension = "mp4" if "mp4" in record["mime_type"] else "webm"
    return Response(audio, media_type=record["mime_type"], headers={
        "Content-Disposition": f'attachment; filename="dictation-{record["id"]}.{extension}"',
        "Cache-Control": "no-store",
    })


@router.delete("/api/voice/recordings/{recording_id}")
def delete_voice_recording(recording_id: str):
    try:
        dictation_history.delete(recording_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="录音已被删除。") from None
    return {"deleted": recording_id}


class RecordingTranscriptionRequest(BaseModel):
    provider: Literal["", "openai", "anthropic", "custom"] = ""
    model: str = Field(min_length=1, max_length=200)
    expected_endpoint: str
    dictionary: list[str] = Field(default_factory=list)


@router.post("/api/voice/recordings/{recording_id}/transcribe")
async def transcribe_voice_recording(recording_id: str, request: RecordingTranscriptionRequest):
    record, audio = _recording_audio(recording_id)
    settings_data = load_config_layer_stack().effective_config()
    service = _require_service(request.provider, request.model, settings_data)
    if service["endpoint"] != request.expected_endpoint:
        raise HTTPException(status_code=409, detail="听写服务地址已变化，请确认配置后重试。")
    if recording_id in _transcribing_recordings:
        raise HTTPException(status_code=409, detail="这段录音正在转录。")
    extension = "mp4" if "mp4" in record["mime_type"] else "webm"

    async def transcribe():
        if recording_id in _transcribing_recordings:
            yield json.dumps({"error": "这段录音正在转录。"}, ensure_ascii=False)
            return
        adapter = build_provider_adapter(service["provider"], model_override=request.model, settings_snapshot=settings_data)
        _transcribing_recordings.add(recording_id)
        try:
            text = await adapter.transcribe_audio(audio, filename="recording." + extension, model=request.model,
                language=record["language"], prompt="\n".join(word.strip() for word in request.dictionary if word.strip()))
            dictation_history.update(recording_id, text=text, status="completed", error="")
            _checks[(service["provider"], service["endpoint"], request.model)] = {"ok": True, "at": time.time(), "error": ""}
            yield json.dumps({"text": text}, ensure_ascii=False)
        except asyncio.CancelledError:
            dictation_history.update(recording_id, status="interrupted")
            raise
        except (ValueError, NotImplementedError, httpx.HTTPError) as exc:
            dictation_history.update(recording_id, status="failed", error=str(exc))
            _checks[(service["provider"], service["endpoint"], request.model)] = {"ok": False, "at": time.time(), "error": str(exc)}
            yield json.dumps({"error": str(exc)}, ensure_ascii=False)
        finally:
            _transcribing_recordings.discard(recording_id)
            await adapter.aclose()

    return StreamingResponse(transcribe(), media_type="application/json", headers={"Cache-Control": "no-store"})
