/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { FooterRow } from "./FooterRow";
import { uploadComposerFiles } from "./uploads";
import { showConfirm } from "../overlays/DialogService";

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
      appMode: "cowork",
      availableModels: ["gpt-5"],
      effortLevel: "high",
      prMonitor: null,
      contextUsage: null,
      budgetBuckets: [],
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

    expect(screen.getByLabelText("显示上下文和令牌用量")).toBeTruthy();
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
    expect((screen.getByTitle(
      "模型推理强度：最大推理强度。当前 Provider 未声明支持该强度，请改选下方受支持的档位。",
    ) as HTMLElement).style.color).toBe("var(--text-secondary)");
    expect(screen.getByText("最大（不支持）")).toBeTruthy();
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
    fireEvent.click(screen.getByRole("button", { name: "选择推理档位" }));

    expect(screen.getByText("低")).toBeTruthy();
    expect(screen.getAllByText("focused").length).toBeGreaterThan(0);
    expect(screen.getByText("Ultra")).toBeTruthy();
    expect(screen.queryByText("中")).toBeNull();
    expect(screen.queryByText("极高")).toBeNull();
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
    fireEvent.click(screen.getByRole("button", { name: "选择推理档位" }));
    fireEvent.click(screen.getByText("极高"));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "llm.config.set",
      provider: "openai",
      reasoning_effort: "xhigh",
      source: "frontend.footer",
    }, "effort"));
  });

  it("keeps a standard reasoning ladder compact but never hides the live level", () => {
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

    // The pill names the configured level. It used to display 极高 (xhigh)
    // because narrowing dropped `ultra` from the ladder, which also made the
    // real level impossible to reselect.
    fireEvent.click(screen.getByTitle("模型推理强度：Ultra 推理强度。仅在当前 Provider/模型支持时生效，不改变工具迭代预算。"));
    fireEvent.click(screen.getByRole("button", { name: "选择推理档位" }));

    expect(screen.getByText("低")).toBeTruthy();
    expect(screen.getByText("中")).toBeTruthy();
    expect(screen.getByText("高")).toBeTruthy();
    expect(screen.getByText("极高")).toBeTruthy();
    expect(screen.getAllByText("Ultra").length).toBeGreaterThan(0);
    // Still narrowed: `max` is declared but neither standard nor selected.
    expect(screen.queryByText("最大")).toBeNull();
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
    expect(screen.getAllByText("最低").length).toBeGreaterThan(0);
    expect(screen.queryByText("中（不支持）")).toBeNull();

    fireEvent.click(pill);
    fireEvent.click(screen.getByRole("button", { name: "选择推理档位" }));

    const choices = screen.getAllByRole("button").filter((node) => node.textContent === "最低");
    expect(choices.length).toBeGreaterThan(0);
    expect(screen.getByText("低")).toBeTruthy();
    expect(screen.getByText("中")).toBeTruthy();
    expect(screen.getByText("高")).toBeTruthy();
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
    expect(screen.getByRole("group", { name: "模型与推理强度" }).querySelectorAll(':scope > div')).toHaveLength(2);
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

  it("does not display an unknown usage placeholder in the footer", () => {
    render(<FooterRow sendState="idle" onSend={() => {}} />);
    expect(screen.queryByRole("button", { name: "显示上下文和令牌用量" })).toBeNull();
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
