import type { StateCreator } from "zustand";
import type { AppStore, ComposerSlice } from "./types";
import { initialUiPermissionMode, syncPermissionMode } from "../protocol/permissions";
import { buildApprovalResponseCommand } from "../protocol/prompt-responses";
import { sendClientCommand } from "../protocol/ws-outbox";
import { LS, readLS, writeLS } from "./shared-helpers";

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
  permissionMode: initialPermissionMode(),
  effortLevel: "high" as const,
  prMonitor: null,
  actionChip: null,
  mentionResults: [],
  selectedMentions: [],
  selectedSkills: [],
  slashPanelOpen: false,
  mentionPanelOpen: false,
  setDraft: (d) => set({ draft: d }),
  addAttachment: (a) =>
    set((s) => ({ attachments: [...s.attachments, a] })),
  updateAttachment: (id, patch) =>
    set((s) => ({
      attachments: s.attachments.map((a) => (a.id === id ? { ...a, ...patch } : a)),
    })),
  removeAttachment: (id) =>
    set((s) => ({ attachments: s.attachments.filter((a) => a.id !== id) })),
  clearAttachments: () => set({ attachments: [] }),
  setPermissionMode: (m) => {
    const before = get();
    writeLS(LS.permissionMode, m);
    set({ permissionMode: m });
    syncPermissionMode(m);
    if (m === "bypass") {
      const responded = new Set<string>();
      for (const approval of [before.pendingApproval, ...before.approvalQueue]) {
        if (!approval || responded.has(approval.requestId)) continue;
        responded.add(approval.requestId);
        sendClientCommand(buildApprovalResponseCommand(approval.requestId, "approve", approval.protocol));
      }
      for (const diff of [before.pendingDiffReview, before.diffReview]) {
        if (!diff || responded.has(diff.requestId)) continue;
        responded.add(diff.requestId);
        sendClientCommand(buildApprovalResponseCommand(diff.requestId, "approve", diff.protocol));
      }
      set({
        pendingApproval: null,
        approvalQueue: [],
        pendingDiffReview: null,
        diffReview: null,
      });
    }
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
      selectedSkills: s.selectedSkills.some((existing) => existing.name === skill.name)
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
