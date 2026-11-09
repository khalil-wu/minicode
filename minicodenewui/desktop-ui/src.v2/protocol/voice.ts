import { apiBase, runtimeToken, errorMessageFromResponseText, fetchWithTimeout } from "./api";
import { desktop } from "../desktop/runtime";

async function voiceRequest(path: string, options: RequestInit = {}, timed = true): Promise<Response> {
  const native = desktop()?.dictation;
  const transport = native ? await native.getLocalRuntime() : { apiBaseUrl: apiBase(), runtimeToken: runtimeToken() };
  const headers = new Headers(options.headers);
  if (transport.runtimeToken) headers.set("X-MiniCode-Token", transport.runtimeToken);
  const request = { ...options, headers };
  return timed ? fetchWithTimeout(new URL(path, transport.apiBaseUrl), request) : fetch(new URL(path, transport.apiBaseUrl), request);
}

export interface VoiceServiceStatus {
  provider: string; label: string; endpoint: string; compatible: boolean; configured: boolean;
  can_attempt: boolean; reason: string;
  last_check: { ok: boolean; at: number; error: string } | null;
}

export interface DictationRecording {
  id: string; created_at: number; mime_type: string; language: string; duration_ms: number;
  status: "saved" | "cancelled" | "interrupted" | "completed" | "failed";
  conversation_id: string; workspace_root: string; text: string; error: string;
}

async function voiceResponse<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json();
}

export async function listDictationRecordings(signal?: AbortSignal): Promise<DictationRecording[]> {
  const result = await voiceResponse<{ recordings: DictationRecording[] }>(await voiceRequest("/api/voice/recordings", { signal }));
  return result.recordings;
}

export async function saveDictationRecording(audio: Blob, metadata: {
  language: string; duration_ms: number; status: "saved" | "cancelled" | "interrupted"; conversation_id: string; workspace_root: string;
}): Promise<DictationRecording> {
  const body = new FormData();
  body.append("audio", audio, audio.type.includes("mp4") ? "recording.mp4" : "recording.webm");
  Object.entries(metadata).forEach(([name, value]) => body.append(name, String(value)));
  return voiceResponse(await voiceRequest("/api/voice/recordings", { method: "POST", body }, false));
}

export async function transcribeDictationRecording(id: string, service: VoiceServiceStatus, model: string, dictionary: string[], signal?: AbortSignal): Promise<string> {
  const result = await voiceResponse<{ text?: string; error?: string }>(await voiceRequest("/api/voice/recordings/" + encodeURIComponent(id) + "/transcribe", {
    method: "POST", headers: { "content-type": "application/json" }, signal,
    body: JSON.stringify({ provider: service.provider, expected_endpoint: service.endpoint, model, dictionary }),
  }, false));
  if (result.error) throw new Error(result.error);
  return result.text!;
}

export async function deleteDictationRecording(id: string): Promise<void> {
  await voiceResponse(await voiceRequest("/api/voice/recordings/" + encodeURIComponent(id), { method: "DELETE" }));
}

export async function downloadDictationRecording(recording: DictationRecording): Promise<void> {
  const response = await voiceRequest("/api/voice/recordings/" + encodeURIComponent(recording.id) + "/audio");
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a"); link.href = url;
  link.download = "dictation-" + recording.id + (recording.mime_type.includes("mp4") ? ".mp4" : ".webm");
  link.click(); URL.revokeObjectURL(url);
}

export async function fetchVoiceService(provider: string, model: string, signal?: AbortSignal): Promise<VoiceServiceStatus> {
  const query = new URLSearchParams({ provider, model });
  const response = await voiceRequest("/api/voice/status?" + query.toString(), { signal });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json();
}
