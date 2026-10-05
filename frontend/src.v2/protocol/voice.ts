import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "./api";

export interface VoiceServiceStatus {
  provider: string; label: string; endpoint: string; compatible: boolean; configured: boolean;
  can_attempt: boolean; reason: string;
  last_check: { ok: boolean; at: number; error: string } | null;
}

export async function fetchVoiceService(provider: string, model: string, signal?: AbortSignal): Promise<VoiceServiceStatus> {
  const url = new URL("/api/voice/status", apiBase());
  url.searchParams.set("provider", provider); url.searchParams.set("model", model);
  const response = await fetchWithTimeout(url, { headers: authHeaders(), signal });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json();
}

export async function checkVoiceService(provider: string, model: string, signal?: AbortSignal): Promise<VoiceServiceStatus> {
  const response = await fetch(new URL("/api/voice/check", apiBase()), {
    method: "POST", headers: authHeaders({ "content-type": "application/json" }), signal, body: JSON.stringify({ provider, model }),
  });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json();
}
