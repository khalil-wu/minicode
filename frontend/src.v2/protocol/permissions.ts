import type { PermissionMode } from "../stores/types";
import { sendClientCommand } from "./ws-outbox";

export type BackendPermissionMode = "default" | "plan" | "confirm" | "bypass" | "auto" | "accept_edits";
export const DEFAULT_UI_PERMISSION_MODE: PermissionMode = "ask_permissions";

export const normalizeUiPermissionMode = (mode: unknown): PermissionMode => {
  const normalized = String(mode ?? "").trim().toLowerCase();
  const canonical = normalized.replace(/[\s-]+/g, "_");
  const compact = normalized.replace(/[\s_-]+/g, "");
  if (
    canonical === "ask_permissions" ||
    normalized === "confirm" ||
    normalized === "ask" ||
    compact === "askpermissions"
  ) {
    return "ask_permissions";
  }
  if (normalized === "plan" || canonical === "plan_mode" || compact === "planmode") {
    return "plan";
  }
  if (
    normalized === "bypass" ||
    canonical === "full_access" ||
    canonical === "danger_full_access" ||
    compact === "bypasspermissions" ||
    compact === "fullaccess" ||
    compact === "dangerfullaccess"
  ) {
    return "bypass";
  }
  if (
    normalized === "auto" ||
    normalized === "default" ||
    canonical === "accept_edits" ||
    compact === "acceptedits"
  ) {
    return "auto";
  }
  return "auto";
};

export const initialUiPermissionMode = (stored: unknown): PermissionMode => {
  const raw = String(stored ?? "").trim();
  if (!raw) return DEFAULT_UI_PERMISSION_MODE;
  const normalized = normalizeUiPermissionMode(raw);
  if (normalized === "auto" && !["auto", "default", "accept_edits", "acceptedits"].includes(raw.toLowerCase())) {
    return DEFAULT_UI_PERMISSION_MODE;
  }
  return normalized;
};

export const toBackendPermissionMode = (mode: PermissionMode): BackendPermissionMode => {
  if (mode === "bypass") return "bypass";
  if (mode === "auto") return "auto";
  if (mode === "plan") return "plan";
  if (mode === "ask_permissions") return "confirm";
  return "auto";
};

export const fromBackendPermissionMode = (mode: string): PermissionMode => {
  return normalizeUiPermissionMode(mode);
};

export const syncPermissionMode = (
  mode: PermissionMode,
  source = "frontend.ui",
  conversationId?: string | null,
): boolean => {
  const targetConversationId = String(conversationId ?? "").trim();
  return sendClientCommand({
    type: "conversation.permission_mode.set",
    mode: toBackendPermissionMode(mode),
    source,
    ...(targetConversationId ? { conversation_id: targetConversationId } : {}),
  });
};
