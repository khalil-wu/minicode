import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "./api";

export type ComputerUseSettingsOwner = { session_id: string; conversation_id?: string | null; workspace_root?: string | null };
export type ComputerUseApprovedApp = { app_id: string; display_name: string; icon_data_url: string | null };
export type ComputerUseSettingsSnapshot = {
  owner: { session_id: string; conversation_id: string | null; workspace_root: string | null };
  provider_id: "minicode-win32" | "installed-source" | null;
  selection_id: string | null;
  navigation_visible: boolean;
  available: boolean;
  status: "ready" | "not_configured" | "disabled" | "policy_denied" | "unsupported" | "unavailable";
  reason: string | null;
  capabilities: { persistent_approvals: boolean; revoke_app_approval: boolean; persist_modes: Array<"session" | "always">; sound_mode: false; locked_use: false };
  approved_apps: ComputerUseApprovedApp[] | null;
  controls: [];
};

async function settingsResponse(response: Response): Promise<ComputerUseSettingsSnapshot> {
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  return response.json() as Promise<ComputerUseSettingsSnapshot>;
}

export function readComputerUseSettings(owner: ComputerUseSettingsOwner, signal: AbortSignal): Promise<ComputerUseSettingsSnapshot> {
  const url = new URL("/api/settings/computer-use", apiBase());
  url.searchParams.set("session_id", owner.session_id);
  if (owner.conversation_id) url.searchParams.set("conversation_id", owner.conversation_id);
  if (owner.workspace_root) url.searchParams.set("workspace_root", owner.workspace_root);
  return fetchWithTimeout(url, { headers: authHeaders(), cache: "no-store", signal }).then(settingsResponse);
}

export function removeComputerUseAppApproval(owner: ComputerUseSettingsOwner, snapshot: ComputerUseSettingsSnapshot,
  app: ComputerUseApprovedApp, signal: AbortSignal): Promise<ComputerUseSettingsSnapshot> {
  return fetchWithTimeout(new URL("/api/settings/computer-use/app-approvals/remove", apiBase()), {
    method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), signal,
    body: JSON.stringify({ ...owner, provider_id: snapshot.provider_id, selection_id: snapshot.selection_id, app_id: app.app_id }),
  }).then(settingsResponse);
}
