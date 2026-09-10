import { beforeEach, describe, expect, it } from "vitest";

import { useAppStore } from "./index";


describe("side-chat selected context", () => {
  beforeEach(() => {
    useAppStore.setState({
      sideChatOpen: false,
      sideChatPendingContext: null,
      sideChats: {},
      messages: [],
    });
  });

  it("opens the panel and consumes the selected text into the new thread", () => {
    useAppStore.getState().openSideChatWithSelection("const answer = 42;", "src/app.ts");

    expect(useAppStore.getState().sideChatOpen).toBe(true);
    expect(useAppStore.getState().sideChatPendingContext).toEqual({
      text: "const answer = 42;",
      source: "src/app.ts",
    });

    useAppStore.getState().ensureSideChat("side-selection");
    const state = useAppStore.getState();
    expect(state.sideChatPendingContext).toBeNull();
    expect(state.sideChats["side-selection"]?.selectedContext).toEqual({
      text: "const answer = 42;",
      source: "src/app.ts",
    });
  });

  it("updates the open side-chat thread when a new selection is requested", () => {
    useAppStore.getState().ensureSideChat("side-existing");
    useAppStore.setState({ sideChatOpen: true });
    useAppStore.getState().openSideChatWithSelection("new selection", "README.md");

    expect(useAppStore.getState().sideChats["side-existing"]?.selectedContext).toEqual({
      text: "new selection",
      source: "README.md",
    });
  });

  it("resumes the existing side-chat message without unpausing the main conversation", () => {
    useAppStore.setState({
      conversationId: "main", isPaused: true, conversationMessages: {},
      sideChats: { side: { id: "side", draft: "", isStreaming: false, messages: [
        { id: "side-answer", role: "assistant", content: "partial", blocks: [], artifacts: [], timestamp: 1 },
      ] } },
    });
    useAppStore.getState().resumeStreaming("side", [], "side-answer", "side-turn");
    const state = useAppStore.getState();
    expect(state.isPaused).toBe(true);
    expect(state.sideChats.side.isStreaming).toBe(true);
    expect(state.sideChats.side.messages).toHaveLength(1);
    expect(state.sideChats.side.messages[0]).toMatchObject({ id: "side-answer", isStreaming: true, turnId: "side-turn" });
    expect(state.conversationMessages.side).toBeUndefined();
  });
});
