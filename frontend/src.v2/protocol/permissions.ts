import type { PermissionMode } from "../stores/types";
import { sendClientCommand } from "./ws-outbox";

export type BackendPermissionMode = PermissionMode;
export const DEFAULT_UI_PERMISSION_MODE: PermissionMode = "confirm";

export const normalizeUiPermissionMode = (mode: unknown): PermissionMode => {
  const normalized = String(mode ?? "").trim().toLowerCase();
  if (normalized === "confirm") return "confirm";
  if (normalized === "plan") return "plan";
  if (normalized === "auto") return "auto";
  if (normalized === "bypass") return "bypass";
  throw new Error(`Unsupported permission mode: ${String(mode)}`);
};

export const initialUiPermissionMode = (stored: unknown): PermissionMode => {
  const raw = String(stored ?? "").trim();
  if (!raw) return DEFAULT_UI_PERMISSION_MODE;
  return normalizeUiPermissionMode(raw);
};

export const toBackendPermissionMode = (mode: PermissionMode): BackendPermissionMode => {
  return mode;
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
