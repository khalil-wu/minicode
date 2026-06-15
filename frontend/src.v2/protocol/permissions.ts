import type { PermissionMode } from "../stores/types";
import { sendClientCommand } from "./ws-outbox";

export type BackendPermissionMode = "default" | "plan" | "confirm" | "bypass" | "auto" | "accept_edits";
export const DEFAULT_UI_PERMISSION_MODE: PermissionMode = "ask_permissions";

export const normalizeUiPermissionMode = (mode: unknown): PermissionMode => {
  const normalized = String(mode ?? "").trim().toLowerCase();
  if (normalized === "ask_permissions" || normalized === "confirm" || normalized === "ask") {
    return "ask_permissions";
  }
  if (
    normalized === "bypass" ||
    normalized === "bypasspermissions" ||
    normalized === "full_access" ||
    normalized === "full-access" ||
    normalized === "danger-full-access"
  ) {
    return "bypass";
  }
  if (normalized === "auto" || normalized === "default" || normalized === "acceptedits" || normalized === "accept_edits") {
    return "auto";
  }
  if (normalized === "plan") {
    return "ask_permissions";
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
  if (mode === "ask_permissions") return "confirm";
  return "auto";
};

export const fromBackendPermissionMode = (mode: string): PermissionMode => {
  return normalizeUiPermissionMode(mode);
};

export const syncPermissionMode = (mode: PermissionMode, source = "frontend.ui"): boolean =>
  sendClientCommand({
    type: "conversation.permission_mode.set",
    mode: toBackendPermissionMode(mode),
    source,
  });
