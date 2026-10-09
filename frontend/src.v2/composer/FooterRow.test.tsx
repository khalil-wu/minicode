/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { FooterRow } from "./FooterRow";
import { uploadComposerFiles } from "./uploads";
import { showConfirm } from "../overlays/DialogService";
import { handleSessionEvent } from "../chat/sessionEvents";
import { handleRuntimeEvent } from "../chat/runtimeEvents";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";

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
    useAppStore.setState({
      permissionMode: "auto",
      agentMode: "build",
      currentModel: "gpt-5",
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
      runtimeCapabilities: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("opens the hidden file input from the attach button and uploads selected files", () => {
    render(<FooterRow sendState="idle" onSend={() => {}} />);

    const attach = screen.getByRole("button", { name: "添加附件" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const clickSpy = vi.spyOn(input, "click").mockImplementation(() => {});

    fireEvent.click(attach);
    expect(clickSpy).toHaveBeenCalledTimes(1);

    const files = [
      new File(["hello"], "note.txt", { type: "text/plain" }),
      new File(["{}"], "data.json", { type: "application/json" }),
    ];
    fireEvent.change(input, { target: { files } });

    expect(uploadComposerFiles).toHaveBeenCalledWith(files);
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

  it("shows all permission choices with distinct icons and concise descriptions", () => {
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent.click(screen.getByTitle("权限：自动"));

    expect(document.querySelector(".mc-dropdown-menu.composer-picker-menu")).toBeTruthy();
    expect(screen.getByText("询问")).toBeTruthy();
    expect(screen.getAllByText("自动").length).toBeGreaterThan(0);
    expect(screen.getByText("完全访问")).toBeTruthy();
    expect(screen.queryByText("Ask before file and network actions")).toBeNull();
    expect(screen.queryByText("Auto read, search, and edit workspace files")).toBeNull();
    expect(screen.queryByText("Use files, network, edits, and commands without prompts")).toBeNull();
    expect(screen.getByText("规划")).toBeTruthy();
    expect(screen.getByRole("option", { name: "询问" }).querySelector(".lucide-hand")).toBeTruthy();
    expect(screen.getByRole("option", { name: "完全访问" }).querySelector(".lucide-shield-alert")).toBeTruthy();
    expect(screen.getByText("敏感操作前请求确认")).toBeTruthy();
    expect(screen.queryByText("Accept")).toBeNull();
    expect(screen.queryByText("Auto-accept file edits, ask for commands")).toBeNull();
  });

  it("switches the conversation into Plan permission mode", async () => {
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent.click(screen.getByTitle("权限：自动"));
    fireEvent.click(screen.getByText("规划"));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "conversation.permission_mode.set",
      mode: "plan",
      source: "frontend.ui",
      conversation_id: "conv-footer",
    }, "conversation.permission_mode.set"));
  });

  it("asks for confirmation before switching to bypass", async () => {
    let accept!: (value: Awaited<ReturnType<typeof sendClientCommandAwaitResult>>) => void;
    vi.mocked(sendClientCommandAwaitResult).mockReturnValueOnce(new Promise((resolve) => { accept = resolve; }));
    const confirmSpy = vi.mocked(showConfirm);
    confirmSpy.mockClear();
    confirmSpy.mockResolvedValue(true);
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent.click(screen.getByTitle("权限：自动"));
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
    expect(screen.getByTitle("权限：完全访问")).toBeTruthy();
  });

  it("keeps the current mode when the bypass confirmation is cancelled", async () => {
    const confirmSpy = vi.mocked(showConfirm);
    confirmSpy.mockClear();
    confirmSpy.mockResolvedValue(false);
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent.click(screen.getByTitle("权限：自动"));
    fireEvent.click(screen.getByText("完全访问"));

    await waitFor(() => {
      expect(confirmSpy).toHaveBeenCalled();
    });
    expect(useAppStore.getState().permissionMode).toBe("auto");
  });

  it("opens the permission menu from the global shortcut event", () => {
    render(<FooterRow sendState="disabled" onSend={() => {}} />);

    fireEvent(document, new CustomEvent("open-permission-menu"));

    expect(screen.getByText("询问")).toBeTruthy();
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

    expect(screen.getByText("gpt-5")).toBeTruthy();
    expect(screen.getByText("配置模型…")).toBeTruthy();
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
    expect(screen.getByText("配置模型…")).toBeTruthy();
  });

  it("sends model changes through the shared websocket outbox", () => {
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
    fireEvent.click(screen.getByText("gpt-5"));

    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "llm.model.set",
      conversation_id: "conv-footer",
      model: "gpt-5",
    });
  });

  it("opens provider settings from Configure without closing an open settings dialog", async () => {
    useAppStore.setState({
      currentModel: "deepseek-v4-flash",
      availableModels: ["deepseek-v4-flash", "gpt-5"],
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
    expect(screen.getByText("gpt-5-mini")).toBeTruthy();
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

    expect(screen.getByTitle("权限：自动")).toBeTruthy();
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

    expect((screen.getByTitle("权限：规划") as HTMLElement).style.color).toBe("var(--text-secondary)");
    // `max` is not among the provider's declared levels. Previously the pill
    // silently rendered the nearest extreme level (极高) as if it were the
    // configured value; it now names the real level and says it is unsupported.
    expect(screen.getByTitle(
      "模型推理强度：最大推理强度。当前 Provider 未声明支持该强度，请改选下方受支持的档位。",
    ).tagName).toBe("BUTTON");
    expect(screen.getByRole("button", { name: "模型与推理强度：5，最大（不支持）" })).toBeTruthy();
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

    fireEvent.click(screen.getByTitle("模型推理强度：Provider 声明的推理强度：focused。仅在当前 Provider/模型支持时生效，不改变工具迭代预算。"));
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

    fireEvent.click(screen.getByTitle("模型推理强度：中等推理强度。仅在当前 Provider/模型支持时生效，不改变工具迭代预算。"));
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

    fireEvent.click(screen.getByTitle("模型推理强度：Ultra 推理强度。仅在当前 Provider/模型支持时生效，不改变工具迭代预算。"));
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

  it("describes Ultra as collaboration while retaining its canonical value and showing the actual model effort", () => {
    useAppStore.setState({ currentModel: "gpt-6.1-sol", effortLevel: "ultra", runtimeCapabilities: { provider_capabilities: {
      model: "gpt-6.1-sol", reasoning_effort_supported: true,
      reasoning_effort_levels: ["low", "medium", "high", "xhigh", "max", "ultra"],
      reasoning_effort_wire_map: { ultra: "xhigh" }, wire_reasoning_effort: "xhigh",
    } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    const control = screen.getByRole("button", { name: "模型与推理强度：6.1 Sol，Ultra" });
    expect(control.title).toContain("主动多智能体协作；模型请求使用极高推理");
    fireEvent.click(control);
    expect(screen.getByRole("slider").getAttribute("aria-valuetext")).toBe("Ultra");
    expect(screen.getByRole("slider").getAttribute("max")).toBe("5");
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

    const pill = screen.getByTitle(
      "模型推理强度：最低推理强度。仅在当前 Provider/模型支持时生效，不改变工具迭代预算。",
    );
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

    expect(screen.getByTitle("模型推理强度：中等推理强度。仅在当前 Provider/模型支持时生效，不改变工具迭代预算。")).toBeTruthy();
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
    fireEvent.click(screen.getByTitle(/模型推理强度：中等/));
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
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("option", { name: "gpt-5", exact: true })));
    fireEvent.click(screen.getByRole("option", { name: "gpt-5.5", exact: true }));
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "llm.model.set", model: "gpt-5.5", conversation_id: "conv-footer" });
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(trigger));
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

  it("closes the model list when keyboard focus moves to the next composer control", async () => {
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByTitle("gpt-5"));
    const option = screen.getByRole("option", { name: "gpt-5", exact: true });
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
    fireEvent.click(screen.getByTitle(/模型推理强度：中等/));
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

  it("offers declared choices when the current effort is unsupported", () => {
    useAppStore.setState({ effortLevel: "max", runtimeCapabilities: { provider_capabilities: {
      reasoning_effort: true, reasoning_effort_levels: ["low", "medium", "high"],
    } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByTitle(/模型推理强度：最大/));
    expect(screen.queryByRole("slider")).toBeNull();
    fireEvent.click(screen.getByRole("option", { name: "中" }));
    expect(screen.getByRole("slider").getAttribute("aria-valuetext")).toBe("中");
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
    fireEvent.click(screen.getByTitle(/模型推理强度：中等/));
    const slider = screen.getByRole("slider");
    fireEvent.change(slider, { target: { value: "0" } });
    fireEvent.pointerUp(slider);
    expect(screen.getByTitle(/模型推理强度：低/)).toBeTruthy();
    fireEvent.change(slider, { target: { value: "2" } });
    fireEvent.pointerUp(slider);
    expect(screen.getByTitle(/模型推理强度：高/)).toBeTruthy();
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
    fireEvent.click(screen.getByTitle(/模型推理强度：中等/));
    fireEvent.change(screen.getByRole("slider"), { target: { value: "2" } });
    fireEvent.pointerUp(screen.getByRole("slider"));
    await waitFor(() => expect(screen.getByTitle(/模型推理强度：中等/)).toBeTruthy());
    expect(screen.getByRole("slider").getAttribute("aria-valuetext")).toBe("中");
  });

  it("does not display an unknown usage placeholder in the footer", () => {
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    expect(screen.queryByRole("button", { name: "查看会话用量详情" })).toBeNull();
  });

  it("resets to a declared medium level without inventing unsupported levels", () => {
    useAppStore.setState({ effortLevel: "high", runtimeCapabilities: { provider_capabilities: { reasoning_effort: true, reasoning_effort_levels: ["low", "medium", "high"] } } });
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    fireEvent.click(screen.getByTitle(/模型推理强度：高/));
    fireEvent.click(screen.getByRole("button", { name: "恢复中等推理强度" }));
    expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ reasoning_effort: "medium" }), "effort");
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
});
