import type { RightStackTab } from "../stores/types";

export type DisplayRoutable = {
  display_scope?: string;
  displayScope?: string;
  panel_hint?: string;
  panelHint?: string;
  requires_attention?: boolean;
  requiresAttention?: boolean;
  is_error?: boolean;
  error?: unknown;
  status?: string;
};

const RIGHT_TABS = new Set<RightStackTab>([
  "preview",
  "browser",
  "terminal",
  "tasks",
  "diff",
  "plan",
  "subagents",
  "inspector",
  "diagnostics",
]);

export function normalizePanelHint(value?: string): RightStackTab | null {
  if (!value) return null;
  const normalized = value === "agents" ? "subagents" : value;
  return RIGHT_TABS.has(normalized as RightStackTab) ? normalized as RightStackTab : null;
}

export function displayScopeOf(event: DisplayRoutable): string {
  return String(event.display_scope ?? event.displayScope ?? "").toLowerCase();
}

export function panelHintOf(event: DisplayRoutable): RightStackTab | null {
  return normalizePanelHint(event.panel_hint ?? event.panelHint);
}

export function requiresAttention(event: DisplayRoutable): boolean {
  const explicit = event.requires_attention ?? event.requiresAttention;
  if (explicit != null) return Boolean(explicit);
  if (event.is_error || event.error) return true;
  const status = String(event.status || "").toLowerCase();
  return status === "error" || status === "failed";
}

export function isHiddenFromActivity(event: DisplayRoutable): boolean {
  const scope = displayScopeOf(event);
  return scope === "silent" || scope === "inspector";
}

export function shouldShowInActivity(event: DisplayRoutable): boolean {
  if (requiresAttention(event)) return true;
  return !isHiddenFromActivity(event);
}

export function shouldShowInMainChat(event: DisplayRoutable): boolean {
  if (requiresAttention(event)) return true;
  const scope = displayScopeOf(event);
  if (!scope) return true;
  return scope === "chat" || scope === "activity" || scope === "notice";
}
