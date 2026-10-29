/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { emptyComposerDraft } from "../stores/composer-drafts";
import { claimSubmittedDraft } from "./submittedDraftClaim";

beforeEach(() => {
  localStorage.clear();
  useAppStore.setState({ ...emptyComposerDraft(), conversationId: "claim-owner", workingDirectory: "C:/claim-owner",
    activeConversationDraftId: null, conversationDrafts: {}, conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, messages: [] });
});
afterEach(() => vi.restoreAllMocks());

it("stops examining the replacement draft after the author takes ownership", () => {
  const command = vi.fn(() => null);
  const claim = claimSubmittedDraft({ conversationId: "claim-owner", workspaceRoot: "C:/claim-owner", userMessageId: "submitted", selectedCommand: command });
  try {
    expect(claim.canRestore()).toBe(true);
    useAppStore.setState({ draft: "newer authored input" });
    expect(claim.canRestore()).toBe(false);
    command.mockClear();
    for (let index = 0; index < 100; index++) useAppStore.setState({ runtimeSession: { active_task_id: `update-${index}` } });
    expect(claim.canRestore()).toBe(false);
    expect(command).not.toHaveBeenCalled();
    expect(useAppStore.getState().draft).toBe("newer authored input");
  } finally { claim.release(); }
});

it("keeps restoration scoped to the original hidden owner across navigation", () => {
  const claim = claimSubmittedDraft({ conversationId: "claim-owner", workspaceRoot: "C:/claim-owner", userMessageId: "submitted", selectedCommand: () => null });
  try {
    useAppStore.setState({ conversationId: "other", workingDirectory: "C:/other", draft: "other chat input",
      conversationWorkbenchStates: { "claim-owner": emptyComposerDraft() as never } });
    expect(claim.canRestore()).toBe(true);
    expect(claim.location()).toMatchObject({ owner: "claim-owner", active: false });
    useAppStore.setState({ conversationWorkbenchStates: { "claim-owner": { ...emptyComposerDraft(), draft: "new hidden input" } as never } });
    expect(claim.canRestore()).toBe(false);
    expect(useAppStore.getState().draft).toBe("other chat input");
  } finally { claim.release(); }
});
