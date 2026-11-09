/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Profiler } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ChatContextCard, collectAttachments } from "./ChatContextCard";
import { hydrateMessages } from "./transcriptHydration";
import * as desktopRuntime from "../desktop/runtime";
import type { EmbeddedBrowserState } from "../desktop/runtime";
import * as protocolApi from "../protocol/api";
import { readFileWorkspacePreview } from "../protocol/file-workspace";

vi.mock("../hooks/useWebSocket", () => ({
  getWebSocket: () => ({ sessionId: "session-context-image" }),
}));

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

describe("ChatContextCard", () => {
  it("does not present native guest navigation commands as newly discovered browser pages", async () => {
    let publish!: (event: EmbeddedBrowserState) => void;
    vi.spyOn(desktopRuntime, "isDesktop").mockReturnValue(true);
    vi.spyOn(desktopRuntime, "embeddedBrowserList").mockResolvedValue([]);
    vi.spyOn(desktopRuntime, "onEmbeddedBrowserEvent").mockImplementation(listener => { publish = listener; return () => {}; });
    render(<ChatContextCard />);
    await waitFor(() => expect(desktopRuntime.embeddedBrowserList).toHaveBeenCalledWith("conv-active"));
    const command: EmbeddedBrowserState = { id: "command-native", conversationId: "conv-active", type: "numbered-shortcut", title: "Command is not a browser page", url: "https://source.example/", loading: false, canGoBack: false, canGoForward: false,
      numberedShortcut: { target: "tabs", index: 1 } };
    act(() => publish(command));
    act(() => publish({ ...command, type: "external-link-request", requestedUrl: "https://destination.example/" }));
    expect(screen.queryByText("Command is not a browser page")).toBeNull();
  });

  it("rejects browser-list results from the previous context-card owner", async () => {
    let finishOld!: (targets: EmbeddedBrowserState[]) => void;
    const old = new Promise<EmbeddedBrowserState[]>((resolve) => { finishOld = resolve; });
    const target: EmbeddedBrowserState = { id: "native-new", conversationId: "conv-new", type: "updated", url: "https://new.example/", title: "New owner page", loading: false, canGoBack: false, canGoForward: false };
    vi.spyOn(desktopRuntime, "isDesktop").mockReturnValue(true);
    vi.spyOn(desktopRuntime, "embeddedBrowserList").mockImplementation((owner) => owner === "conv-active" ? old : Promise.resolve([target]));
    vi.spyOn(desktopRuntime, "onEmbeddedBrowserEvent").mockReturnValue(vi.fn());
    const activate = vi.spyOn(desktopRuntime, "embeddedBrowserActivate").mockResolvedValue(null);
    render(<ChatContextCard />);
    act(() => useAppStore.setState({ conversationId: "conv-new" }));
    await screen.findByText("New owner page");
    await act(async () => finishOld([{ ...target, id: "native-old", conversationId: "conv-active", title: "Old owner page" }]));
    expect(screen.queryByText("Old owner page")).toBeNull();
    fireEvent.click(screen.getByText("New owner page"));
    expect(activate).toHaveBeenCalledWith("conv-new", "native-new");
  });
  beforeEach(() => {
    useAppStore.setState({
      appMode: "cowork",
      contextCardCollapsed: false,
      contextCardPinned: false,
      compactPanel: null,
      defaultTerminalLocation: "bottom",
      conversationId: "conv-active",
      messages: [{
        id: "assistant-1",
        role: "assistant",
        content: "Done [1]",
        timestamp: 1,
        artifacts: [],
        attachmentRefs: [{
          id: "source-image",
          name: "layout-reference.png",
          kind: "image",
          mediaType: "image/png",
          sizeBytes: 128,
        }],
        citations: [{
          source: "https://docs.example.com/layout",
          url: "https://docs.example.com/layout",
          title: "Layout guide",
          range: [0, 0],
        }],
      }],
      workingDirectory: "C:\\Desktop\\MiniCode",
      workspaceGit: { branch: "codex/ui-polish", isWorktree: false, currentPath: "C:\\Desktop\\MiniCode" },
      gitChanges: {
        workingTree: [{ path: "src/app.ts", additions: 2, deletions: 1 }],
        staged: [],
        untracked: ["src/new.ts"],
        loading: false,
      },
      subagents: [{
        id: "subagent-layout",
        role: "reviewer",
        status: "running",
        objective: "Audit layout",
      }],
      focusedSubagentId: null,
      backgroundTasks: [{ id: "bg-1", command: "npm test", status: "running", timestamp: 1, conversationId: "conv-active" }],
      terminalSessions: [],
      rightStackTab: "tasks",
      rightPanelOpen: false,
      isConnected: false,
      turnDiffs: {},
    });
  });

  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("removes a closed browser target from the summary while retaining targets owned by this conversation", async () => {
    let onBrowserEvent!: (event: EmbeddedBrowserState) => void;
    const first: EmbeddedBrowserState = { id: "page-one", conversationId: "conv-active", type: "updated", url: "https://one.example/", title: "First preview", loading: false, canGoBack: false, canGoForward: false };
    const second = { ...first, id: "page-two", url: "https://two.example/", title: "Second preview" };
    vi.spyOn(desktopRuntime, "isDesktop").mockReturnValue(true);
    vi.spyOn(desktopRuntime, "embeddedBrowserList").mockResolvedValue([first, second]);
    vi.spyOn(desktopRuntime, "onEmbeddedBrowserEvent").mockImplementation((callback) => { onBrowserEvent = callback; return vi.fn(); });
    render(<ChatContextCard />);
    await screen.findByText("First preview");
    act(() => onBrowserEvent({ ...first, conversationId: "conv-other", type: "closed", url: "" }));
    expect(screen.getByText("First preview")).toBeTruthy();
    act(() => onBrowserEvent({ ...first, type: "closed", url: "" }));
    await waitFor(() => expect(screen.queryByText("First preview")).toBeNull());
    expect(screen.getByText("Second preview")).toBeTruthy();
  });

  it("summarizes failed, stopped, waiting and completed agents truthfully", () => {
    useAppStore.setState({ subagents: [
      { id: "running", role: "explore", status: "running", objective: "检查代码" },
      { id: "failed", role: "explore", status: "error", objective: "检查样式" },
      { id: "stopped", role: "explore", status: "cancelled", terminationInitiator: "user" },
      { id: "waiting", role: "explore", status: "blocked", blockedBy: ["build"] },
      { id: "done", role: "explore", status: "done" },
    ] });
    render(<ChatContextCard />);
    expect(screen.getByRole("button", { name: "1 失败 · 1 已停止 · 1 运行中 · 1 等待中 · 1 已完成" })).toBeTruthy();
    expect(screen.queryByText("4 个运行中")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：检查样式" }));
    expect(useAppStore.getState().focusedSubagentId).toBe("failed");
    expect(useAppStore.getState().rightStackTab).toBe("subagents");
  });

  it("does not render the context card for text-only stream updates", () => {
    const onRender = vi.fn();
    render(<Profiler id="context" onRender={onRender}><ChatContextCard /></Profiler>);
    onRender.mockClear();
    act(() => useAppStore.setState((state) => ({ messages: state.messages.map((message) => ({ ...message, content: message.content + " next" })) })));
    expect(onRender).not.toHaveBeenCalled();
    act(() => useAppStore.setState((state) => ({ messages: state.messages.map((message) => ({ ...message, content: "Done [1] [2]", citations: [...(message.citations ?? []), { source: "https://example.com/new", url: "https://example.com/new", title: "New source", range: [0, 0] }] })) })));
    expect(screen.getByText("New source")).toBeTruthy();
    expect(onRender).toHaveBeenCalled();
  });

  it("renders a focused context summary with shared sources and compact workspace", () => {
    const { container } = render(<ChatContextCard />);

    expect(screen.getByRole("dialog", { name: "工作区上下文摘要" })).toBeTruthy();
    expect(container.querySelector(".mc-chat-context-card-header")?.textContent).toContain("MiniCode");
    expect(screen.queryByText("附件")).toBeNull();
    expect(screen.getByRole("region", { name: "来源摘要" })).toBeTruthy();
    expect(screen.getByText("layout-reference.png")).toBeTruthy();
    expect(screen.getByText("Layout guide")).toBeTruthy();
    expect(screen.getByText("后台任务")).toBeTruthy();
    expect(screen.getByRole("button", { name: "查看工作区：MiniCode" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "查看工作区：MiniCode" }).title).toContain("codex/ui-polish");
    expect(container.querySelector('.brand-icon-remote')?.getAttribute('src')).toContain('https://www.google.com/s2/favicons?domain=');
    expect(container.querySelector('[data-brand="generic"] svg')).toBeTruthy();
  });

  it("closes the summary without leaving a second toolbar glyph in Cowork", () => {
    render(<ChatContextCard />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(useAppStore.getState().contextCardCollapsed).toBe(true);
    expect(screen.queryByRole("dialog", { name: "工作区上下文摘要" })).toBeNull();
    expect(screen.queryByRole("button", { name: "展开上下文卡片" })).toBeNull();
    act(() => useAppStore.getState().setContextCardCollapsed(false));
    expect(screen.getByText("layout-reference.png")).toBeTruthy();
    expect(screen.getByText("Layout guide")).toBeTruthy();
  });

  it("leaves the chat summary closed without an overlapping floating glyph", () => {
    useAppStore.setState({ appMode: "chat", contextCardCollapsed: true });
    render(<ChatContextCard />);
    expect(screen.queryByRole("button", { name: "展开上下文卡片" })).toBeNull();
    act(() => useAppStore.getState().setContextCardCollapsed(false));
    expect(useAppStore.getState().contextCardCollapsed).toBe(false);
    expect(screen.getByRole("dialog", { name: "工作区上下文摘要" }).getAttribute("data-collapsed")).toBe("false");
    expect(screen.getByText("来源").closest(".mc-chat-context-card-body")?.hidden).toBe(false);
  });

  it("renders provider document locations as informative non-web context", () => {
    useAppStore.setState({
      messages: [{
        id: "assistant-document",
        role: "assistant",
        content: "Document-cited answer",
        timestamp: 1,
        artifacts: [],
        attachmentRefs: [],
        citations: [{
          source: "anthropic:document:abc123",
          title: "Architecture notes",
          label: "Pages 2–3",
          locationType: "page_location",
          providerNative: true,
          range: [2, 3],
        }],
      }],
      subagents: [],
      backgroundTasks: [],
      terminalSessions: [],
    });

    render(<ChatContextCard />);

    expect(screen.getByRole("note")).toBeTruthy();
    expect(screen.getByText("Architecture notes")).toBeTruthy();
    expect(screen.getByText("Pages 2–3")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /打开来源/ })).toBeNull();
    expect(document.body.textContent).not.toContain("anthropic:document:abc123");
  });

  it("renders generated images as thumbnails and locates the image in chat", () => {
    const scrollIntoView = vi.fn();
    const target = document.createElement("div");
    target.dataset.artifactId = "generated-image-1";
    target.scrollIntoView = scrollIntoView;
    document.body.appendChild(target);
    useAppStore.setState({
      messages: [{
        id: "assistant-image",
        role: "assistant",
        content: "图像已经生成好了。",
        timestamp: 1,
        artifacts: [{
          artifactId: "generated-image-1",
          kind: "image",
          summary: "生成的猫咪图片",
          mediaType: "image/png",
          url: "data:image/png;base64,iVBORw0KGgo=",
        }],
      }],
      subagents: [],
      backgroundTasks: [],
    });

    const { container } = render(<ChatContextCard />);
    const imageButton = screen.getByRole("button", { name: "查看生成文件：生成的猫咪图片" });
    expect(container.querySelector('.mc-chat-context-source img')?.getAttribute("src"))
      .toBe("data:image/png;base64,iVBORw0KGgo=");

    fireEvent.click(imageButton);

    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });
    expect(target.classList.contains("assistant-cell-artifact-context-target")).toBe(true);
    target.remove();
  });

  it("restores a cold-history generated image thumbnail from its signed artifact resource", () => {
    useAppStore.setState({
      isConnected: true,
      messages: [{
        id: "assistant-history-image",
        role: "assistant",
        content: "历史图片",
        timestamp: 1,
        artifacts: [{
          artifactId: "generated-history-image",
          kind: "image",
          summary: "历史猫咪图片",
          mediaType: "image/png",
        }],
      }],
      subagents: [],
      backgroundTasks: [],
    });

    const { container } = render(<ChatContextCard />);
    const src = container.querySelector('.mc-chat-context-source img')?.getAttribute("src") || "";

    expect(src).toContain("/api/artifacts/raw");
    expect(src).toContain("artifact_id=generated-history-image");
    expect(src).toContain("session_id=session-context-image");
    expect(src).toContain("conversation_id=conv-active");
  });

  it("restores an uploaded image thumbnail from its signed attachment resource", () => {
    useAppStore.setState({
      isConnected: true,
      messages: [{
        id: "user-history-upload",
        role: "user",
        content: "参考这张图片",
        timestamp: 1,
        attachmentRefs: [{
          id: "uploaded-history-image",
          artifactId: "uploaded-history-image",
          name: "参考图.png",
          kind: "image",
          mediaType: "image/png",
          sizeBytes: 128,
          inputSource: "upload",
        }],
      }],
      subagents: [],
      backgroundTasks: [],
    });

    const { container } = render(<ChatContextCard />);
    const src = container.querySelector('.mc-chat-context-source img')?.getAttribute("src") || "";

    expect(src).toContain("/api/attachments/raw");
    expect(src).toContain("artifact_id=uploaded-history-image");
    expect(src).toContain("session_id=session-context-image");
    expect(src).toContain("conversation_id=conv-active");
  });

  it("projects legacy tool-owned browser screenshots into the context card", () => {
    useAppStore.setState({
      isConnected: true,
      messages: [{
        id: "assistant-legacy-browser-shot",
        role: "assistant",
        content: "已截取页面。",
        timestamp: 1,
        artifacts: [],
        toolCalls: [{
          id: "browser-shot-call",
          name: "browser_control",
          args: { action: "screenshot" },
          status: "success",
          resultKind: "browser",
          activityKind: "browser",
          artifactId: "legacy-browser-shot",
          artifactMediaType: "image/png",
          summary: "Browser screenshot",
          startedAt: 1,
          finishedAt: 2,
        }],
      } as never],
      subagents: [],
      backgroundTasks: [],
    });

    const { container } = render(<ChatContextCard />);
    expect(screen.getByRole("button", { name: "查看生成文件：Browser screenshot" })).toBeTruthy();
    const src = container.querySelector('.mc-chat-context-source img')?.getAttribute("src") || "";
    expect(src).toContain("/api/artifacts/raw");
    expect(src).toContain("artifact_id=legacy-browser-shot");
    expect(src).toContain("conversation_id=conv-active");
  });

  it("merges sparse message artifact metadata with the richer tool record", () => {
    useAppStore.setState({
      isConnected: true,
      messages: [{
        id: "assistant-sparse-browser-shot",
        role: "assistant",
        content: "截图完成。",
        timestamp: 1,
        artifacts: [{
          artifactId: "sparse-browser-shot",
          kind: "image",
          summary: "生成图片",
        }],
        blocks: [{
          type: "tool_call",
          record: {
            id: "sparse-browser-call",
            name: "browser_control",
            args: { action: "screenshot" },
            status: "success",
            resultKind: "browser",
            activityKind: "browser",
            artifactId: "sparse-browser-shot",
            artifactMediaType: "image/webp",
            summary: "页面截图",
            startedAt: 1,
            finishedAt: 2,
          },
        }],
      }],
      subagents: [],
      backgroundTasks: [],
    });

    const { container } = render(<ChatContextCard />);
    expect(screen.getByRole("button", { name: "查看生成文件：页面截图" })).toBeTruthy();
    const src = container.querySelector('.mc-chat-context-source img')?.getAttribute("src") || "";
    expect(src).toContain("artifact_id=sparse-browser-shot");
  });

  it("keeps uploaded attachments and generated artifacts in separate identity domains", () => {
    const attachments = collectAttachments([{
      id: "assistant-shared-id",
      role: "assistant",
      content: "两个资源共享后端 ID。",
      timestamp: 1,
      attachmentRefs: [{
        id: "upload-reference",
        artifactId: "shared-resource-id",
        name: "输入参考图.png",
        kind: "image",
        mediaType: "image/png",
      }],
      artifacts: [{
        artifactId: "shared-resource-id",
        kind: "image",
        summary: "生成结果图",
        mediaType: "image/png",
      }],
    }], "conv-owner");

    expect(attachments).toHaveLength(2);
    expect(attachments.map((attachment) => attachment.id)).toEqual(expect.arrayContaining([
      "attachment:shared-resource-id",
      "artifact:shared-resource-id",
    ]));
    expect(attachments.find((attachment) => attachment.generated)?.label).toBe("生成结果图");
    expect(attachments.find((attachment) => !attachment.generated)?.label).toBe("输入参考图.png");
    expect(attachments.find((attachment) => attachment.source === "attachment")?.id)
      .toBe("attachment:shared-resource-id");
    expect(attachments.find((attachment) => attachment.source === "artifact")?.id)
      .toBe("artifact:shared-resource-id");
  });

  it("keeps persisted orchestration outputs in the original records without adding normal context sources", () => {
    const firstAt = 1790857935157;
    const secondAt = 1790857961877;
    const messages = hydrateMessages([{
      id: "assistant-audit",
      role: "assistant",
      content: "检查已完成。",
      timestamp: firstAt,
      artifacts: [{ artifactId: "output-first", kind: "file", summary: "Script completed" }],
      tool_calls: [
        { id: "script-first", name: "tool_exec", args: {}, status: "success", artifact_id: "output-first", display_summary: "Script completed", result_kind: "generic", started_at: firstAt },
        { id: "script-second", name: "tool_exec", args: {}, status: "success", artifact_id: "output-second", display_summary: "Script completed", result_kind: "generic", started_at: secondAt },
      ],
    }]);
    useAppStore.setState({ messages, subagents: [], backgroundTasks: [] });
    const original = JSON.stringify(messages);
    const attachments = collectAttachments(messages, "conv-active");
    expect(attachments).toEqual([]);

    render(<ChatContextCard />);
    expect(screen.queryByRole("region", { name: "来源摘要" })).toBeNull();
    expect(screen.queryByText("生成文件")).toBeNull();
    expect(screen.queryByText("Script completed")).toBeNull();
    expect(screen.queryByRole("button", { name: /^查看执行结果：代码执行输出/ })).toBeNull();
    expect(JSON.stringify(messages)).toBe(original);
  });

  it("keeps the actual output path without adding the call's transport artifact as a context source", () => {
    const messages = hydrateMessages([{
      id: "assistant-deliverables",
      role: "assistant",
      content: "报告已生成。",
      blocks: [{ type: "tool_call", record: {
        id: "report-call", name: "tool_exec", args: {}, status: "success",
        artifactId: "report-log", artifactKind: "text", displaySummary: "Script completed", startedAt: 1,
        outputFiles: [{ path: "reports/audit.md", name: "Script completed", size: 120 }],
      } }],
      reply_attachments: [{ path: "reports/audit.md" }],
    }]);
    useAppStore.setState({ messages, subagents: [], backgroundTasks: [] });
    const attachments = collectAttachments(messages, "conv-active");
    expect(attachments).toHaveLength(1);
    expect(attachments.find((item) => item.source === "workspace")).toMatchObject({
      label: "audit.md", path: "reports/audit.md", conversationId: "conv-active",
    });
    render(<ChatContextCard />);
    expect(screen.getByRole("button", { name: "查看生成文件：audit.md" }).getAttribute("title")).toBe("reports/audit.md");
    fireEvent.click(screen.getByRole("button", { name: "来源", exact: true }));
    expect(screen.getByText("reports/audit.md")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^查看执行结果：代码执行输出/ })).toBeNull();
  });

  it.each(["absolute", "cwd-relative"] as const)("opens a projectless Recents %s file in the declared browser namespace", async pathKind => {
    const cwd = "C:/tasks/2026-10-25/markdown-alignment-check-md", base = "C:/tasks";
    const path = pathKind === "absolute" ? `${cwd}/outputs/alignment-check.md` : "outputs/alignment-check.md";
    const messages = hydrateMessages([{ id: "generated-projectless", role: "assistant", content: "已生成文件。", timestamp: 1,
      reply_attachments: [{ path }], blocks: [{ type: "tool_call", record: { id: "report", name: "tool_exec", args: {}, status: "success",
        outputFiles: [{ path, name: "alignment-check.md", size: 43 }], startedAt: 1 } }] }]);
    useAppStore.setState({ conversationId: "projectless", workingDirectory: cwd, conversations: [{ id: "projectless", title: "Task", updatedAt: "",
      workspaceRoot: "", executionCwd: cwd, projectlessBrowserRoot: base }], workspaceFileTabs: [], pendingConversationSwitchId: null,
      messages, subagents: [], backgroundTasks: [], terminalSessions: [], draft: "keep this draft" });
    const dirty = useAppStore.getState().editorTabs;
    const fetch = vi.spyOn(protocolApi, "fetchWithTimeout").mockResolvedValue(Response.json({ content: "# 对齐验收\n右侧栏最近产物验收" }));
    render(<ChatContextCard />);
    fireEvent.click(screen.getByRole("button", { name: "查看生成文件：alignment-check.md" }));
    const tab = useAppStore.getState().workspaceFileTabs[0];
    expect(tab.owner).toEqual({ scopeId: "conversation:projectless", conversationId: "projectless", executionCwd: cwd, browserRoot: base });
    expect(tab.path).toBe("2026-10-25/markdown-alignment-check-md/outputs/alignment-check.md");
    await readFileWorkspacePreview(tab.owner, tab.path);
    const url = new URL(String(fetch.mock.calls.at(-1)![0]));
    expect(url.pathname).toBe("/api/workspace/browser-preview");
    expect(url.searchParams.get("execution_cwd")).toBe(cwd);
    expect(url.searchParams.get("path")).toBe(tab.path);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "projectless", workingDirectory: cwd,
      rightStackTab: `file:${tab.id}`, draft: "keep this draft" });
    expect(useAppStore.getState().editorTabs).toBe(dirty);
  });

  it("preserves explicit image metadata when its execution record is sparse", () => {
    const attachments = collectAttachments([{
      id: "assistant-exec-image", role: "assistant", content: "图片已生成。", timestamp: 1,
      artifacts: [{ artifactId: "exec-image", kind: "image", summary: "生成图片", mediaType: "image/png" }],
      blocks: [{ type: "tool_call", record: {
        id: "exec-image-call", name: "tool_exec", args: {}, status: "success",
        artifactId: "exec-image", displaySummary: "Script completed", startedAt: 1,
      } }],
    }], "conv-active");
    expect(attachments).toHaveLength(1);
    expect(attachments[0]).toMatchObject({ kind: "image", label: "生成图片", executionResult: false, artifactId: "exec-image" });
  });

  it("opens a selected agent in the right sidebar", () => {
    render(<ChatContextCard />);

    fireEvent.click(screen.getByRole("button", { name: "打开子智能体：Audit layout" }));

    expect(useAppStore.getState()).toMatchObject({
      focusedSubagentId: "subagent-layout",
      rightStackTab: "subagents",
      rightPanelOpen: true,
    });
  });

  it("opens uncategorized attachments in artifacts and web sources in browser", () => {
    render(<ChatContextCard />);

    fireEvent.click(screen.getByRole("button", { name: "查看附件：layout-reference.png" }));
    expect(useAppStore.getState()).toMatchObject({ rightStackTab: "artifacts", rightPanelOpen: true });

    act(() => {
      useAppStore.setState({ rightPanelOpen: false });
      useAppStore.getState().toggleContextCard(700);
    });
    fireEvent.click(screen.getByRole("button", { name: "打开来源：Layout guide" }));
    expect(useAppStore.getState()).toMatchObject({
      rightStackTab: "browser",
      rightPanelOpen: true,
      livePreviewUrl: null,
    });

  });

  it("does not render an empty context card", () => {
    useAppStore.setState({ messages: [], subagents: [], backgroundTasks: [], terminalSessions: [], workingDirectory: "" });
    const { container } = render(<ChatContextCard />);

    expect(container.firstChild).toBeNull();
  });

  it("does not classify user-opened terminals as agent background tasks", () => {
    useAppStore.setState({
      workingDirectory: "",
      messages: [],
      subagents: [],
      backgroundTasks: [],
      terminalSessions: [{ id: "term-user", conversationId: "conv-active", shell: "pwsh", cwd: "C:\\Desktop\\MiniCode", status: "running" }],
    });
    const { container } = render(<ChatContextCard />);

    expect(container.firstChild).toBeNull();
  });

  it("scopes agent background tasks to the active conversation", () => {
    useAppStore.setState({
      workingDirectory: "",
      messages: [],
      subagents: [],
      conversationId: "conv-active",
      backgroundTasks: [{
        id: "bg-other",
        command: "npm run dev",
        status: "running",
        timestamp: 1,
        conversationId: "conv-other",
      }],
      terminalSessions: [],
    });
    const { container } = render(<ChatContextCard />);

    expect(container.firstChild).toBeNull();
  });

  it("prioritizes a stalled background command over ordinary running counts", () => {
    useAppStore.setState({
      messages: [],
      subagents: [],
      backgroundTasks: [
        { id: "bg-running", command: "npm test", status: "running", timestamp: 1, conversationId: "conv-active" },
        { id: "bg-stalled", command: "npm create vite", status: "stalled", timestamp: 2, conversationId: "conv-active" },
      ],
      terminalSessions: [],
    });

    render(<ChatContextCard />);

    expect(screen.getByText("1 个等待输入")).toBeTruthy();
  });

  it("opens the summary and its real project actions beside another right pane after the user reopens it", () => {
    useAppStore.setState({ rightPanelOpen: true, rightStackTab: "subagents" });
    render(<ChatContextCard />);
    expect(screen.queryByRole("dialog", { name: "工作区上下文摘要" })).toBeNull();
    act(() => useAppStore.getState().toggleContextCard(700));
    expect(screen.getByRole("dialog", { name: "工作区上下文摘要" })).toBeTruthy();
    expect(screen.getByText("layout-reference.png")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "摘要操作" }));
    expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["设置本地环境", "打开终端", "移动到工作树"]);
    const terminal = screen.getByRole("menuitem", { name: "打开终端" });
    fireEvent.pointerDown(terminal);
    expect(screen.getByRole("dialog", { name: "工作区上下文摘要" })).toBeTruthy();
    fireEvent.click(terminal);
    expect(useAppStore.getState()).toMatchObject({ activeBottomTab: "terminal", dockCollapsed: false });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(useAppStore.getState()).toMatchObject({ contextCardCollapsed: true, rightPanelOpen: true, rightStackTab: "subagents" });
  });

  it("retains the pinned choice while resizing between inline and popover with another pane open", () => {
    act(() => useAppStore.getState().setContextCardPinned(true));
    useAppStore.setState({ rightPanelOpen: true, rightStackTab: "files" });
    const view = render(<ChatContextCard displayMode="shift" />);
    expect(screen.queryByRole("complementary", { name: "工作区上下文摘要" })).toBeNull();
    act(() => useAppStore.getState().toggleContextCard(1200));
    expect(screen.getByRole("complementary", { name: "工作区上下文摘要" }).getAttribute("data-surface")).toBe("inline");
    view.rerender(<ChatContextCard displayMode="overlay" />);
    expect(screen.getByRole("dialog", { name: "工作区上下文摘要" })).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(useAppStore.getState()).toMatchObject({ contextCardPinned: true, rightPanelOpen: true, rightStackTab: "files" });
    view.rerender(<ChatContextCard displayMode="shift" />);
    expect(screen.queryByRole("complementary", { name: "工作区上下文摘要" })).toBeNull();
    act(() => useAppStore.getState().toggleContextCard(1200));
    expect(screen.getByRole("complementary", { name: "工作区上下文摘要" })).toBeTruthy();
    expect(localStorage.getItem("minicode.summary-pinned")).toBe("1");
  });

  it("does not reopen a closed summary when a details pane opens or closes", () => {
    useAppStore.setState({ contextCardCollapsed: true });
    const { container } = render(<ChatContextCard />);
    act(() => useAppStore.setState({ rightPanelOpen: true }));
    expect(container.firstChild).toBeNull();
    act(() => useAppStore.setState({ rightPanelOpen: false }));
    expect(container.firstChild).toBeNull();
  });

  it("keeps summary content and its chosen visibility across Code and Cowork modes", () => {
    const { container } = render(<ChatContextCard />);
    act(() => useAppStore.setState({ appMode: "code" }));
    expect(screen.getByText("layout-reference.png")).toBeTruthy();
    act(() => useAppStore.getState().setContextCardCollapsed(true));
    act(() => useAppStore.setState({ appMode: "cowork" }));
    expect(container.firstChild).toBeNull();
    act(() => useAppStore.getState().setContextCardCollapsed(false));
    expect(screen.getByText("layout-reference.png")).toBeTruthy();
    expect(screen.getByText("Layout guide")).toBeTruthy();
  });

});
