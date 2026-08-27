import type { StateCreator } from "zustand";
import type { AppStore, ComposerSlice } from "./types";
import { initialUiPermissionMode, toBackendPermissionMode } from "../protocol/permissions";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { workspaceFilePathsEqual } from "../lib/workspace-path";
import { LS, readLS, writeLS } from "./shared-helpers";

/**
 * The permission mode and reasoning effort pills only move when the backend
 * echoes the new value back, so a refusal leaves the UI silently unchanged: the
 * user accepts the 完全访问 danger dialog and nothing at all happens. Note that
 * `sendClientCommandAwaitResult` *resolves* with an error-level `command.result`
 * rather than rejecting, so a bare `.catch()` never sees a refusal — the level
 * has to be inspected.
 */
const reportCommandOutcome = async (
  pending: Promise<Awaited<ReturnType<typeof sendClientCommandAwaitResult>>>,
  failureTitle: string,
): Promise<boolean> => {
  try {
    const result = await pending;
    if (commandResultSucceeded(result)) return true;
    pushToast(result.message ? `${failureTitle}：${result.message}` : `${failureTitle}。`, "error", 6000);
    return false;
  } catch (error) {
    pushToast(
      `${failureTitle}：${error instanceof Error ? error.message : "请检查连接后重试"}`,
      "error",
      6000,
    );
    return false;
  }
};

const isFilesystemMention = (kind: string): boolean => kind === "file" || kind === "folder";

const revokeBlobDataUrl = (dataUrl: string | undefined | null): void => {
  if (
    typeof dataUrl === "string" &&
    dataUrl.startsWith("blob:") &&
    typeof URL !== "undefined" &&
    typeof URL.revokeObjectURL === "function"
  ) {
    URL.revokeObjectURL(dataUrl);
  }
};

const initialAgentMode = (): ComposerSlice["agentMode"] => {
  const stored = readLS(LS.agentMode);
  if (stored === "build" || stored === "plan" || stored === "review" || stored === "explore") return stored;
  writeLS(LS.agentMode, "build");
  return "build";
};

const initialPermissionMode = (): ComposerSlice["permissionMode"] => {
  const stored = readLS(LS.permissionMode);
  const normalized = initialUiPermissionMode(stored);
  if (stored !== normalized) {
    writeLS(LS.permissionMode, normalized);
  }
  return normalized;
};

export const createComposerSlice: StateCreator<AppStore, [], [], ComposerSlice> = (set, get) => ({
  draft: "",
  attachments: [],
  quotedMessage: null,
  permissionMode: initialPermissionMode(),
  agentMode: initialAgentMode(),
  // Match MiniCode's balanced default; model/runtime capabilities still decide
  // which effort levels are actually exposed and accepted.
  effortLevel: "medium" as const,
  prMonitor: null,
  actionChip: null,
  mentionResults: [],
  selectedMentions: [],
  selectedSkills: [],
  slashPanelOpen: false,
  mentionPanelOpen: false,
  setDraft: (d) => set({ draft: d }),
  setQuotedMessage: (message) => set({ quotedMessage: message }),
  clearQuotedMessage: () => set({ quotedMessage: null }),
  addAttachment: (a) =>
    set((s) => ({ attachments: [...s.attachments, a] })),
  updateAttachment: (id, patch) =>
    set((s) => ({
      attachments: s.attachments.map((a) => (a.id === id ? { ...a, ...patch } : a)),
      conversationWorkbenchStates: Object.fromEntries(
        Object.entries(s.conversationWorkbenchStates).map(([conversationId, state]) => [
          conversationId,
          (state.attachments ?? []).some((attachment) => attachment.id === id)
            ? {
                ...state,
                attachments: (state.attachments ?? []).map((attachment) =>
                  attachment.id === id ? { ...attachment, ...patch } : attachment,
                ),
              }
            : state,
        ]),
      ),
    })),
  removeAttachment: (id) =>
    set((s) => {
      const removed = s.attachments.find((a) => a.id === id);
      revokeBlobDataUrl(removed?.dataUrl);
      return { attachments: s.attachments.filter((a) => a.id !== id) };
    }),
  clearAttachments: () =>
    set((s) => {
      // Revoke every blob: dataURL so image blob URLs don't leak when the
      // composer is cleared on send / conversation switch / long sessions.
      for (const attachment of s.attachments) revokeBlobDataUrl(attachment.dataUrl);
      return { attachments: [] };
    }),
  setPermissionMode: (m) => {
    const before = get();
    const conversationId = String(before.conversationId || "").trim();
    void reportCommandOutcome(
      sendClientCommandAwaitResult({
        type: "conversation.permission_mode.set",
        mode: toBackendPermissionMode(m),
        source: "frontend.ui",
        ...(conversationId ? { conversation_id: conversationId } : {}),
      }, "conversation.permission_mode.set"),
      "切换权限模式失败",
    );
  },
  setAgentMode: (m) => {
    writeLS(LS.agentMode, m);
    set({ agentMode: m });
  },
  setEffortLevel: (e) => {
    void reportCommandOutcome(
      sendClientCommandAwaitResult({
        type: "llm.config.set",
        provider: get().currentProvider || "openai",
        reasoning_effort: e,
        source: "frontend.footer",
      }, "effort"),
      "切换推理强度失败",
    );
  },
  setPRMonitor: (pr) => set({ prMonitor: pr }),
  setActionChip: (c) => set({ actionChip: c }),
  setMentionResults: (items) => set({ mentionResults: items }),
  addSelectedMention: (item) =>
    set((s) => ({
      selectedMentions: s.selectedMentions.some((existing) => (
        isFilesystemMention(existing.kind) && isFilesystemMention(item.kind)
          ? workspaceFilePathsEqual(existing.path, item.path, s.workingDirectory)
          : existing.kind === item.kind && existing.path === item.path
      ))
        ? s.selectedMentions
        : [...s.selectedMentions, item],
    })),
  removeSelectedMention: (path) =>
    set((s) => ({
      selectedMentions: s.selectedMentions.filter((item) => (
        isFilesystemMention(item.kind)
          ? !workspaceFilePathsEqual(item.path, path, s.workingDirectory)
          : item.path !== path
      )),
    })),
  clearSelectedMentions: () => set({ selectedMentions: [] }),
  addSelectedSkill: (skill) =>
    set((s) => ({
      selectedSkills: s.selectedSkills.some((existing) => (
        existing.path && skill.path
          ? workspaceFilePathsEqual(existing.path, skill.path, s.workingDirectory)
          : existing.name === skill.name
      ))
        ? s.selectedSkills
        : [...s.selectedSkills, { ...skill, kind: "skill" as const }],
    })),
  removeSelectedSkill: (name) =>
    set((s) => ({ selectedSkills: s.selectedSkills.filter((skill) => skill.name !== name) })),
  clearSelectedSkills: () => set({ selectedSkills: [] }),
  openSlashPanel: () => set({ slashPanelOpen: true, mentionPanelOpen: false }),
  closeSlashPanel: () => set({ slashPanelOpen: false }),
  openMentionPanel: () => set({ mentionPanelOpen: true, slashPanelOpen: false }),
  closeMentionPanel: () => set({ mentionPanelOpen: false }),
});
