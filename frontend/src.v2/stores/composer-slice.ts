import type { StateCreator } from "zustand";
import type { AppStore, ComposerSlice } from "./types";
import { initialUiPermissionMode, syncPermissionMode } from "../protocol/permissions";
import { sendClientCommand } from "../protocol/ws-outbox";
import { LS, readLS, writeLS } from "./shared-helpers";

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
  effortLevel: "high" as const,
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
    writeLS(LS.permissionMode, m);
    set({ permissionMode: m });
    syncPermissionMode(m, "frontend.ui", before.conversationId);
  },
  setAgentMode: (m) => {
    writeLS(LS.agentMode, m);
    set({ agentMode: m });
  },
  setEffortLevel: (e) => {
    set({ effortLevel: e });
    sendClientCommand({
      type: "llm.config.set",
      provider: get().currentProvider || "openai",
      reasoning_effort: e,
      source: "frontend.footer",
    });
  },
  setPRMonitor: (pr) => set({ prMonitor: pr }),
  setActionChip: (c) => set({ actionChip: c }),
  setMentionResults: (items) => set({ mentionResults: items }),
  addSelectedMention: (item) =>
    set((s) => ({
      selectedMentions: s.selectedMentions.some((existing) => existing.path === item.path)
        ? s.selectedMentions
        : [...s.selectedMentions, item],
    })),
  removeSelectedMention: (path) =>
    set((s) => ({ selectedMentions: s.selectedMentions.filter((item) => item.path !== path) })),
  clearSelectedMentions: () => set({ selectedMentions: [] }),
  addSelectedSkill: (skill) =>
    set((s) => ({
      selectedSkills: s.selectedSkills.some((existing) =>
        skill.path && existing.path ? existing.path === skill.path : existing.name === skill.name
      )
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
