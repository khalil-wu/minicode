/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    sendChatMessage: vi.fn(() => true),
    pushToast: vi.fn(),
    buildContextPayload: vi.fn(async () => "File: src/App.tsx\n```tsx\nexport const value = 1;\n```"),
    buildContextNativeAttachments: vi.fn(async () => ({ attachments: [], attachmentRefs: [], notes: "" })),
    menuSelection: "/usage",
  };
});

vi.mock("../protocol/ws-outbox", () => ({
  registerWebSocketSender: vi.fn(),
  sendClientCommand: mocks.sendClientCommand,
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
  beforeEach(() => {
    localStorage.clear();
    mocks.sendClientCommand.mockClear();
    mocks.sendChatMessage.mockClear();
    mocks.pushToast.mockClear();
    mocks.buildContextPayload.mockClear();
    mocks.menuSelection = "/usage";
    useAppStore.setState({
      pendingApproval: null,
      approvalQueue: [],
      pendingAskUser: null,
      pendingDiffReview: null,
      quotedMessage: null,
    });
  });

  afterEach(() => {
    cleanup();
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

  it("renders pending tool approval inside the composer", async () => {
    const { useAppStore } = await import("../stores");
    const { Composer } = await import("./Composer");

    useAppStore.setState({
      conversationId: "conv-approval",
      appMode: "chat",
      draft: "",
      currentModel: "gpt-5",
      isConnected: true,
      isStreaming: false,
      slashPanelOpen: false,
      mentionPanelOpen: false,
      attachments: [],
      selectedSkills: [],
      gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
      pendingApproval: {
        requestId: "approval-inline",
        conversationId: "conv-approval",
        toolName: "run_command",
        args: { command: "npm test" },
      },
      approvalQueue: [],
    });

    const { container } = render(<Composer />);

    expect(container.querySelector(".composer-container")?.textContent).toContain("允许使用 运行命令？");
    expect(screen.queryByRole("dialog")).toBeNull();
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
