/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(() => false),
    })),
  });

  return {
    sendClientCommand: vi.fn(() => true),
    sendChatMessage: vi.fn((): boolean | Promise<boolean> => true),
    pushToast: vi.fn(),
    buildContextPayload: vi.fn(async () => "File: src/App.tsx\n```tsx\nexport const value = 1;\n```"),
    buildContextNativeAttachments: vi.fn(async () => ({ attachments: [], attachmentRefs: [], notes: "" })),
    promptResponse: vi.fn(async () => ({ type: "command.result", level: "success", message: "", data: {} })),
    menuSelection: "/usage",
  };
});

vi.mock("../protocol/ws-outbox", () => ({
  registerWebSocketSender: vi.fn(),
  sendClientCommand: mocks.sendClientCommand,
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed",
  sendPromptResponseCommand: mocks.promptResponse,
}));

vi.mock("../chat/sendChatMessage", () => ({
  sendChatMessage: mocks.sendChatMessage,
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: mocks.pushToast,
}));

vi.mock("./contextPayload", () => ({
  buildContextPayload: mocks.buildContextPayload,
  buildContextNativeAttachments: mocks.buildContextNativeAttachments,
}));

vi.mock("./ActionChipRegion", () => ({
  ContextChipRegion: () => null,
}));

vi.mock("./AttachmentStrip", () => ({
  AttachmentStrip: () => null,
}));

vi.mock("./ComposerTextarea", () => ({
  ComposerTextarea: ({
    value,
    placeholder,
    onChange,
    onSubmit,
  }: {
    value: string;
    placeholder?: string;
    onChange: (value: string) => void;
    onSubmit: () => void | Promise<void>;
  }) => (
    <textarea
      aria-label="composer"
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event.currentTarget.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          void onSubmit();
        }
      }}
    />
  ),
}));

vi.mock("./MenuOverlay", () => ({
  MenuOverlay: ({ open, kind, onSelect }: { open: boolean; kind: string; onSelect: (value: string) => void }) => (
    open
      ? <button type="button" onClick={() => onSelect(mocks.menuSelection)}>{kind === "skill" ? "Mock skill option" : "Mock slash option"}</button>
      : null
  ),
}));

vi.mock("./FooterRow", () => ({
  FooterRow: ({ sendState, onSend }: { sendState: "idle" | "sending" | "stop" | "disabled"; onSend: () => void | Promise<void> }) => (
    <button type="button" onClick={onSend}>{sendState === "stop" ? "Stop" : "Send"}</button>
  ),
}));

vi.mock("./uploads", () => ({
  uploadComposerFiles: vi.fn(),
}));

import { useAppStore } from "../stores";
import { Composer } from "./Composer";
import { appendPromptHistory } from "./prompt-history";

describe("Composer goal bar", () => {
  it("routes a template through the upload fence and sends attachments and quoted context", async () => {
    const quote = { id: "template-quote", role: "assistant" as const, content: "Quoted template context" };
    const uploading = { id: "template-upload", name: "notes.txt", type: "text/plain", size: 12, status: "uploading" as const, conversationId: "template-owner" };
    useAppStore.setState({ conversationId: "template-owner", workingDirectory: "", appMode: "chat", draft: "/analyze inspect",
      currentModel: "gpt-5", isConnected: true, isStreaming: false, runtimeSession: null, attachments: [uploading],
      quotedMessage: quote, selectedMentions: [], selectedSkills: [], slashPanelOpen: false, mentionPanelOpen: false,
      slashCommands: [{ name: "analyze", command: "analyze", type: "template", label: "/analyze", description: "Analyze" }] });
    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(mocks.pushToast).toHaveBeenCalledWith(expect.stringContaining("仍在上传"), "warning", 3500));
    expect(mocks.sendChatMessage).not.toHaveBeenCalled();
    act(() => useAppStore.setState({ attachments: [{ ...uploading, status: "ready", attachment: { id: "file-id", artifact_id: "artifact-id", file_name: "notes.txt", kind: "document", media_type: "text/plain" } }] }));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(mocks.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      displayContent: "/analyze inspect", backendContent: expect.stringContaining("Quoted template context"), quotedMessage: quote,
      attachments: [expect.objectContaining({ artifact_id: "artifact-id" })], attachmentRefs: [expect.objectContaining({ artifactId: "artifact-id" })], skipLocalAppend: false,
    })));
    await waitFor(() => expect(useAppStore.getState().attachments).toEqual([]));
    expect(useAppStore.getState().quotedMessage).toBeNull();
    expect(useAppStore.getState().draft).toBe("");
  });
  beforeEach(() => {
    localStorage.clear();
    mocks.sendClientCommand.mockClear();
    mocks.sendChatMessage.mockClear();
    mocks.pushToast.mockClear();
    mocks.buildContextPayload.mockClear();
    mocks.promptResponse.mockClear();
    mocks.menuSelection = "/usage";
    useAppStore.setState({
      pendingConversationSwitchId: null,
      pendingApproval: null,
      approvalQueue: [],
      pendingAskUser: null,
      askUserQueue: [],
      pendingDiffReview: null,
      diffReviewQueue: [],
      quotedMessage: null,
    });
  });

  afterEach(() => {
    cleanup();
  });

  it.each(["old draft", "/permissions auto"])("does not start preparing or dispatching %s while switching owners", async (draft) => {
    useAppStore.setState({ conversationId: "old-owner", pendingConversationSwitchId: "new-owner", draft,
      currentModel: "gpt-5", isConnected: true, isStreaming: false, attachments: [], selectedMentions: [], selectedSkills: [],
      slashPanelOpen: false, mentionPanelOpen: false });
    render(<Composer />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "composer" }), { key: "Enter" });
    await act(async () => Promise.resolve());
    expect(mocks.sendChatMessage).not.toHaveBeenCalled();
    expect(mocks.buildContextPayload).not.toHaveBeenCalled();
    expect(mocks.sendClientCommand).not.toHaveBeenCalled();
    expect(useAppStore.getState().draft).toBe(draft);
  });

  it("submits once while context preparation is pending and preserves a newly typed draft", async () => {
    let finishContext!: (value: string) => void;
    mocks.buildContextPayload.mockReturnValueOnce(new Promise((resolve) => { finishContext = resolve; }));
    useAppStore.setState({
      conversationId: "send-owner", draft: "first draft", currentModel: "gpt-5",
      isConnected: true, isStreaming: false, attachments: [], selectedMentions: [], selectedSkills: [],
      slashPanelOpen: false, mentionPanelOpen: false,
    });
    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "composer" }), { key: "Enter" });
    expect(mocks.buildContextPayload).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByRole("textbox", { name: "composer" }), { target: { value: "next draft" } });
    await act(async () => finishContext(""));
    expect(mocks.sendChatMessage).toHaveBeenCalledTimes(1);
    expect(mocks.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({ displayContent: "first draft" }));
    expect(useAppStore.getState().draft).toBe("next draft");
  });

  it.each([true, false])("clears only the original owner's unchanged composer after a delayed submission returns %s", async (accepted) => {
    let finishSend!: (value: boolean) => void;
    mocks.sendChatMessage.mockReturnValueOnce(new Promise((resolve) => { finishSend = resolve; }));
    useAppStore.setState({ conversationId: "send-owner-a", workingDirectory: "C:/owner-a", draft: "A requested task",
      currentModel: "gpt-5", isConnected: true, isStreaming: false, attachments: [], selectedMentions: [], selectedSkills: [],
      quotedMessage: null, slashPanelOpen: false, mentionPanelOpen: false });
    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(mocks.sendChatMessage).toHaveBeenCalled());
    expect(useAppStore.getState().draft).toBe("A requested task");
    act(() => {
      useAppStore.getState().snapshotWorkbenchState("send-owner-a");
      useAppStore.setState({ conversationId: "send-owner-b", workingDirectory: "C:/owner-b", draft: "B unsent task" });
    });
    await act(async () => { finishSend(accepted); });

    expect(useAppStore.getState().draft).toBe("B unsent task");
    expect(useAppStore.getState().conversationWorkbenchStates["send-owner-a"].draft).toBe(accepted ? "" : "A requested task");
  });

  it("keeps raw display text and quoted metadata separate from references in the assembled input", async () => {
    const quote = { id: "quoted-A", role: "assistant" as const, content: "exact quote" };
    const mention = { kind: "file" as const, name: "App.tsx", path: "src/App.tsx" };
    useAppStore.setState({ conversationId: "display-owner", workingDirectory: "C:/A", draft: "original @literal",
      currentModel: "gpt-5", isConnected: true, isStreaming: false, attachments: [],
      selectedMentions: [mention], selectedSkills: [], quotedMessage: quote, slashPanelOpen: false, mentionPanelOpen: false });
    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(mocks.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      displayContent: "original @literal", quotedMessage: quote, contextRefs: [mention],
      backendContent: expect.stringContaining("Quoted Assistant message:\nexact quote\n\noriginal @literal"),
    })));
  });

  it.each(["quote", "annotation"])("preserves a newer %s with the same identity during context preparation", async (changed) => {
    let finishContext!: (value: string) => void;
    mocks.buildContextPayload.mockReturnValueOnce(new Promise((resolve) => { finishContext = resolve; }));
    const quote = { id: "same-quote", role: "assistant" as const, content: "old quote" };
    const mention = { kind: "browser_annotation" as const, name: "target", path: "https://example.test",
      url: "https://example.test", note: "old note" };
    useAppStore.setState({ conversationId: "display-owner", workingDirectory: "C:/A", draft: "unchanged draft",
      currentModel: "gpt-5", isConnected: true, isStreaming: false, attachments: [],
      selectedMentions: [mention], selectedSkills: [], quotedMessage: quote, slashPanelOpen: false, mentionPanelOpen: false });
    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    act(() => useAppStore.setState(changed === "quote"
      ? { quotedMessage: { ...quote, content: "new quote" } }
      : { selectedMentions: [{ ...mention, note: "new note" }] }));
    await act(async () => finishContext(""));
    expect(useAppStore.getState().draft).toBe("unchanged draft");
    expect(changed === "quote" ? useAppStore.getState().quotedMessage?.content : useAppStore.getState().selectedMentions[0]?.kind === "browser_annotation"
      ? useAppStore.getState().selectedMentions[0].note : "").toBe(changed === "quote" ? "new quote" : "new note");
  });

  it("marks Code mode for the wide composer axis", () => {
    useAppStore.setState({
      appMode: "code",
      draft: "",
      currentModel: "gpt-5",
      isConnected: true,
      isStreaming: false,
      attachments: [],
      selectedSkills: [],
    });

    const { container } = render(<Composer />);

    expect(container.querySelector(".composer-container")?.getAttribute("data-layout-mode")).toBe("code");
  });

  it("uses the same code-layout composer in Cowork mode", () => {
    useAppStore.setState({ appMode: "cowork" });

    const { container } = render(<Composer />);

    expect(container.querySelector(".composer-container")?.getAttribute("data-layout-mode")).toBe("code");
  });

  it("shows an active goal and sends pause or clear actions", async () => {
    useAppStore.setState({
      conversationId: "conv-1",
      activeGoal: {
        id: "goal-1",
        text: "Match MiniCode desktop goal mode",
        status: "active",
      },
      appMode: "chat",
      draft: "",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);
    mocks.sendClientCommand.mockClear();

    expect(screen.getByText("目标")).toBeTruthy();
    expect(screen.getByText("Match MiniCode desktop goal mode")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "暂停目标" }));
    expect(mocks.sendClientCommand).toHaveBeenCalledWith({
      type: "conversation.goal.set",
      conversation_id: "conv-1",
      action: "pause",
      source: "frontend.goal_bar",
    });

    fireEvent.click(screen.getByRole("button", { name: "清除目标" }));
    expect(mocks.sendClientCommand).toHaveBeenCalledWith({
      type: "conversation.goal.set",
      conversation_id: "conv-1",
      action: "clear",
      source: "frontend.goal_bar",
    });
  });

  it("sends resume for a paused goal", async () => {
    useAppStore.setState({
      conversationId: "conv-2",
      activeGoal: {
        id: "goal-2",
        text: "Continue the desktop parity pass",
        status: "paused",
      },
      appMode: "chat",
      draft: "",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);
    mocks.sendClientCommand.mockClear();

    expect(screen.getByText("已暂停")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "继续目标" }));
    expect(mocks.sendClientCommand).toHaveBeenCalledWith({
      type: "conversation.goal.set",
      conversation_id: "conv-2",
      action: "resume",
      source: "frontend.goal_bar",
    });
  });

  it("sends slash commands with inline file context in backend content", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    useAppStore.setState({
      conversationId: "conv-slash",
      appMode: "chat",
      draft: "/review @file:src/App.tsx",
      currentModel: "gpt-5",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedMentions: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);

    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(mocks.sendChatMessage).toHaveBeenCalled());
    expect(mocks.buildContextPayload).toHaveBeenCalledWith([
      { path: "src/App.tsx", name: "App.tsx", kind: "file" },
    ]);
    expect(mocks.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      displayContent: "/review",
      backendContent: expect.stringContaining("File: src/App.tsx"),
      skipLocalAppend: true,
      contextRefs: [
        { path: "src/App.tsx", name: "App.tsx", kind: "file" },
      ],
    }));
    expect(useAppStore.getState().draft).toBe("");
  });

  it("renders a quoted message placeholder and sends it as backend-only context", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    mocks.buildContextPayload.mockResolvedValueOnce("");
    useAppStore.setState({
      conversationId: "conv-quote",
      appMode: "chat",
      draft: "继续解释一下",
      currentModel: "gpt-5",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedMentions: [],
      selectedSkills: [],
      quotedMessage: {
        id: "assistant-quoted",
        role: "assistant",
        content: "上一条助手回复里比较长的内容",
      },
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);

    expect(screen.getByText("回复 助手")).toBeTruthy();
    expect(screen.getByText("上一条助手回复里比较长的内容")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(mocks.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      displayContent: "继续解释一下",
      backendContent: [
        "Quoted Assistant message:",
        "上一条助手回复里比较长的内容",
        "",
        "继续解释一下",
      ].join("\n"),
    })));
    expect(useAppStore.getState().quotedMessage).toBeNull();
  });

  it("routes slash menu protocol commands through the shared runtime executor", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    mocks.buildContextPayload.mockResolvedValueOnce("");
    mocks.menuSelection = "/usage";
    useAppStore.setState({
      conversationId: "conv-menu-protocol",
      appMode: "chat",
      draft: "/",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: true,
      mentionPanelOpen: false,
      attachments: [],
      selectedMentions: [],
      selectedSkills: [],
      availableSkills: [],
      slashCommands: [
        { name: "usage", command: "usage", label: "/usage", description: "Usage", type: "protocol" },
      ],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);

    fireEvent.click(screen.getByText("Mock slash option"));

    await waitFor(() => expect(mocks.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      displayContent: "/usage",
      backendContent: "/usage",
      skipLocalAppend: true,
    })));
  });

  it("does not send or clear the composer while an attachment is still uploading", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    useAppStore.setState({
      conversationId: "conv-uploading",
      appMode: "chat",
      draft: "please inspect this screenshot",
      currentModel: "gpt-5",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [{
        id: "att-uploading",
        name: "screen.png",
        type: "image/png",
        size: 2048,
        status: "uploading",
      }],
      selectedMentions: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);

    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(mocks.pushToast).toHaveBeenCalledWith(
      '“screen.png”仍在上传，请等待完成后发送。',
      "warning",
      3500,
    ));
    expect(mocks.sendChatMessage).not.toHaveBeenCalled();
    expect(useAppStore.getState().draft).toBe("please inspect this screenshot");
    expect(useAppStore.getState().attachments).toHaveLength(1);
  });

  it("sends a pasted-text attachment as the whole user message when the draft is empty", async () => {
    mocks.buildContextPayload.mockResolvedValueOnce("");
    const attachmentPayload = {
      id: "artifact-paste",
      file_name: "pasted-4.txt",
      kind: "document",
      media_type: "text/plain",
      artifact_id: "artifact-paste",
      doc_id: "doc-paste",
      input_source: "pasted_text",
      source_char_count: 25_000,
    };
    useAppStore.setState({
      conversationId: "conv-paste",
      appMode: "chat",
      draft: "",
      currentModel: "gpt-5",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [{
        id: "composer-paste",
        name: "pasted-4.txt",
        type: "text/plain",
        size: 25_000,
        status: "ready",
        artifactId: "artifact-paste",
        docId: "doc-paste",
        attachment: attachmentPayload,
        conversationId: "conv-paste",
        inputSource: "pasted_text",
        sourceCharCount: 25_000,
      }],
      selectedMentions: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(mocks.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      displayContent: "",
      backendContent: "",
      attachments: [attachmentPayload],
    })));
    expect(useAppStore.getState().attachments).toHaveLength(0);
  });

  it("resends a recalled durable attachment in its original conversation", async () => {
    mocks.buildContextPayload.mockResolvedValueOnce("");
    const attachmentPayload = {
      id: "att-original",
      file_name: "design.pdf",
      kind: "document",
      media_type: "application/pdf",
      artifact_id: "artifact-design",
      doc_id: "doc-design",
      size_bytes: 4096,
    };
    useAppStore.setState({
      conversationId: "conv-recall",
      appMode: "chat",
      draft: "review this again",
      currentModel: "gpt-5",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [{
        id: "att-recall-artifact-design",
        name: "design.pdf",
        type: "application/pdf",
        size: 4096,
        status: "ready",
        conversationId: "conv-recall",
        artifactId: "artifact-design",
        docId: "doc-design",
        attachment: attachmentPayload,
      }],
      selectedMentions: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(mocks.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      displayContent: "review this again",
      backendContent: "review this again",
      attachments: [attachmentPayload],
      conversationId: "conv-recall",
      attachmentRefs: [expect.objectContaining({
        artifactId: "artifact-design",
        docId: "doc-design",
        name: "design.pdf",
      })],
    })));
    expect(useAppStore.getState().attachments).toHaveLength(0);
  });

  it("keeps an invalid recalled attachment visible and explains that it must be re-uploaded", async () => {
    useAppStore.setState({
      conversationId: "conv-recall-invalid",
      appMode: "chat",
      draft: "retry this",
      currentModel: "gpt-5",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [{
        id: "att-recall-invalid",
        name: "missing.txt",
        type: "text/plain",
        size: 10,
        status: "error",
        conversationId: "conv-recall-invalid",
        error: "原附件缺少可验证的持久化引用，请重新上传。",
      }],
      selectedMentions: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(mocks.pushToast).toHaveBeenCalledWith(
      "“missing.txt”原附件缺少可验证的持久化引用，请重新上传。",
      "warning",
      3500,
    ));
    expect(mocks.sendChatMessage).not.toHaveBeenCalled();
    expect(useAppStore.getState().attachments).toHaveLength(1);
  });

  it("routes template slash menu commands into composer command mode", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    mocks.menuSelection = "/review";
    useAppStore.setState({
      conversationId: "conv-menu-template",
      appMode: "chat",
      draft: "/",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: true,
      mentionPanelOpen: false,
      attachments: [],
      selectedMentions: [],
      selectedSkills: [],
      availableSkills: [],
      slashCommands: [
        { name: "review", command: "review", label: "/review", description: "Review", type: "template" },
      ],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);

    fireEvent.click(screen.getByText("Mock slash option"));

    await waitFor(() => expect(screen.getByPlaceholderText("补充指令…")).toBeTruthy());
    expect(mocks.sendChatMessage).not.toHaveBeenCalled();
  });

  it("enters a second-level picker for /skill before selecting a skill", async () => {
    mocks.menuSelection = "/skill";
    useAppStore.setState({
      conversationId: "conv-menu-skill",
      appMode: "chat",
      draft: "/skill",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: true,
      mentionPanelOpen: false,
      attachments: [],
      selectedMentions: [],
      selectedSkills: [],
      availableSkills: [
        { name: "openai-docs", description: "Use official OpenAI docs", source_level: "builtin" },
      ],
      slashCommands: [
        { name: "skill", command: "skill", label: "/skill", description: "Choose a skill", type: "local" },
      ],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);
    fireEvent.click(screen.getByText("Mock slash option"));

    expect(useAppStore.getState().draft).toBe("/skill ");
    expect(useAppStore.getState().slashPanelOpen).toBe(true);
    expect(mocks.sendChatMessage).not.toHaveBeenCalled();

    mocks.menuSelection = "skill-name:openai-docs";
    fireEvent.click(screen.getByText("Mock slash option"));

    expect(useAppStore.getState().selectedSkills).toMatchObject([
      { name: "openai-docs", sourceLevel: "builtin" },
    ]);
    expect(useAppStore.getState().draft).toBe("");
  });

  it("turns an explicit $skill picker selection into a composer skill chip", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    mocks.menuSelection = "skill-name:openai-docs";
    useAppStore.setState({
      conversationId: "conv-skill-picker",
      appMode: "chat",
      draft: "",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedMentions: [],
      selectedSkills: [],
      availableSkills: [
        { name: "openai-docs", description: "Use official OpenAI docs", source_level: "builtin" },
      ],
      slashCommands: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);

    fireEvent.change(screen.getByLabelText("composer"), { target: { value: "$open" } });
    await waitFor(() => expect(screen.getByText("Mock skill option")).toBeTruthy());
    fireEvent.click(screen.getByText("Mock skill option"));

    expect(useAppStore.getState().selectedSkills).toMatchObject([
      { name: "openai-docs", description: "Use official OpenAI docs", sourceLevel: "builtin" },
    ]);
    expect(useAppStore.getState().draft).toBe("");
  });

  it("does not add git chrome to the code composer", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    useAppStore.setState({
      conversationId: "conv-diff",
      appMode: "code",
      draft: "",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedSkills: [],
      gitChanges: {
        workingTree: [
          {
            path: "src/app.ts",
            patch: "diff --git a/src/app.ts b/src/app.ts\n@@\n-old\n+new",
            additions: 1,
            deletions: 1,
          },
        ],
        staged: [],
        untracked: [],
        loading: false,
      },
      diffReview: null,
      rightPanelOpen: false,
      rightStackTab: "preview",
      rightStackTabLocked: false,
    });

    render(<Composer />);

    expect(screen.queryByText("Commit changes")).toBeNull();
    expect(screen.queryByRole("button", { name: /Review diff/ })).toBeNull();
  });

  it("replaces the composer with its approval and restores the same draft, selection and attachment after acceptance", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    useAppStore.setState({
      conversationId: "conv-approval",
      appMode: "chat",
      draft: "unfinished task",
      currentModel: "gpt-5",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
      pendingApproval: null,
      approvalQueue: [],
    });

    const { container } = render(<Composer />);
    const input = screen.getByRole("textbox", { name: "composer" }) as HTMLTextAreaElement;
    input.setSelectionRange(3, 7);
    const attachment = { id: "draft-file", name: "notes.txt", type: "text/plain", size: 4, status: "ready" as const,
      conversationId: "conv-approval", artifactId: "draft-artifact",
      attachment: { id: "draft-artifact", artifact_id: "draft-artifact", file_name: "notes.txt", media_type: "text/plain", kind: "document" } };
    act(() => useAppStore.setState({
      attachments: [attachment],
      pendingApproval: {
        requestId: "approval-inline",
        conversationId: "conv-approval",
        toolName: "run_command",
        args: { command: "npm test" },
      },
    }));

    expect(container.querySelector(".composer-container")?.textContent).toContain("允许 MiniCode 运行此命令？");
    expect(container.querySelector(".composer-container")?.getAttribute("data-approval-replacement")).toBe("true");
    expect(screen.queryByRole("textbox", { name: "composer" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    expect(container.querySelector("textarea")).toBe(input);
    expect(input.closest(".composer-input-region")?.hasAttribute("hidden")).toBe(true);
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.click(screen.getByText("Send", { selector: "button" }));
    await act(async () => Promise.resolve());
    expect(mocks.sendChatMessage).not.toHaveBeenCalled();
    expect(mocks.buildContextPayload).not.toHaveBeenCalled();
    expect(mocks.promptResponse).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "允许使用工具" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "composer" })).toBe(input));
    expect(input.value).toBe("unfinished task");
    expect([input.selectionStart, input.selectionEnd]).toEqual([3, 7]);
    expect(useAppStore.getState().attachments).toEqual([attachment]);
    expect(container.querySelector(".composer-container")?.getAttribute("data-approval-replacement")).toBe("false");
  });

  it("replaces input only for the current owner's queued approval and suppresses old approval during a switch", () => {
    useAppStore.setState({ conversationId: "composer-owner", draft: "kept draft", currentModel: "gpt-5",
      isConnected: true, isStreaming: false, attachments: [], selectedMentions: [], selectedSkills: [],
      slashPanelOpen: false, mentionPanelOpen: false,
      pendingApproval: { requestId: "other-approval", conversationId: "another-owner", toolName: "read_file", args: {} },
    });
    const { container } = render(<Composer />);
    const input = screen.getByRole("textbox", { name: "composer" });
    expect(screen.queryByText("权限")).toBeNull();
    act(() => useAppStore.setState({ approvalQueue: [
      { requestId: "owned-approval", conversationId: "composer-owner", toolName: "run_command", args: { command: "npm test" } },
    ] }));
    expect(screen.queryByRole("textbox", { name: "composer" })).toBeNull();
    expect(screen.getByText("允许 MiniCode 运行此命令？")).toBeTruthy();
    act(() => useAppStore.setState({ pendingConversationSwitchId: "another-owner" }));
    expect(screen.queryByText("权限")).toBeNull();
    expect(container.querySelector("textarea")).toBe(input);
    expect(container.querySelector(".composer-container")?.getAttribute("data-approval-replacement")).toBe("false");
    act(() => useAppStore.setState({ pendingConversationSwitchId: null, conversationId: "new-owner" }));
    expect(screen.getByRole("textbox", { name: "composer" })).toBe(input);
    expect(screen.queryByText("权限")).toBeNull();
  });

  it("keeps the ordinary composer alongside a question", () => {
    useAppStore.setState({ conversationId: "question-owner", draft: "kept draft", currentModel: "gpt-5",
      isConnected: true, isStreaming: false, attachments: [], selectedMentions: [], selectedSkills: [],
      slashPanelOpen: false, mentionPanelOpen: false,
      pendingAskUser: { requestId: "question", conversationId: "question-owner", question: "Choose a direction" },
    });
    const { container } = render(<Composer />);
    expect(screen.getByRole("textbox", { name: "composer" })).toBeTruthy();
    expect(screen.getByText("Choose a direction")).toBeTruthy();
    expect(container.querySelector(".composer-container")?.getAttribute("data-approval-replacement")).toBe("false");
  });

  it("suspends the saved input menu while approval replaces it and restores the menu afterward", () => {
    useAppStore.setState({ conversationId: "menu-owner", draft: "/usage", currentModel: "gpt-5",
      isConnected: true, isStreaming: false, attachments: [], selectedMentions: [], selectedSkills: [],
      slashPanelOpen: true, mentionPanelOpen: false,
    });
    const { container } = render(<Composer />);
    expect(screen.getByText("Mock slash option")).toBeTruthy();
    act(() => useAppStore.setState({ pendingApproval: {
      requestId: "menu-approval", conversationId: "menu-owner", toolName: "run_command", args: { command: "npm test" },
    } }));
    expect(container.querySelector(".composer-input-region")?.textContent).not.toContain("Mock slash option");
    expect(useAppStore.getState().slashPanelOpen).toBe(true);
    act(() => useAppStore.getState().clearApproval("menu-approval"));
    expect(screen.getByText("Mock slash option")).toBeTruthy();
    expect(useAppStore.getState().draft).toBe("/usage");
  });

  it("uses the queued file review's owner to replace input and keeps unrelated reviews out", () => {
    useAppStore.setState({ conversationId: "review-owner", draft: "kept draft", currentModel: "gpt-5",
      isConnected: true, isStreaming: false, attachments: [], selectedMentions: [], selectedSkills: [],
      slashPanelOpen: false, mentionPanelOpen: false,
      pendingDiffReview: { requestId: "other-review", conversationId: "another-owner", filePath: "other.ts", diff: "+other" },
    });
    const { container } = render(<Composer />);
    const input = screen.getByRole("textbox", { name: "composer" });
    act(() => useAppStore.setState({ diffReviewQueue: [
      { requestId: "owned-review", conversationId: "review-owner", filePath: "app.ts", diff: "-old\n+new" },
    ] }));
    expect(screen.queryByRole("textbox", { name: "composer" })).toBeNull();
    expect(container.querySelector("textarea")).toBe(input);
    expect(container.querySelector(".composer-container")?.getAttribute("data-approval-replacement")).toBe("true");
    expect(container.querySelector(".inline-agent-prompt")?.textContent).not.toContain("other.ts");
    act(() => useAppStore.getState().clearDiffReview("owned-review"));
    expect(screen.getByRole("textbox", { name: "composer" })).toBe(input);
    expect(useAppStore.getState().pendingDiffReview?.requestId).toBe("other-review");
  });

  it("hides review diff in code mode until changes exist", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    useAppStore.setState({
      conversationId: "conv-empty-diff",
      appMode: "code",
      draft: "",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);

    expect(screen.queryByRole("button", { name: /Review diff/ })).toBeNull();
  });

  it("does not interrupt a streaming turn when Enter is pressed in an empty composer", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    useAppStore.setState({
      conversationId: "conv-streaming-enter",
      appMode: "chat",
      draft: "",
      isConnected: true,
      isStreaming: true,
      conversationStreaming: { "conv-streaming-enter": true },
      messages: [{
        id: "assistant-running",
        role: "assistant",
        content: "",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);
    mocks.sendClientCommand.mockClear();

    fireEvent.keyDown(screen.getByLabelText("composer"), { key: "Enter" });

    expect(mocks.sendClientCommand).not.toHaveBeenCalledWith({ type: "interrupt" });
    expect(useAppStore.getState().isStreaming).toBe(true);
  });

  it("opens workspace prompt history from the global Ctrl+R event and restores a prompt", async () => {
    const workspace = "C:\\Desktop\\MiniCode";
    appendPromptHistory(workspace, "inspect the queue ordering");
    useAppStore.setState({
      conversationId: "conv-history",
      workingDirectory: workspace,
      appMode: "chat",
      draft: "",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });
    render(<Composer />);

    fireEvent(window, new Event("composer:history-search"));

    expect(screen.getByLabelText("搜索输入历史")).toBeTruthy();
    fireEvent.click(screen.getByText("inspect the queue ordering"));
    await waitFor(() => expect(useAppStore.getState().draft).toBe("inspect the queue ordering"));
    expect(screen.queryByLabelText("搜索输入历史")).toBeNull();
  });

  it("queues typed input when Enter is pressed during a streaming turn", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    useAppStore.setState({
      conversationId: "conv-streaming-queue",
      appMode: "chat",
      draft: "do this next",
      isConnected: true,
      isStreaming: true,
      currentModel: "gpt-test",
      conversationStreaming: { "conv-streaming-queue": true },
      messages: [{
        id: "assistant-running",
        role: "assistant",
        content: "",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);
    mocks.sendChatMessage.mockClear();

    fireEvent.keyDown(screen.getByLabelText("composer"), { key: "Enter" });

    await waitFor(() => expect(mocks.sendChatMessage).toHaveBeenCalledWith(expect.objectContaining({
      allowWhileStreaming: true,
    })));
    expect(useAppStore.getState().draft).toBe("");
    expect(mocks.sendClientCommand).not.toHaveBeenCalledWith(expect.objectContaining({ type: "interrupt" }));
  });

  it("keeps streaming until the backend confirms the Stop terminal event", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    useAppStore.setState({
      conversationId: "conv-streaming-stop",
      appMode: "chat",
      draft: "",
      isConnected: true,
      isStreaming: true,
      conversationStreaming: { "conv-streaming-stop": true },
      messages: [{
        id: "assistant-running",
        role: "assistant",
        content: "",
        artifacts: [],
        timestamp: 1,
        isStreaming: true,
      }],
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    });

    render(<Composer />);
    mocks.sendClientCommand.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));

    expect(mocks.sendClientCommand).toHaveBeenCalledWith({
      type: "interrupt",
      conversation_id: "conv-streaming-stop",
      message_id: "assistant-running",
    });
    expect(useAppStore.getState().isStreaming).toBe(true);
  });
});
