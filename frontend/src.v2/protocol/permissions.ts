import type { PermissionMode } from "../stores/types";
import { sendClientCommand } from "./ws-outbox";

export const toBackendPermissionMode = (mode: PermissionMode): "default" | "plan" | "confirm" | "bypass" | "auto" | "accept_edits" => {
  if (mode === "plan") return "plan";
  if (mode === "bypass") return "bypass";
  if (mode === "auto") return "auto";
  if (mode === "acceptEdits") return "accept_edits";
  if (mode === "ask_permissions") return "confirm";
  return "default";
};

export const fromBackendPermissionMode = (mode: string): PermissionMode => {
  if (mode === "plan") return "plan";
  if (mode === "confirm") return "ask_permissions";
  if (mode === "bypass") return "bypass";
  if (mode === "accept_edits") return "acceptEdits";
  if (mode === "auto") return "auto";
  return "ask_permissions";
};

export const syncPermissionMode = (mode: PermissionMode, source = "frontend.ui"): boolean =>
  sendClientCommand({
    type: "conversation.permission_mode.set",
    mode: toBackendPermissionMode(mode),
    source,
  });
