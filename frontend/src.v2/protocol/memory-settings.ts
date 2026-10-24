import { desktop, runtime, type PersonalizationAction, type PersonalizationHost } from "../desktop/runtime";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "./api";

export interface MemorySettingsPayload {
  feature_enabled: boolean;
  memories: { generate_memories: boolean; use_memories: boolean; disable_on_external_context: boolean };
  path: string;
  feature_locked: boolean;
  locked_fields: string[];
}
export interface PersonalizationPayload { instructions: string; path: string; exists: boolean; max_bytes: number }
export type MemorySettingsUpdate = { enabled?: boolean; disable_on_external_context?: boolean };

export const currentPersonalizationHost = (): PersonalizationHost => {
  const host = runtime()?.remoteHost;
  return host ? { id: host.id, displayName: host.displayName, kind: host.kind } : { id: "local", displayName: "Local", kind: "local" };
};

export async function listPersonalizationHosts(): Promise<PersonalizationHost[]> {
  const native = desktop()?.personalizations;
  return native ? native.listHosts() : [currentPersonalizationHost()];
}

export async function personalizationRequest<T>(hostId: string, action: PersonalizationAction, payload?: unknown): Promise<T> {
  const native = desktop()?.personalizations;
  if (native) return await native.request(hostId, action, payload) as T;
  if (hostId !== currentPersonalizationHost().id) throw new Error("This personalization host is not connected to this page.");
  const operations = {
    "read-memory": ["GET", "/api/settings/memory"], "update-memory": ["PUT", "/api/settings/memory"],
    "read-instructions": ["GET", "/api/settings/personalization"], "update-instructions": ["PUT", "/api/settings/personalization"],
    "reset-memory": ["POST", "/api/settings/memory/reset"],
  } as const;
  const [method, path] = operations[action];
  const response = await fetchWithTimeout(`${apiBase()}${path}`, { method, headers: authHeaders({ "content-type": "application/json" }),
    cache: "no-store", ...method !== "GET" ? { body: JSON.stringify(payload) } : {} });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return await response.json() as T;
}
