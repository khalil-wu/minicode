/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { FooterRow } from "./FooterRow";
import { uploadComposerFiles } from "./uploads";
import { showChoice, showConfirm } from "../overlays/DialogService";
import { handleSessionEvent } from "../chat/sessionEvents";
import { handleRuntimeEvent } from "../chat/runtimeEvents";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";

const reasoningPreference = vi.hoisted(() => ({ enabled: ["low", "medium", "high", "xhigh", "max", "ultra"] as string[] | null }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({
  preferences: reasoningPreference.enabled === null ? null : { enabledReasoningEfforts: reasoningPreference.enabled, showFullAccess: true, showContextWindowUsage: false }, error: "", update: vi.fn(),
}) }));

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
});

vi.mock("../hooks/useWebSocket", () => ({
  getWebSocket: () => ({ send: vi.fn() }),
}));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true),
  commandResultSucceeded: (result: { level?: string }) => !["error", "failed"].includes(result.level ?? ""),
  sendClientCommandAwaitResult: vi.fn(async (_command, expectedCommand) => ({
    type: "command.result",
    command: expectedCommand,
    level: "success",
    message: "",
    data: {},
  })),
}));

vi.mock("./uploads", () => ({
  uploadComposerFiles: vi.fn(),
}));

vi.mock("../overlays/DialogService", () => ({
  showConfirm: vi.fn(),
  showChoice: vi.fn(),
}));

vi.mock("../overlays/ToastContainer", () => ({
  pushToast: vi.fn(),
}));

describe("FooterRow permission picker", () => {
  it.each(["conversation.switched", "session.restored", "session.synced", "conversation.list", "task.update", "runtime.capabilities"])(
    "restores a cached task's model, provider and effort through %s without a model-update event", (source) => {
      const buffers = { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as Parameters<typeof handleSessionEvent>[1];
      const task = (id: string) => ({ id, title: id, updated_at: "2026-10-08T00:00:00Z", workspace_root: `C:/${id}`, transcript: [] });
      const low = task("task-low"), high = task("task-high");
      const selection = (effort: "low" | "high") => ({
        provider: effort === "low" ? "custom" : "openai", model: effort === "low" ? "gpt-6.1-sol" : "gpt-6-luna",
        provider_id: `${effort}-provider`, base_url: `https://${effort}.invalid/v1`, wire_api: "responses",
        configured_reasoning_effort: effort, effective_reasoning_effort: effort,
        reasoning_effort_supported: true, reasoning_effort_levels: ["low", "high"],
      });
      const snapshot = (effort: "low" | "high") => ({
        active_conversation_id: `task-${effort}`, workspace_root: `C:/task-${effort}`,
        selected_model: selection(effort).model, capabilities: { provider_capabilities: selection(effort) },
      });
      const receive = (raw: unknown) => {
        const frame = normalizeInboundServerEvent(raw);
        expect(frame).toBe(raw);
        expect(frame).not.toBeNull();
        act(() => { if (!handleSessionEvent(frame!, buffers)) expect(handleRuntimeEvent(frame!)).toBe(true); });
      };
      useAppStore.setState({
        pendingConversationSwitchId: null, pendingConversationCreateId: null, conversationInventoryInstanceId: null,
        conversationInventoryRevision: 0, conversations: [], conversationMessages: {}, conversationStreaming: {},
        availableModels: ["gpt-6.1-sol", "gpt-6-luna"], modelsSource: "live", messages: [], isStreaming: false,
      });
      render(<FooterRow sendState="idle" onSend={() => {}} />);
      receive({ type: "conversation.switched", conversation_id: low.id, conversation: low, session: snapshot("low") });
      expect(screen.getByRole("button", { name: "模型与推理强度：6.1 Sol，低" })).toBeTruthy();
      receive({ type: "conversation.switched", conversation_id: high.id, conversation: high, session: snapshot("high") });
      expect(screen.getByRole("button", { name: "模型与推理强度：6 Luna，高" })).toBeTruthy();
      if (source !== "conversation.switched") receive({ type: "conversation.switched", conversation_id: low.id, conversation: low });
      if (source === "conversation.switched") receive({ type: source, conversation_id: low.id, conversation: low, session: snapshot("low") });
      else if (source === "runtime.capabilities") receive({ type: source, conversation_id: low.id, workspace_root: low.workspace_root, capabilities: snapshot("low").capabilities });
      else if (source === "task.update") receive({ type: source, conversation_id: low.id, partial: true,
        session: { active_conversation_id: low.id, workspace_root: low.workspace_root, selected_model: selection("low").model, provider_capabilities: selection("low") } });
      else if (source === "conversation.list") receive({ type: source, inventory_instance_id: "footer-epoch", inventory_revision: 1,
        conversations: [low, high], active_conversation_id: low.id, active_conversation: low, session: snapshot("low") });
      else receive({ type: source, active_conversation_id: low.id, active_conversation: low, conversation: low, session: snapshot("low") });
      expect(useAppStore.getState()).toMatchObject({ currentModel: "gpt-6.1-sol", currentProvider: "custom", currentProviderId: "low-provider",
        currentProviderBaseUrl: "https://low.invalid/v1", currentWireApi: "responses", effortLevel: "low" });
      expect(screen.getByRole("button", { name: "模型与推理强度：6.1 Sol，低" })).toBeTruthy();
      receive({ type: "task.update", conversation_id: high.id, session: snapshot("high") });
      receive({ type: "runtime.capabilities", conversation_id: low.id, workspace_root: "C:/other", capabilities: snapshot("high").capabilities });
      expect(screen.getByRole("button", { name: "模型与推理强度：6.1 Sol，低" })).toBeTruthy();
    },
  );
  it("shows a GitHub connection failure while retaining the last PR and retries in its owner scope", () => {
    useAppStore.setState({ workingDirectory: "C:/project", prStatusIssue: { message: "gh auth login", code: "auth_required" },
      prMonitor: { prNumber: 7, prUrl: "https://example.invalid/pr/7", ciStatus: "passed", autoFix: false, autoMerge: false, lastCheckedAt: 1 } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    expect(screen.getByText("PR #7")).toBeTruthy();
    expect(screen.getByText(/GitHub 尚未连接/)).toBeTruthy();
    expect(screen.queryByText(/gh auth login/)).toBeNull();
    expect((screen.getByRole("button", { name: "自动合并" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "连接 GitHub" }));
    expect(useAppStore.getState().settingsTab).toBe("workspaceGit");
    expect(useAppStore.getState().settingsOpen).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "git.pr_status", conversation_id: "conv-footer", workspace_root: "C:/project" });
  });
  it("routes a missing GitHub runtime to connection settings without shell installation instructions", () => {
    useAppStore.setState({ prStatusIssue: { message: "未找到 GitHub CLI，安装 gh 后重试。", code: "gh_unavailable" } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    expect(screen.getByText("GitHub 连接组件未就绪。")).toBeTruthy();
    expect(screen.queryByText(/安装 gh/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "查看连接设置" }));
    expect(useAppStore.getState().settingsTab).toBe("workspaceGit");
  });
  it("scopes remote auto-merge to the PR and branch shown when the user enabled it", async () => {
    useAppStore.setState({ workingDirectory: "C:/project", workspaceGit: { branch: "feature-a", isWorktree: false },
      prMonitor: { prNumber: 7, prUrl: "https://github.com/org/repo/pull/7", ciStatus: "passed", autoFix: false, autoMerge: false, lastCheckedAt: 1 } });
    vi.mocked(showConfirm).mockImplementationOnce(async () => {
      useAppStore.setState({ workspaceGit: { branch: "feature-b", isWorktree: false } });
      return true;
    });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "自动合并" }));
    await waitFor(() => expect(sendClientCommand).toHaveBeenCalledWith({ type: "git.pr_automation.set", conversation_id: "conv-footer", workspace_root: "C:/project", auto_merge: true, expected_pr_number: 7, expected_branch: "feature-a" }));
  });
  it("keeps local auto-fix settings independent from remote auto-merge identity fields", async () => {
    useAppStore.setState({ workingDirectory: "C:/project", prMonitor: { prNumber: 7, prUrl: "https://github.com/org/repo/pull/7", ciStatus: "passed", autoFix: false, autoMerge: false, lastCheckedAt: 1 } });
    vi.mocked(showConfirm).mockResolvedValueOnce(true);
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "自动修复" }));
    await waitFor(() => expect(sendClientCommand).toHaveBeenCalledWith({ type: "git.pr_automation.set", conversation_id: "conv-footer", workspace_root: "C:/project", auto_fix: true }));
  });
  beforeEach(() => {
    reasoningPreference.enabled = ["low", "medium", "high", "xhigh", "max", "ultra"];
    useAppStore.setState({
      permissionMode: "auto",
      agentMode: "build",
      currentModel: "gpt-5",
      defaultModelSelection: null,
      modelSelectionMode: null,
      availableModelOptions: [],
      currentProvider: "openai",
      currentProviderId: "openai_official",
      currentProviderBaseUrl: "https://api.openai.com/v1",
      currentWireApi: "responses",
      conversationId: "conv-footer",
      isConnected: true,
      pendingConversationSwitchId: null,
      pendingConversationCreateId: null,
      conversationHydration: {},
      appMode: "cowork",
      availableModels: ["gpt-5"],
      effortLevel: "high",
      prMonitor: null,
      prStatusIssue: null,
      settingsOpen: false,
      settingsTab: "general",
      workspaceGit: null,
      contextUsage: null,
      budgetBuckets: [],
      totalBudgetPercent: 0,
      lastUsage: null,
      isStreaming: false,
      runtimeSession: null,
      runtimeCapabilities: { permission: { available_modes: ["confirm", "auto", "plan", "bypass"] } },
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("opens the add menu before choosing files and uploads selected files", () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ plugins: [] }), {status:200})));
    render(<FooterRow sendState="idle" onSend={() => {}} />);

    const attach = screen.getByRole("button", { name: "添加文件等内容" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const clickSpy = vi.spyOn(input, "click").mockImplementation(() => {});

    fireEvent.click(attach);
    expect(clickSpy).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("menuitem", { name: "文件和文件夹" }));
    expect(clickSpy).toHaveBeenCalledTimes(1);

    const files = [
      new File(["hello"], "note.txt", { type: "text/plain" }),
      new File(["{}"], "data.json", { type: "application/json" }),
    ];
    fireEvent.change(input, { target: { files } });

    expect(uploadComposerFiles).toHaveBeenCalledWith(files);
  });

  it.each(["chat", "workspace", "switch", "create", "away-and-back"])("retires the file picker when its %s scope changes before selection", (change) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ plugins: [] }), { status: 200 })));
    useAppStore.setState({ workingDirectory: "C:/picker-original", draft: "Keep this draft", attachments: [] });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
    vi.spyOn(input, "click").mockImplementation(() => {});
    fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "文件和文件夹" }));
    act(() => {
      if (change === "workspace") useAppStore.setState({ workingDirectory: "C:/picker-new" });
      else if (change === "switch") useAppStore.setState({ pendingConversationSwitchId: "new-chat" });
      else if (change === "create") useAppStore.setState({ pendingConversationCreateId: "new-request" });
      else {
        useAppStore.setState({ conversationId: "new-chat" });
        if (change === "away-and-back") useAppStore.setState({ conversationId: "conv-footer" });
      }
    });
    fireEvent.change(input, { target: { files: [new File(["original"], "old-selection.txt")] } });
    expect(uploadComposerFiles).not.toHaveBeenCalled();
    expect(useAppStore.getState()).toMatchObject({ draft: "Keep this draft", attachments: [] });
    if (change === "away-and-back") {
      fireEvent.click(screen.getByRole("button", { name: "添加文件等内容" }));
      fireEvent.click(screen.getByRole("menuitem", { name: "文件和文件夹" }));
      const next = [new File(["new"], "new-selection.txt")];
      fireEvent.change(input, { target: { files: next } });
      expect(uploadComposerFiles).toHaveBeenCalledExactlyOnceWith(next);
    }
  });

  it("exposes one responsive layout region per existing control group", () => {
    const { container } = render(<FooterRow sendState="idle" onSend={() => {}} />);

    expect(container.querySelectorAll(".composer-footer-primary")).toHaveLength(1);
    expect(container.querySelectorAll(".composer-model-picker")).toHaveLength(1);
    expect(container.querySelectorAll(".composer-send-btn")).toHaveLength(1);
    expect(container.querySelector(".composer-footer")?.getAttribute("data-compact")).toBe("false");
  });

  it("offers model configuration when no model has been configured", () => {
    useAppStore.setState({ currentModel: "", availableModels: [], settingsOpen: false });
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    expect(screen.getByText("尚未配置模型")).toBeTruthy();
    fireEvent.click(screen.getByRole("option", { name: "配置模型…" }));
    expect(useAppStore.getState().settingsOpen).toBe(true);
    expect(useAppStore.getState().settingsTab).toBe("provider");
  });

  it("keeps stop and queue as separate accessible controls", () => {
    const onStop = vi.fn();
    const onSend = vi.fn();
    render(<FooterRow sendState="queue" onSend={onSend} onStop={onStop} compact />);

    fireEvent.click(screen.getByRole("button", { name: "停止当前回复" }));
    fireEvent.click(screen.getByRole("button", { name: "将消息加入队列" }));
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("shows the context budget control in compact code mode", () => {
    useAppStore.setState({ contextUsage: { used: 1_000, limit: 10_000 } });

    render(<FooterRow sendState="idle" onSend={() => {}} compact />);

    expect(screen.getByLabelText("查看会话用量详情")).toBeTruthy();
  });

  it("shows a known scalar budget feed without context or buckets and inspects its current owner", () => {
    useAppStore.setState({ totalBudgetPercent: 0 });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    expect(screen.queryByRole("meter")).toBeNull();
    act(() => useAppStore.getState().setBudget([], 0.75));
    expect(screen.getByRole("meter", { name: "会话用量 75%" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "查看会话用量详情" }));
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "session.usage.inspect", conversation_id: "conv-footer", source: "usage_ring" });
  });

  it("refreshes context usage automatically when the app mode changes", async () => {
    render(<FooterRow sendState="idle" onSend={() => {}} />);

    await waitFor(() => expect(sendClientCommand).toHaveBeenCalledWith({
      type: "session.usage.inspect",
      conversation_id: "conv-footer",
      source: "usage_ring_auto",
      silent: true,
    }));

    vi.mocked(sendClientCommand).mockClear();
    useAppStore.getState().setAppMode("code");

    await waitFor(() => expect(sendClientCommand).toHaveBeenCalledWith({
      type: "session.usage.inspect",
      conversation_id: "conv-footer",
      source: "usage_ring_auto",
      silent: true,
    }));
  });

  it("inspects a restored owner once its full context is hydrated, without borrowing a previous limit or refreshing again on sync", () => {
    const buffers = { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as Parameters<typeof handleSessionEvent>[1];
    const conversation = { id: "usage-restored", title: "Restored usage", revision: 1, transcript: [{ id: "restored-user", role: "user", content: "Restore" }],
      context_snapshot: { context_ledger: { schema_version: 1, estimated_tokens: 21133, actual_tokens: 19791, compaction_count: 0,
        entries: [{ category: "history", label: "History", estimated_tokens: 21133, item_count: 2, source_count: 1 }] } } };
    const usageRequests = () => vi.mocked(sendClientCommand).mock.calls.filter(([command]) => command.type === "session.usage.inspect");
    useAppStore.setState({ conversationId: "previous-owner", isConnected: false, conversationMessages: {}, conversationStreaming: {},
      conversations: [{ id: "previous-owner", title: "Previous", updatedAt: "2026-10-09" }],
      contextUsage: { used: 3000, limit: 40000 }, messages: [], conversationHistoryPages: {} });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    expect(usageRequests()).toHaveLength(0);
    act(() => {
      useAppStore.setState({ isConnected: true });
      handleSessionEvent({ type: "conversation.switched", conversation_id: conversation.id, conversation, is_hydrating: true, context_pending: true } as never, buffers);
    });
    expect(usageRequests()).toHaveLength(0);
    expect(useAppStore.getState().contextUsage).toMatchObject({ used: 19791, limit: 0 });
    expect(screen.getByRole("meter", { name: "用量暂无数据" })).toBeTruthy();
    act(() => { handleSessionEvent({ type: "conversation.switched", conversation_id: conversation.id, conversation, is_hydrating: false } as never, buffers); });
    expect(usageRequests()).toHaveLength(1);
    expect(usageRequests()[0][0]).toEqual({ type: "session.usage.inspect", conversation_id: conversation.id, source: "usage_ring_auto", silent: true });
    act(() => { handleRuntimeEvent({ type: "context_usage", conversation_id: conversation.id, used: 19791, limit: 720000,
      ledger: conversation.context_snapshot.context_ledger } as never); });
    expect(screen.getByRole("meter", { name: "会话用量 3%" })).toBeTruthy();
    expect(useAppStore.getState().contextUsage).toMatchObject({ used: 19791, limit: 720000 });
    act(() => { handleSessionEvent({ type: "session.synced", active_conversation_id: conversation.id, active_conversation: conversation,
      session: { active_conversation_id: conversation.id }, synced: true } as never, buffers); });
    expect(usageRequests()).toHaveLength(1);
  });

  it("groups the actual non-full permissions and shows Full access after the default option", () => {
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent.click(screen.getByTitle("更改权限"));

    expect(document.querySelector(".mc-dropdown-menu.composer-picker-menu")).toBeTruthy();
    expect(screen.getAllByRole("option").map(option => option.getAttribute("aria-label"))).toEqual(["请求批准", "完全访问"]);
    expect(screen.getByText("完全访问")).toBeTruthy();
    expect(screen.queryByText("Ask before file and network actions")).toBeNull();
    expect(screen.queryByText("Auto read, search, and edit workspace files")).toBeNull();
    expect(screen.queryByText("Use files, network, edits, and commands without prompts")).toBeNull();
    expect(screen.queryByText("规划")).toBeNull();
    expect(screen.getByRole("option", { name: "请求批准" }).querySelector(".lucide-hand")).toBeTruthy();
    expect(screen.getByRole("option", { name: "完全访问" }).querySelector(".lucide-shield-alert")).toBeTruthy();
    expect(screen.getByText("编辑外部文件和使用互联网时始终询问")).toBeTruthy();
    expect(screen.queryByText("Accept")).toBeNull();
    expect(screen.queryByText("Auto-accept file edits, ask for commands")).toBeNull();
  });

  it("keeps an existing read-only permission in the default group without changing agent Plan", async () => {
    useAppStore.setState({ permissionMode: "plan", agentMode: "build" });
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent.click(screen.getByTitle("更改权限"));
    fireEvent.click(screen.getByRole("option", { name: "请求批准" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "conversation.permission_mode.set",
      mode: "plan",
      source: "frontend.ui",
      conversation_id: "conv-footer",
    }, "conversation.permission_mode.set"));
    expect(useAppStore.getState().agentMode).toBe("build");
  });

  it("asks for confirmation before switching to bypass", async () => {
    let accept!: (value: Awaited<ReturnType<typeof sendClientCommandAwaitResult>>) => void;
    vi.mocked(sendClientCommandAwaitResult).mockReturnValueOnce(new Promise((resolve) => { accept = resolve; }));
    const confirmSpy = vi.mocked(showConfirm);
    confirmSpy.mockClear();
    confirmSpy.mockResolvedValue(true);
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent.click(screen.getByTitle("更改权限"));
    fireEvent.click(screen.getByText("完全访问"));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "conversation.permission_mode.set",
      mode: "bypass",
      source: "frontend.ui",
      conversation_id: "conv-footer",
    }, "conversation.permission_mode.set"));
    expect(useAppStore.getState().permissionMode).toBe("auto");
    expect(confirmSpy).toHaveBeenCalledWith(expect.objectContaining({ danger: true }));
    await act(async () => accept({ type: "command.result", command: "conversation.permission_mode.set", level: "info", message: "",
      data: { conversation_id: "conv-footer", mode: "bypass" } }));
    expect(useAppStore.getState().permissionMode).toBe("bypass");
    expect(screen.getByTitle("更改权限")).toBeTruthy();
  });

  it("keeps the current mode when the bypass confirmation is cancelled", async () => {
    const confirmSpy = vi.mocked(showConfirm);
    confirmSpy.mockClear();
    confirmSpy.mockResolvedValue(false);
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent.click(screen.getByTitle("更改权限"));
    fireEvent.click(screen.getByText("完全访问"));

    await waitFor(() => {
      expect(confirmSpy).toHaveBeenCalled();
    });
    expect(useAppStore.getState().permissionMode).toBe("auto");
  });

  it("opens the permission menu from the global shortcut event", () => {
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent(document, new CustomEvent("open-permission-menu"));

    expect(screen.getByRole("option", { name: "请求批准" })).toBeTruthy();
    expect(screen.getByText("完全访问")).toBeTruthy();
  });

  it("does not render the legacy agent mode picker", () => {
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    expect(screen.queryByTitle("Implement and verify changes")).toBeNull();
    expect(screen.queryByText("Review")).toBeNull();
  });

  it("opens the model menu from the global shortcut event", () => {
    useAppStore.setState({
      currentModel: "deepseek-v4-flash",
      currentProvider: "custom",
      currentProviderId: "deepseek",
      currentProviderBaseUrl: "https://api.deepseek.com/v1",
      availableModels: ["deepseek-v4-flash", "gpt-5"],
      modelsSource: "live",
    });
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent(document, new CustomEvent("open-model-menu"));

    expect(screen.getByRole("option", { name: "5" })).toBeTruthy();
    expect(screen.queryByText("配置模型…")).toBeNull();
  });

  it("does not show stale fallback models for unknown custom gateways in the model menu", () => {
    useAppStore.setState({
      currentModel: "mimo-v2.5-pro",
      currentProvider: "custom",
      currentProviderId: "custom_openai",
      currentProviderBaseUrl: "https://api.bbe.to/v1",
      currentWireApi: "chat",
      availableModels: ["gpt-5.5", "gpt-5.4", "mimo-v2.5-pro"],
      modelsSource: "",
    });

    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent(document, new CustomEvent("open-model-menu"));

    expect(screen.getAllByText("mimo-v2.5-pro")).toHaveLength(2);
    expect(screen.queryByText("gpt-5.5")).toBeNull();
    expect(screen.queryByText("gpt-5.4")).toBeNull();
    expect(screen.queryByText("配置模型…")).toBeNull();
  });

  it("waits for the model result through the shared websocket outbox", async () => {
    useAppStore.setState({
      currentModel: "deepseek-v4-flash",
      currentProvider: "custom",
      currentProviderId: "deepseek",
      currentProviderBaseUrl: "https://api.deepseek.com/v1",
      availableModels: ["deepseek-v4-flash", "gpt-5"],
      modelsSource: "live",
    });
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent(document, new CustomEvent("open-model-menu"));
    fireEvent.click(screen.getByRole("option", { name: "5" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "llm.model.set",
      conversation_id: "conv-footer",
      model: "gpt-5",
      selection_mode: "model",
    }, "model.set"));
  });

  it("offers configuration when no models are available and retains an open settings dialog", async () => {
    useAppStore.setState({
      currentModel: "",
      availableModels: [],
      settingsOpen: true,
      settingsTab: "general",
    });

    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent(document, new CustomEvent("open-model-menu"));
    fireEvent.click(screen.getByText("配置模型…"));

    expect(useAppStore.getState().settingsOpen).toBe(true);
    await waitFor(() => expect(useAppStore.getState().settingsTab).toBe("provider"));
  });

  it("uses the shared compact menu surface for the model picker", () => {
    useAppStore.setState({ availableModels: ["gpt-5", "gpt-5-mini"] });
    render(<FooterRow sendState="idle" onSend={() => {}} />);

    fireEvent.click(screen.getByTitle("gpt-5"));

    expect(document.querySelector(".mc-dropdown-menu.composer-picker-menu")).toBeTruthy();
    expect(screen.getByRole("option", { name: "5 Mini" })).toBeTruthy();
  });

  it("keeps runtime sandbox metadata out of the permission control", () => {
    useAppStore.setState({
      runtimeSession: {
        permission_profile: "auto",
        workspace_scope: "computer",
        sandbox_status: { os: "app_layer", network: "approval_required" },
      },
    });

    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    expect(screen.getByTitle("更改权限")).toBeTruthy();
    expect(screen.queryByText("Auto · Guarded · Net asks")).toBeNull();
    expect(screen.queryByText("app_layer")).toBeNull();
    expect(screen.queryByText(/Files:|Network:|Sandbox/)).toBeNull();
  });

  it("keeps permission and effort picker labels neutral across modes", () => {
    useAppStore.setState({
      permissionMode: "plan",
      effortLevel: "max",
      runtimeCapabilities: {
        provider_capabilities: {
          reasoning_effort: true,
          reasoning_effort_levels: ["low", "medium", "high", "xhigh"],
        },
      },
    });

    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    expect((screen.getByTitle("更改权限") as HTMLElement).style.color).toBe("var(--text-secondary)");
    // `max` is not among the provider's declared levels. Keep the retained
    // value out of the trigger instead of presenting an unsupported control.
    const trigger = screen.getByTitle("gpt-5");
    expect(trigger.querySelector(".composer-model-trigger-effort")).toBeNull();
    expect(screen.queryByText("最大（不支持）")).toBeNull();
  });

  it("does not show fake thinking controls for unsupported chat models", () => {
    useAppStore.setState({
      currentProvider: "custom",
      currentModel: "deepseek-v4-flash",
      availableModels: ["deepseek-v4-flash"],
      runtimeCapabilities: {
        provider_capabilities: {
          reasoning_effort: false,
          reasoning_effort_levels: [],
        },
      },
    });

    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    expect(screen.queryByTitle(/推理强度：/)).toBeNull();
  });

  it.each([{ levels: [] }, { levels: ["off"] }, { levels: ["high"] }])("keeps a model with fixed levels $levels on the model picker", ({ levels }) => {
    useAppStore.setState({ currentProvider: "custom", currentModel: "fixed-model", availableModels: ["fixed-model"],
      effortLevel: levels[0] || "medium", availableModelOptions: [{ model: "fixed-model", reasoningEffortLevels: levels, defaultReasoningEffort: levels[0] || "off" }],
      runtimeCapabilities: { provider_capabilities: { model: "fixed-model", reasoning_effort_supported: true,
        reasoning_effort_levels: ["low", "medium", "high"] } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    const trigger = screen.getByTitle("fixed-model");
    expect(trigger.querySelector(".composer-model-trigger-effort")).toBeNull();
    fireEvent.click(trigger);
    expect(screen.getByRole("listbox", { name: "选择模型" })).toBeTruthy();
    expect(screen.queryByRole("slider")).toBeNull();
  });

  it("hides reasoning effort until the runtime confirms model support", () => {
    useAppStore.setState({
      currentProvider: "custom",
      currentModel: "gpt-5.5",
      currentProviderBaseUrl: "https://api.bbe.to/v1",
      currentWireApi: "chat",
      availableModels: ["gpt-5.5"],
      effortLevel: "medium",
      runtimeCapabilities: {
        provider_capabilities: {
          reasoning_effort: false,
          reasoning_effort_levels: [],
        },
      },
    });

    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    expect(screen.queryByTitle(/推理强度：/)).toBeNull();
  });

  it("shows only runtime-supported reasoning effort levels", () => {
    useAppStore.setState({
      effortLevel: "focused",
      runtimeCapabilities: {
        provider_capabilities: {
          reasoning_effort: true,
          reasoning_effort_levels: ["low", "focused", "ultra"],
        },
      },
    });

    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度：/ }));
    const slider = screen.getByRole("slider");
    expect(slider.getAttribute("max")).toBe("2");
    for (const [index, label] of ["低", "focused", "Ultra"].entries()) {
      fireEvent.change(slider, { target: { value: String(index) } });
      expect(slider.getAttribute("aria-valuetext")).toBe(label);
    }
    expect(screen.queryByRole("button", { name: "恢复中等推理强度" })).toBeNull();
  });

  it("selects the model-declared xhigh effort from the Composer", async () => {
    useAppStore.setState({
      effortLevel: "medium",
      runtimeCapabilities: {
        provider_capabilities: {
          reasoning_effort: true,
          reasoning_effort_levels: ["low", "medium", "high", "xhigh"],
        },
      },
    });

    render(<FooterRow sendState="idle" onSend={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度：/ }));
    fireEvent.change(screen.getByRole("slider"), { target: { value: "3" } });
    fireEvent.pointerUp(screen.getByRole("slider"));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "llm.config.set",
      conversation_id: "conv-footer",
      provider: "openai",
      reasoning_effort: "xhigh",
      source: "frontend.footer",
    }, "effort"));
  });

  it("keeps every declared level selectable and the slider mounted when leaving ultra", () => {
    useAppStore.setState({
      effortLevel: "ultra",
      runtimeCapabilities: {
        provider_capabilities: {
          reasoning_effort: true,
          reasoning_effort_levels: ["low", "medium", "high", "xhigh", "max", "ultra"],
        },
      },
    });

    render(<FooterRow sendState="idle" onSend={() => {}} />);

    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度：/ }));
    const slider = screen.getByRole("slider");
    expect(slider.getAttribute("aria-valuetext")).toBe("Ultra");
    expect(slider.getAttribute("max")).toBe("5");
    fireEvent.change(slider, { target: { value: "4" } });
    fireEvent.pointerUp(slider);
    expect(screen.getByRole("slider")).toBe(slider);
    expect(slider.getAttribute("aria-valuetext")).toBe("最大");
    fireEvent.change(slider, { target: { value: "5" } });
    fireEvent.pointerUp(slider);
    expect(slider.getAttribute("aria-valuetext")).toBe("Ultra");
  });

  it("keeps the Ultra value while the tooltip only offers the model choice", () => {
    useAppStore.setState({ currentModel: "gpt-6.1-sol", effortLevel: "ultra", runtimeCapabilities: { provider_capabilities: {
      model: "gpt-6.1-sol", reasoning_effort_supported: true,
      reasoning_effort_levels: ["low", "medium", "high", "xhigh", "max", "ultra"],
      reasoning_effort_wire_map: { ultra: "xhigh" }, wire_reasoning_effort: "xhigh",
    } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    const control = screen.getByRole("button", { name: "模型与推理强度：6.1 Sol，Ultra" });
    expect(control.title).toBe("选择模型");
    fireEvent.click(control);
    expect(screen.getByRole("slider").getAttribute("aria-valuetext")).toBe("Ultra");
    expect(screen.getByRole("slider").getAttribute("max")).toBe("5");
  });

  it("applies optional effort visibility while preserving the current model, effort and declared capability data", () => {
    const capability = { model: "gpt-6.1-sol", reasoning_effort_supported: true, reasoning_effort_levels: ["low", "focused", "high", "max", "ultra"] };
    reasoningPreference.enabled = ["low", "medium", "high", "xhigh"];
    useAppStore.setState({ currentModel: "gpt-6.1-sol", effortLevel: "max", draft: "Keep the current input", runtimeCapabilities: { provider_capabilities: capability } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "模型与推理强度：6.1 Sol，最大" }));
    expect(screen.queryByRole("slider")).toBeNull();
    expect(screen.getByRole("option", { name: "focused" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "最大" })).toBeNull();
    expect(screen.queryByRole("option", { name: "Ultra" })).toBeNull();
    expect(useAppStore.getState()).toMatchObject({ currentModel: "gpt-6.1-sol", effortLevel: "max", draft: "Keep the current input" });
    expect(useAppStore.getState().runtimeCapabilities?.provider_capabilities).toBe(capability);
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("updates the open model control when effort preferences change without sending a model configuration", () => {
    useAppStore.setState({ currentModel: "gpt-6.1-sol", effortLevel: "high", runtimeCapabilities: { provider_capabilities: {
      reasoning_effort_supported: true, reasoning_effort_levels: ["low", "medium", "high", "xhigh", "max", "ultra"],
    } } });
    const view = render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "模型与推理强度：6.1 Sol，高" }));
    expect(screen.getByRole("slider").getAttribute("max")).toBe("5");
    reasoningPreference.enabled = ["low", "medium", "high", "xhigh"];
    view.rerender(<FooterRow sendState="idle" onSend={() => {}} />);
    expect(screen.getByRole("slider").getAttribute("max")).toBe("3");
    expect(screen.getByRole("slider").getAttribute("aria-valuetext")).toBe("高");
    expect(useAppStore.getState().effortLevel).toBe("high");
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("keeps declared efforts available when no native visibility preference has been produced", () => {
    reasoningPreference.enabled = null;
    useAppStore.setState({ currentModel: "gpt-6.1-sol", effortLevel: "high", runtimeCapabilities: { provider_capabilities: {
      reasoning_effort_supported: true, reasoning_effort_levels: ["low", "medium", "high", "xhigh", "max", "ultra"],
    } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "模型与推理强度：6.1 Sol，高" }));
    expect(screen.getByRole("slider").getAttribute("max")).toBe("5");
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it.each(["low", "medium", "high", "xhigh", "max", "ultra"] as const)("sends the selected GPT-6.1 Sol reasoning level to its task: %s", async (level) => {
    const levels = ["low", "medium", "high", "xhigh", "max", "ultra"];
    useAppStore.setState({ currentProvider: "custom", currentModel: "gpt-6.1-sol", effortLevel: level === "low" ? "medium" : "low", runtimeCapabilities: {
      provider_capabilities: { model: "gpt-6.1-sol", reasoning_effort_supported: true, reasoning_effort_levels: levels },
    } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /^模型与推理强度：/ }));
    const slider = screen.getByRole("slider");
    expect(slider.getAttribute("max")).toBe("5");
    fireEvent.change(slider, { target: { value: String(levels.indexOf(level)) } });
    fireEvent.pointerUp(slider);
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "llm.config.set", provider: "custom", conversation_id: "conv-footer",
      reasoning_effort: level, source: "frontend.footer",
    }, "effort"));
  });

  // Regression: narrowing the ladder to low/medium/high plus one extreme level
  // hid a configured `minimal`, and the pill then substituted 中 with the
  // checkmark on medium — a value the user never chose, and `minimal` could not
  // be reselected.
  it("shows and keeps a declared minimal reasoning level selectable", () => {
    useAppStore.setState({
      effortLevel: "minimal",
      runtimeCapabilities: {
        provider_capabilities: {
          reasoning_effort: true,
          reasoning_effort_levels: ["minimal", "low", "medium", "high"],
        },
      },
    });

    render(<FooterRow sendState="idle" onSend={() => {}} />);

    const pill = screen.getByRole("button", { name: /模型与推理强度：/ });
    expect(screen.getByRole("button", { name: "模型与推理强度：5，最低" })).toBeTruthy();
    expect(screen.queryByText("中（不支持）")).toBeNull();

    fireEvent.click(pill);
    const slider = screen.getByRole("slider");
    expect(slider.getAttribute("aria-valuetext")).toBe("最低");
    fireEvent.change(slider, { target: { value: "2" } });
    fireEvent.pointerUp(slider);
    expect(screen.getByRole("slider")).toBe(slider);
    fireEvent.change(slider, { target: { value: "0" } });
    fireEvent.pointerUp(slider);
    expect(slider.getAttribute("aria-valuetext")).toBe("最低");
  });

  it("shows model reasoning effort in the minimal empty-conversation Composer", () => {
    useAppStore.setState({
      effortLevel: "medium",
      runtimeCapabilities: {
        provider_capabilities: {
          reasoning_effort: true,
          reasoning_effort_levels: ["low", "medium", "high", "xhigh"],
        },
      },
    });

    render(<FooterRow minimal sendState="idle" onSend={() => {}} />);

    expect(screen.getByRole("button", { name: /模型与推理强度：/ })).toBeTruthy();
  });

  it("commits a supported slider value once after dragging, not on every move", async () => {
    useAppStore.setState({ effortLevel: "medium", runtimeCapabilities: { provider_capabilities: { reasoning_effort: true, reasoning_effort_levels: ["low", "medium", "high", "xhigh"] } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    const modelControl = screen.getByRole("group", { name: "模型与推理强度" });
    expect(modelControl.querySelectorAll('button')).toHaveLength(1);
    expect(screen.queryByText("选择强度")).toBeNull();
    expect(modelControl.querySelector('.composer-model-trigger-label')?.textContent).toBeTruthy();
    expect(modelControl.querySelector('.composer-model-trigger-effort')?.textContent).toBe("中");
    expect(screen.queryByRole("button", { name: "中", exact: true })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度：.*，中/ }));
    const slider = screen.getByRole("slider", { name: "推理强度" });
    fireEvent.change(slider, { target: { value: "2" } });
    fireEvent.change(slider, { target: { value: "3" } });
    expect(slider.getAttribute("aria-valuetext")).toBe("极高");
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    fireEvent.pointerUp(slider);
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ reasoning_effort: "xhigh" }), "effort"));
    expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(1);
  });

  it("shows the selected model and effort together on the single trigger", () => {
    useAppStore.setState({ currentModel: "gpt-6.1-sol", effortLevel: "ultra",
      runtimeCapabilities: { provider_capabilities: { model: "gpt-6.1-sol", reasoning_effort: true,
        reasoning_effort_levels: ["low", "medium", "high", "xhigh", "max", "ultra"] } },
    });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    const trigger = screen.getByRole("button", { name: "模型与推理强度：6.1 Sol，Ultra" });
    expect(trigger.querySelector(".composer-model-trigger-label")?.textContent).toBe("6.1 Sol");
    expect(trigger.querySelector(".composer-model-trigger-effort")?.textContent).toBe("Ultra");
    expect(screen.queryByText("选择强度")).toBeNull();
    act(() => useAppStore.setState({ effortLevel: "medium" }));
    expect(trigger.querySelector(".composer-model-trigger-effort")?.textContent).toBe("中");
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog", { name: "模型与推理强度" })).toBeTruthy();
  });

  it("opens the model list from the effort chevron and switches the conversation model", async () => {
    useAppStore.setState({
      availableModels: ["gpt-5", "gpt-5.5"],
      runtimeCapabilities: { provider_capabilities: {
        reasoning_effort: true, reasoning_effort_levels: ["low", "medium", "high"],
      } },
    });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    expect(screen.queryByRole("button", { name: "高", exact: true })).toBeNull();
    const trigger = screen.getByRole("button", { name: "模型与推理强度：5，高" });
    fireEvent.click(trigger);
    expect(screen.queryByRole("button", { name: "高", exact: true })).toBeNull();
    expect(document.querySelector('.composer-effort-value')?.tagName).toBe("SPAN");
    expect(screen.getByRole("button", { name: "选择模型" }).textContent).toContain("5");
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("listbox", { name: "推理档位" })).toBeNull();
    expect(screen.getByRole("listbox", { name: "选择模型" })).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("option", { name: "5", exact: true })));
    fireEvent.click(screen.getByRole("option", { name: "5.5", exact: true }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({ type: "llm.model.set", model: "gpt-5.5", selection_mode: "model", conversation_id: "conv-footer" }, "model.set"));
    expect(screen.queryByRole("listbox")).toBeNull();
    await waitFor(() => expect(screen.getByRole("slider")).toBe(document.activeElement));
  });

  it("uses one trigger for the model and reasoning picker and restores its focus", async () => {
    useAppStore.setState({ availableModels: ["gpt-5", "gpt-5.5"], runtimeCapabilities: {
      provider_capabilities: { reasoning_effort: true, reasoning_effort_levels: ["low", "medium", "high"] },
    } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    const trigger = screen.getByRole("button", { name: "模型与推理强度：5，高" });
    expect(screen.getByRole("group", { name: "模型与推理强度" }).querySelectorAll('button')).toHaveLength(1);
    fireEvent.click(trigger);
    expect(screen.queryByRole("listbox", { name: "选择模型" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "模型与推理强度" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "选择模型" }).textContent).toContain("5");
    const slider = screen.getByRole("slider", { name: "推理强度" });
    await waitFor(() => expect(document.activeElement).toBe(slider));
    fireEvent.keyDown(slider, { key: "Escape" });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(screen.queryByRole("slider")).toBeNull();
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("keeps the model list non-looping with Tab trapped while leaving the reasoning dialog's traversal separate", async () => {
    useAppStore.setState({ availableModels: ["gpt-5", "gpt-5.5"],
      runtimeCapabilities: { provider_capabilities: { reasoning_effort: false, reasoning_effort_levels: [] } } });
    const view = render(<FooterRow sendState="idle" onSend={() => {}} />);
    const trigger = view.container.querySelector<HTMLButtonElement>(".composer-model-select")!;
    fireEvent.click(trigger);
    const first = screen.getByRole("option", { name: "5", exact: true });
    const last = screen.getByRole("option", { name: "5.5", exact: true });
    await waitFor(() => expect(document.activeElement).toBe(first));
    fireEvent.keyDown(first, { key: "ArrowUp" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(first, { key: "End" });
    expect(document.activeElement).toBe(last);
    for (const event of [{ key: "ArrowDown" }, { key: "Tab" }, { key: "Tab", shiftKey: true }]) {
      expect(fireEvent.keyDown(last, event)).toBe(false);
      expect(document.activeElement).toBe(last);
      expect(screen.getByRole("listbox", { name: "选择模型" })).toBeTruthy();
    }
    fireEvent.keyDown(last, { key: "Escape" });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("reveals the selected model in a long list while leaving IME navigation to composition", async () => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    useAppStore.setState({ currentModel: "model-59", availableModels: Array.from({ length: 60 }, (_, index) => `model-${index}`),
      runtimeCapabilities: { provider_capabilities: { reasoning_effort: false, reasoning_effort_levels: [] } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByTitle("model-59"));
    const selected = screen.getByRole("option", { name: "model-59", exact: true });
    await waitFor(() => expect(document.activeElement).toBe(selected));
    expect(scroll).toHaveBeenCalledWith({ block: "nearest" });
    expect(scroll.mock.contexts.at(-1)).toBe(selected);
    fireEvent.keyDown(selected, { key: "Home", isComposing: true });
    fireEvent.keyDown(selected, { key: "Escape", keyCode: 229 });
    expect(document.activeElement).toBe(selected);
    expect(screen.getByRole("listbox", { name: "选择模型" })).toBeTruthy();
    fireEvent.keyDown(selected, { key: "Home" });
    expect(document.activeElement).toBe(screen.getByRole("option", { name: "model-0", exact: true }));
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("does not close or commit the reasoning control during IME Enter or navigation", async () => {
    useAppStore.setState({ effortLevel: "medium", runtimeCapabilities: { provider_capabilities: {
      reasoning_effort: true, reasoning_effort_levels: ["low", "medium", "high"],
    } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度/ }));
    const slider = screen.getByRole("slider");
    await waitFor(() => expect(document.activeElement).toBe(slider));
    fireEvent.change(slider, { target: { value: "2" } });
    fireEvent.keyUp(slider, { key: "ArrowRight", isComposing: true });
    fireEvent.keyUp(slider, { key: "Home", keyCode: 229 });
    fireEvent.keyDown(slider, { key: "Enter", isComposing: true });
    fireEvent.keyDown(slider, { key: "Escape", keyCode: 229 });
    expect(screen.getByRole("slider")).toBe(slider);
    expect(document.activeElement).toBe(slider);
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    fireEvent.pointerCancel(slider);
    expect(slider.getAttribute("aria-valuetext")).toBe("中");
  });

  it("keeps model-menu Tab navigation inside its controls and confirms the current effort with Enter", async () => {
    useAppStore.setState({ runtimeCapabilities: { provider_capabilities: { reasoning_effort: true, reasoning_effort_levels: ["low", "medium", "high"] } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    const trigger = screen.getByRole("button", { name: /模型与推理强度/ });
    fireEvent.click(trigger);
    const slider = screen.getByRole("slider");
    await waitFor(() => expect(document.activeElement).toBe(slider));
    fireEvent.keyDown(slider, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "选择模型" }));
    fireEvent.keyDown(document.activeElement!, { key: "Tab" });
    expect(document.activeElement).toBe(slider);
    fireEvent.keyDown(slider, { key: "Enter" });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(screen.queryByRole("slider")).toBeNull();
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("closes stale controls during conversation switches and contains menu Escape before the global interrupt handler", async () => {
    useAppStore.setState({ runtimeCapabilities: { provider_capabilities: { reasoning_effort: true, reasoning_effort_levels: ["low", "high"] } } });
    const interrupt = vi.fn();
    window.addEventListener("keydown", interrupt);
    const view = render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度/ }));
    fireEvent.keyDown(screen.getByRole("slider"), { key: "Escape" });
    expect(interrupt).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度/ }));
    act(() => useAppStore.setState({ pendingConversationSwitchId: "other-owner" }));
    expect(screen.queryByRole("dialog", { name: "模型与推理强度" })).toBeNull();
    expect(screen.getByRole("button", { name: /模型与推理强度/ })).toHaveProperty("disabled", true);
    window.removeEventListener("keydown", interrupt);
    view.unmount();
    act(() => useAppStore.setState({ pendingConversationSwitchId: null }));
  });

  it("closes the model list when keyboard focus moves to the next composer control", async () => {
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByTitle("gpt-5"));
    const option = screen.getByRole("option", { name: "5", exact: true });
    await waitFor(() => expect(document.activeElement).toBe(option));
    const next = screen.getByRole("button", { name: "配置听写" });
    act(() => next.focus());
    expect(screen.queryByRole("listbox", { name: "选择模型" })).toBeNull();
    expect(document.activeElement).toBe(next);
  });

  it("cancels a slider preview and commits keyboard changes once", async () => {
    useAppStore.setState({ effortLevel: "medium", runtimeCapabilities: { provider_capabilities: {
      reasoning_effort: true, reasoning_effort_levels: ["low", "medium", "high"],
    } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度：.*，中/ }));
    const slider = screen.getByRole("slider");
    fireEvent.change(slider, { target: { value: "2" } });
    fireEvent.pointerCancel(slider);
    expect(slider.getAttribute("aria-valuetext")).toBe("中");
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    fireEvent.change(slider, { target: { value: "0" } });
    fireEvent.keyUp(slider, { key: "Home" });
    fireEvent.blur(slider);
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ reasoning_effort: "low" }), "effort"));
    expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(1);
  });

  it("hides the retained effort when the current model does not declare it", () => {
    useAppStore.setState({ effortLevel: "max", runtimeCapabilities: { provider_capabilities: {
      reasoning_effort: true, reasoning_effort_levels: ["low", "medium", "high"],
    } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    const trigger = screen.getByTitle("gpt-5");
    fireEvent.click(trigger);
    expect(screen.queryByRole("slider")).toBeNull();
    expect(screen.getByRole("listbox", { name: "选择模型" })).toBeTruthy();
    expect(screen.queryByText("最大（不支持）")).toBeNull();
  });

  it("keeps the picker and latest selection stable while earlier effort replies arrive", async () => {
    const replies: Array<(result: any) => void> = [];
    vi.mocked(sendClientCommandAwaitResult)
      .mockImplementationOnce(() => new Promise(resolve => replies.push(resolve)))
      .mockImplementationOnce(() => new Promise(resolve => replies.push(resolve)));
    useAppStore.setState({ effortLevel: "medium", runtimeCapabilities: { provider_capabilities: {
      reasoning_effort: true, reasoning_effort_levels: ["low", "medium", "high"],
    } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度：.*，中/ }));
    const slider = screen.getByRole("slider");
    fireEvent.change(slider, { target: { value: "0" } });
    fireEvent.pointerUp(slider);
    expect(screen.getByRole("button", { name: /模型与推理强度：.*，低/ })).toBeTruthy();
    fireEvent.change(slider, { target: { value: "2" } });
    fireEvent.pointerUp(slider);
    expect(screen.getByRole("button", { name: /模型与推理强度：.*，高/ })).toBeTruthy();
    await waitFor(() => expect(replies).toHaveLength(2));
    await act(async () => {
      useAppStore.setState({ effortLevel: "low" });
      replies[0]({ type: "command.result", command: "effort", level: "success" });
    });
    expect(screen.getByRole("slider")).toBe(slider);
    expect(slider.getAttribute("aria-valuetext")).toBe("高");
    await act(async () => {
      replies[1]({ type: "command.result", command: "effort", level: "success" });
      useAppStore.setState({ effortLevel: "high" });
    });
    expect(screen.getByRole("slider")).toBe(slider);
    expect(slider.getAttribute("aria-valuetext")).toBe("高");
  });

  it("returns to the acknowledged effort when a selection is refused", async () => {
    vi.mocked(sendClientCommandAwaitResult).mockResolvedValueOnce({ type: "command.result", command: "effort", level: "error", message: "refused" });
    useAppStore.setState({ effortLevel: "medium", runtimeCapabilities: { provider_capabilities: {
      reasoning_effort: true, reasoning_effort_levels: ["low", "medium", "high"],
    } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度：.*，中/ }));
    fireEvent.change(screen.getByRole("slider"), { target: { value: "2" } });
    fireEvent.pointerUp(screen.getByRole("slider"));
    await waitFor(() => expect(screen.getByRole("button", { name: /模型与推理强度：.*，中/ })).toBeTruthy());
    expect(screen.getByRole("slider").getAttribute("aria-valuetext")).toBe("中");
  });

  it("does not display an unknown usage placeholder in the footer", () => {
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    expect(screen.queryByRole("button", { name: "查看会话用量详情" })).toBeNull();
  });

  it("does not offer a reset when no real project default selection is available", () => {
    useAppStore.setState({ effortLevel: "high", runtimeCapabilities: { provider_capabilities: { reasoning_effort: true, reasoning_effort_levels: ["low", "medium", "high"] } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度：.*，高/ }));
    expect(screen.queryByRole("button", { name: "恢复中等推理强度" })).toBeNull();
    expect(screen.queryByRole("button", { name: "重置为默认" })).toBeNull();
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("shows the exact selected model name", () => {
    useAppStore.setState({
      currentModel: "deepseek-v4-flash",
      availableModels: ["deepseek-v4-flash"],
    });

    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    expect(screen.getByText("deepseek-v4-flash")).toBeTruthy();
    expect(screen.queryByText("deepseek-v4")).toBeNull();
  });

  it("uses a clear model selection prompt when no model is configured", () => {
    useAppStore.setState({
      currentModel: "",
      availableModels: [],
    });

    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    expect(screen.getByTitle("选择模型").textContent).toContain("选择模型");
    expect(screen.queryByText("No model")).toBeNull();
  });

  it("uses the same accessible stop label for the active streaming action", () => {
    const onStop = vi.fn();
    render(<FooterRow sendState="stop" onSend={onStop} />);

    const stop = screen.getByRole("button", { name: "停止当前回复" });
    fireEvent.click(stop);

    expect(onStop).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "停止" })).toBeNull();
  });

  it("shows only the permission range resolved by managed requirements", () => {
    useAppStore.setState({ permissionMode: "plan", runtimeCapabilities: { permission: { available_modes: ["plan"] } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByTitle("更改权限"));
    expect(screen.getByRole("option", { name: "请求批准" }).getAttribute("aria-selected")).toBe("true");
    expect((screen.getByRole("option", { name: "完全访问" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("被 requirements.toml 禁用")).toBeTruthy();
    fireEvent.click(screen.getByRole("option", { name: "完全访问" }));
    expect(showConfirm).not.toHaveBeenCalled();
  });

  it("confirms Full access again when selecting the current Full access option", async () => {
    useAppStore.setState({ permissionMode: "bypass" });
    vi.mocked(showConfirm).mockResolvedValueOnce(false);
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByTitle("更改权限"));
    fireEvent.click(screen.getByRole("option", { name: "完全访问" }));
    await waitFor(() => expect(showConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: "要开启完全访问吗？", danger: true })));
    const details = vi.mocked(showConfirm).mock.calls[0][0].message;
    render(<>{details}</>);
    expect(screen.getByText("读取、创建、修改、上传或删除此计算机上任意位置的文件")).toBeTruthy();
    expect(screen.getByText("运行命令、安装软件和更改系统设置")).toBeTruthy();
    expect(screen.getByText("访问网站、发送数据并使用已启用的插件")).toBeTruthy();
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it.each(["settings", "models_json", "configured", "extension"])("restores the authoritative %s project model and effort pair and keeps its catalog", async (modelsSource) => {
    const buffers = { textStreamBuffer: { destroy: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn() } } as Parameters<typeof handleSessionEvent>[1];
    const receive = (model: string, effort: string, mode: "default" | "model") => handleSessionEvent({
      type: "llm.model.updated", conversation_id: "conv-footer", current_model: model, effective_reasoning_effort: effort,
      model_selection_mode: mode, default_model_selection: { provider: "custom", model: "model-a", reasoning_effort: "low", conversation_id: "conv-footer", working_directory: useAppStore.getState().workingDirectory },
      available_model_options: [{ model: "model-a", reasoning_effort_levels: ["low", "high"], default_reasoning_effort: "low" }],
      available_models: ["model-a", "model-b"], provider: "custom", models_source: modelsSource,
    }, buffers);
    act(() => receive("model-b", "high", "model"));
    useAppStore.setState({ runtimeCapabilities: { permission: { available_modes: ["confirm", "bypass"] },
      provider_capabilities: { reasoning_effort_supported: true, reasoning_effort_levels: ["low", "high"] } }, draft: "unsaved draft" });
    vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(async () => {
      receive("model-a", "low", "default");
      return { type: "command.result", command: "model.set", level: "success", message: "", data: {} };
    });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度：/ }));
    fireEvent.click(screen.getByRole("button", { name: "重置为默认" }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({ type: "llm.model.set", selection_mode: "default", conversation_id: "conv-footer" }, "model.set"));
    await waitFor(() => expect(useAppStore.getState()).toMatchObject({ currentModel: "model-a", effortLevel: "low", modelSelectionMode: "default", draft: "unsaved draft" }));
    expect(screen.queryByRole("button", { name: "重置为默认" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "选择模型" }));
    expect(screen.getByRole("option", { name: /默认/ }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("option", { name: "model-b", exact: true })).toBeTruthy();
    expect(screen.queryByText("推荐模型集")).toBeNull();
  });

  it.each([true, false, null] as const)("uses the real Ultra and Full access choice %s", async (choice) => {
    useAppStore.setState({ permissionMode: "bypass", effortLevel: "high", runtimeCapabilities: {
      permission: { available_modes: ["plan", "bypass"] }, provider_capabilities: {
        reasoning_effort_supported: true, reasoning_effort_levels: ["high", "ultra"],
      },
    } });
    vi.mocked(showChoice).mockResolvedValueOnce(choice);
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度：/ }));
    const slider = screen.getByRole("slider");
    fireEvent.change(slider, { target: { value: "1" } });
    fireEvent.pointerUp(slider);
    await waitFor(() => expect(showChoice).toHaveBeenCalledWith(expect.objectContaining({ title: "要将 Ultra 与完全访问搭配使用吗？", cancelDisabled: false })));
    await act(async () => {});
    if (choice === null) expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    else {
      expect(sendClientCommandAwaitResult).toHaveBeenLastCalledWith(expect.objectContaining({ type: "llm.config.set", reasoning_effort: "ultra", conversation_id: "conv-footer" }), "effort");
      if (choice === false) {
        expect(sendClientCommandAwaitResult).toHaveBeenNthCalledWith(1, expect.objectContaining({ type: "conversation.permission_mode.set", mode: "plan" }), "conversation.permission_mode.set");
        expect(useAppStore.getState().permissionMode).toBe("plan");
      } else expect(useAppStore.getState().permissionMode).toBe("bypass");
    }
  });

  it("does not apply an Ultra choice to a conversation selected during the permission acknowledgement", async () => {
    useAppStore.setState({ permissionMode: "bypass", effortLevel: "high", runtimeCapabilities: { permission: { available_modes: ["confirm", "bypass"] },
      provider_capabilities: { reasoning_effort_supported: true, reasoning_effort_levels: ["high", "ultra"] } } });
    vi.mocked(showChoice).mockResolvedValueOnce(false);
    vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(async () => {
      useAppStore.setState({ conversationId: "other-task", effortLevel: "low" });
      return { type: "command.result", command: "conversation.permission_mode.set", level: "success", message: "", data: {} };
    });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /模型与推理强度：/ }));
    const slider = screen.getByRole("slider");
    fireEvent.change(slider, { target: { value: "1" } });
    fireEvent.pointerUp(slider);
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(1));
    await act(async () => {});
    expect(useAppStore.getState().effortLevel).toBe("low");
  });
});
