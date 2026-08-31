/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import type { LLMSettingsPayload } from "./settingsShared";
import { ProviderTab } from "./ProviderTab";
import { SettingsCenter } from "./SettingsCenter";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";

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

const fetchLLMSettingsMock = vi.fn();
const desktopModeMock = vi.fn(() => false);
const openExternalMock = vi.fn(async () => true);
const embeddedBrowserSetSettingsMock = vi.fn(async (payload: { downloadPolicy?: string }) => ({ downloadPolicy: payload.downloadPolicy || "block", origin: "", permissions: [] }));
const awaitCommandResultMock = vi.fn(async (command: { type: string }) => ({ type: "command.result", command: command.type, level: "info", message: "", data: {} }));

vi.mock("../protocol/api", () => ({
  apiBase: () => "http://test.local",
  authHeaders: (headers?: HeadersInit) => headers ?? {},
  errorMessageFromResponseText: (text: string, fallback: string) => text || fallback,
  fetchWithTimeout: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, init),
  LONG_HTTP_TIMEOUT_MS: 300_000,
  fetchLLMSettings: () => fetchLLMSettingsMock(),
}));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(),
  sendClientCommandAwaitResult: (command: { type: string }) => awaitCommandResultMock(command),
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed",
  LONG_COMMAND_RESULT_TIMEOUT_MS: 300_000,
}));

vi.mock("../desktop/runtime", () => ({
  desktop: () => null,
  embeddedBrowserGetSettings: vi.fn(async () => ({ downloadPolicy: "block", origin: "", permissions: [] })),
  embeddedBrowserList: vi.fn(async () => []),
  embeddedBrowserSetSettings: (payload: { downloadPolicy?: string }) => embeddedBrowserSetSettingsMock(payload),
  envDetect: vi.fn(async () => null),
  isDesktop: () => desktopModeMock(),
  openExternal: (url: string) => openExternalMock(url),
}));

describe("SettingsCenter reasoning effort visibility", () => {
  beforeEach(() => {
    desktopModeMock.mockReturnValue(false);
    openExternalMock.mockClear();
    embeddedBrowserSetSettingsMock.mockClear();
    awaitCommandResultMock.mockReset();
    awaitCommandResultMock.mockImplementation(async (command: { type: string }) => ({
      type: "command.result",
      command: command.type,
      level: "info",
      message: "",
      data: {},
    }));
    fetchLLMSettingsMock.mockResolvedValue({
      provider: "custom",
      active_model: "deepseek-v4-flash",
      custom: {
        has_api_key: true,
        api_key: "sk-deepseek-visible",
        base_url: "https://api.deepseek.com/v1",
        model: "deepseek-v4-flash",
        available_models: ["deepseek-v4-flash"],
        wire_api: "chat",
        reasoning_effort: "low",
        configured_reasoning_effort: "low",
        effective_reasoning_effort: "",
        reasoning_effort_supported: false,
        reasoning_effort_levels: [],
        model_metadata: {},
        context_window: 200000,
        context_window_source: "fallback",
        context_window_verified: false,
      },
      provider_history: [
        {
          provider: "custom",
          provider_id: "deepseek",
          display_name: "DeepSeek",
          has_api_key: true,
          api_key: "sk-deepseek-visible",
          base_url: "https://api.deepseek.com/v1",
          model: "deepseek-v4-flash",
          available_models: ["deepseek-v4-flash"],
          wire_api: "chat",
          reasoning_effort: "low",
          configured_reasoning_effort: "low",
          effective_reasoning_effort: "",
          reasoning_effort_supported: false,
          reasoning_effort_levels: [],
          model_metadata: {},
          context_window: 200000,
          context_window_source: "fallback",
          context_window_verified: false,
          updated_at: 20,
        },
        {
          provider: "custom",
          provider_id: "openrouter",
          display_name: "OpenRouter",
          has_api_key: true,
          api_key: "sk-openrouter-visible",
          base_url: "https://openrouter.ai/api/v1",
          model: "anthropic/claude-sonnet-4",
          available_models: ["anthropic/claude-sonnet-4", "openai/gpt-5.2"],
          wire_api: "chat",
          updated_at: 10,
        },
        {
          provider: "openai",
          provider_id: "openai_official",
          display_name: "OpenAI",
          base_url: "https://api.openai.com/v1",
          model: "gpt-5",
          available_models: ["gpt-5"],
          wire_api: "responses",
          updated_at: 8,
        },
        {
          provider: "anthropic",
          provider_id: "anthropic_off",
          display_name: "Anthropic",
          base_url: "",
          model: "claude-sonnet-4-6",
          available_models: ["claude-sonnet-4-6"],
          wire_api: "anthropic",
          updated_at: 6,
        },
      ],
      openai: {
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        available_models: ["gpt-5"],
        wire_api: "responses",
      },
      anthropic: {
        model: "claude-sonnet-4-6",
        available_models: ["claude-sonnet-4-6"],
      },
    });
    useAppStore.setState({
      settingsOpen: true,
      settingsTab: "general",
      themeMode: "dark",
      permissionMode: "auto",
      effortLevel: "high",
      currentModel: "deepseek-v4-flash",
      currentProvider: "custom",
      currentProviderId: "deepseek",
      currentProviderBaseUrl: "https://api.deepseek.com/v1",
      currentWireApi: "chat",
      availableModels: ["deepseek-v4-flash"],
      runtimeCapabilities: null,
      appMode: "code",
      viewMode: "normal",
      sendShortcut: "enter",
      followUpBehavior: "queue",
      codeTextScale: 1,
      reducedMotion: false,
      shortcutBindings: { ...DEFAULT_SHORTCUT_BINDINGS },
      allowedRemoteImageDomains: [],
      availableSkills: [],
      selectedSkills: [],
      conversationId: undefined,
      conversations: [],
      providerOAuthFlowsByConversation: {},
    });
  });

  afterEach(() => {
    cleanup();
    fetchLLMSettingsMock.mockReset();
    vi.clearAllMocks();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("hides reasoning effort for DeepSeek chat completions because runtime will ignore it", async () => {
    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());

    expect(screen.queryByText("推理强度")).toBeNull();
    expect(screen.queryByRole("button", { name: "Low" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Max" })).toBeNull();
  });

  it("keeps system, dark, and light theme controls in appearance settings", () => {
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "外观" }));

    expect(screen.getByRole("radio", { name: "深色" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("radio", { name: "浅色" }));
    expect(useAppStore.getState().themeMode).toBe("light");
    fireEvent.click(screen.getByRole("radio", { name: "系统" }));
    expect(useAppStore.getState().themeMode).toBe("system");
  });

  it("keeps a directly opened settings category visible in the narrow navigation", async () => {
    const mediaMock = vi.mocked(window.matchMedia);
    const previousMediaImplementation = mediaMock.getMockImplementation();
    const scrollDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
    const scrollIntoView = vi.fn();
    mediaMock.mockImplementation((query) => ({
      matches: query === "(max-width: 760px)",
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as MediaQueryList);
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    useAppStore.setState({ settingsTab: "advanced" });

    try {
      render(<SettingsCenter />);

      expect(screen.getByRole("button", { name: "环境" }).getAttribute("aria-current")).toBe("page");
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalledWith({
        behavior: "smooth",
        block: "nearest",
        inline: "center",
      }));
    } finally {
      if (previousMediaImplementation) mediaMock.mockImplementation(previousMediaImplementation);
      if (scrollDescriptor) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", scrollDescriptor);
      else delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    }
  });

  it("edits the user AGENTS.md from personalization and shows instruction sources", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/settings/personalization") && init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        return new Response(JSON.stringify({
          instructions: body.instructions,
          path: "C:\\Users\\ago\\.minicode\\AGENTS.md",
          exists: true,
          max_bytes: 32768,
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.endsWith("/api/settings/personalization")) {
        return new Response(JSON.stringify({
          instructions: "复用现有结构",
          path: "C:\\Users\\ago\\.minicode\\AGENTS.md",
          exists: true,
          max_bytes: 32768,
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.endsWith("/api/guidelines")) {
        return new Response(JSON.stringify({
          blocks: [
            { path: "C:\\Users\\ago\\.minicode\\AGENTS.md", scope: "C:\\Users\\ago", source_kind: "user_memory", label: "User Agent Instructions" },
            { path: "C:\\Desktop\\MiniCode\\AGENTS.md", scope: "C:\\Desktop\\MiniCode", source_kind: "agent_instruction", label: "Agent Instructions" },
          ],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "个性化" }));

    const editor = await screen.findByRole("textbox", { name: "自定义指令" }) as HTMLTextAreaElement;
    expect(editor.value).toBe("复用现有结构");
    expect(screen.getByText("全局指令")).toBeTruthy();
    expect(screen.getByText("项目指令")).toBeTruthy();

    fireEvent.change(editor, { target: { value: "复用现有结构\n统一运行测试" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      const put = fetchMock.mock.calls.find((call) => call[1]?.method === "PUT");
      expect(put).toBeTruthy();
      expect(JSON.parse(String(put?.[1]?.body)).instructions).toBe("复用现有结构\n统一运行测试");
    });
    expect((await screen.findByRole("button", { name: "已保存" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps long-term generation separate from new-task inheritance", async () => {
    useAppStore.setState({
      conversationId: "conv-memory",
      conversations: [{
        id: "conv-memory",
        title: "Memory",
        updatedAt: "2026-08-08T00:00:00.000Z",
        memoryMode: "enabled",
        memoryPolluted: false,
      }],
    });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/settings/personalization")) {
        return new Response(JSON.stringify({
          instructions: "",
          path: "C:\\Users\\ago\\.minicode\\AGENTS.md",
          exists: true,
          max_bytes: 32768,
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.endsWith("/api/guidelines")) {
        return new Response(JSON.stringify({ blocks: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }));

    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "个性化" }));

    const generation = await screen.findByRole("switch", { name: "长期记忆生成" });
    expect(generation.getAttribute("aria-checked")).toBe("true");
    expect(screen.queryByRole("radiogroup", { name: "新任务继承" })).toBeNull();

    fireEvent.click(generation);
    await waitFor(() => expect(awaitCommandResultMock).toHaveBeenCalledWith(expect.objectContaining({
      type: "conversation.memory_mode.set",
      conversation_id: "conv-memory",
      memory_mode: "disabled",
    })));
  });

  it("describes the active page without duplicating composer permission controls", () => {
    render(<SettingsCenter />);

    expect(screen.getByText("设置工作方式、消息跟进、过程展示与内容加载。")).toBeTruthy();
    expect(screen.queryByRole("radiogroup", { name: "默认运行权限" })).toBeNull();
    expect(screen.queryByText("当前模型")).toBeNull();
    expect(screen.queryByRole("radio", { name: "使用自动审核" })).toBeNull();
    expect(screen.queryByRole("radio", { name: "使用规划模式" })).toBeNull();
  });

  it("reuses workspace mode and activity density settings from the app store", () => {
    useAppStore.setState({ allowedRemoteImageDomains: ["images.example.com"] });
    render(<SettingsCenter />);

    expect(screen.queryByRole("button", { name: "对话" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "协作" }));
    expect(useAppStore.getState().appMode).toBe("cowork");
    fireEvent.click(screen.getByRole("button", { name: "代码" }));
    expect(useAppStore.getState().appMode).toBe("code");

    fireEvent.click(screen.getByRole("button", { name: "详细" }));
    expect(useAppStore.getState().viewMode).toBe("verbose");

    fireEvent.click(screen.getByRole("button", { name: "允许" }));
    expect(useAppStore.getState().remoteImagePolicy).toBe("allow");
    fireEvent.click(screen.getByRole("button", { name: "清除列表" }));
    expect(useAppStore.getState().allowedRemoteImageDomains).toEqual([]);
  });

  it("filters the existing shortcut list", () => {
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "快捷键" }));
    const search = screen.getByRole("textbox", { name: "搜索快捷键" });
    fireEvent.change(search, { target: { value: "terminal" } });

    expect(screen.getByText("打开终端")).toBeTruthy();
    expect(screen.queryByText("发送消息")).toBeNull();
  });

  it("persists code sizing and reduced-motion preferences with real consumers", () => {
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "外观" }));

    fireEvent.click(within(screen.getByRole("radiogroup", { name: "代码字号" })).getByRole("radio", { name: "较大" }));
    fireEvent.click(screen.getByRole("switch", { name: "减少动态效果" }));

    expect(useAppStore.getState().codeTextScale).toBe(1.15);
    expect(document.documentElement.style.getPropertyValue("--code-text-scale")).toBe("1.15");
    expect(useAppStore.getState().reducedMotion).toBe(true);
    expect(document.documentElement.getAttribute("data-reduced-motion")).toBe("true");
  });

  it("changes the composer send shortcut and running-turn follow-up behavior", () => {
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "Ctrl/Cmd + Enter" }));
    fireEvent.click(screen.getByRole("button", { name: "引导" }));

    expect(useAppStore.getState().sendShortcut).toBe("mod-enter");
    expect(useAppStore.getState().followUpBehavior).toBe("steer");
  });

  it("records shortcut edits, rejects conflicts, deletes bindings, and restores defaults", () => {
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "快捷键" }));

    fireEvent.click(screen.getByRole("button", { name: "编辑 命令面板" }));
    fireEvent.keyDown(window, { key: "K", code: "KeyK", ctrlKey: true, shiftKey: true });
    expect(useAppStore.getState().shortcutBindings.commandPalette).toBe("Mod+Shift+K");

    fireEvent.click(screen.getByRole("button", { name: "编辑 设置" }));
    fireEvent.keyDown(window, { key: "K", code: "KeyK", ctrlKey: true, shiftKey: true });
    expect(screen.getByRole("alert").textContent).toContain("命令面板");
    expect(useAppStore.getState().shortcutBindings.settings).toBe("Mod+Comma");

    fireEvent.keyDown(window, { key: "Escape", code: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "删除 命令面板 快捷键" }));
    expect(useAppStore.getState().shortcutBindings.commandPalette).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "恢复默认" }));
    expect(useAppStore.getState().shortcutBindings.commandPalette).toBe("Mod+K");
  });

  it("finds setting pages by their actual controls", () => {
    render(<SettingsCenter />);

    fireEvent.change(screen.getByRole("textbox", { name: "搜索设置" }), { target: { value: "下载" } });

    expect(screen.getByRole("heading", { name: "浏览器", level: 2 })).toBeTruthy();
    expect(screen.getByRole("button", { name: "浏览器" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "模型" })).toBeNull();
  });

  it("presents model load failures as a recoverable settings state", async () => {
    fetchLLMSettingsMock.mockRejectedValueOnce(new Error("Bad Gateway"));
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "模型" }));

    expect(await screen.findByText("模型设置暂时不可用")).toBeTruthy();
    expect(screen.getByText("检查后端连接后重试。")).toBeTruthy();
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });

  it("renders the settings destination selected before the lazy panel mounts", () => {
    useAppStore.setState({ settingsTab: "connectors" });

    render(<SettingsCenter />);

    expect(screen.getByRole("region", { name: "MCP" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "常规" })).toBeNull();
  });

  it("uses installed skills through the existing composer selection state", () => {
    useAppStore.setState({
      availableSkills: [{
        name: "code-review",
        display_name: "代码审查",
        description: "审查当前修改",
        source_level: "user",
        path: "C:\\Users\\ago\\.minicode\\skills\\code-review\\SKILL.md",
      }],
    });
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "技能" }));
    expect(screen.getByText("查看任务工作流，并选择用于下一条消息。")).toBeTruthy();
    expect(screen.queryByText("插件是能力包；技能是工作流；MCP 是外部工具服务。")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "使用" }));

    expect(useAppStore.getState().selectedSkills).toEqual([expect.objectContaining({ name: "code-review" })]);
    expect(useAppStore.getState().settingsOpen).toBe(false);
  });

  it("updates the current task memory with the existing conversation command", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/settings/personalization")) {
        return new Response(JSON.stringify({ instructions: "", path: "C:\\Users\\ago\\.minicode\\AGENTS.md", exists: false, max_bytes: 32768 }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.endsWith("/api/guidelines")) {
        return new Response(JSON.stringify({ blocks: [] }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }));
    useAppStore.setState({
      conversationId: "conv-memory",
      conversations: [{ id: "conv-memory", title: "Memory", updatedAt: "2026-08-02T00:00:00.000Z", memoryMode: "disabled" }],
    });
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "个性化" }));
    fireEvent.click(await screen.findByRole("switch", { name: "长期记忆生成" }));

    await waitFor(() => expect(awaitCommandResultMock).toHaveBeenCalledWith({
      type: "conversation.memory_mode.set",
      conversation_id: "conv-memory",
      memory_mode: "enabled",
    }));
  });

  it("requires confirmation before resetting generated memory", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/settings/personalization")) {
        return new Response(JSON.stringify({ instructions: "", path: "C:\\Users\\ago\\.minicode\\AGENTS.md", exists: false, max_bytes: 32768 }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.endsWith("/api/guidelines")) {
        return new Response(JSON.stringify({ blocks: [] }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }));
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "个性化" }));
    fireEvent.click(await screen.findByRole("button", { name: "清除记忆" }));

    const dialog = screen.getByRole("dialog", { name: "清除长期记忆" });
    expect(dialog).toBeTruthy();
    expect(awaitCommandResultMock).not.toHaveBeenCalledWith({ type: "memory.reset", confirmed: true });
    fireEvent.click(within(dialog).getByRole("button", { name: "清除记忆" }));

    await waitFor(() => expect(awaitCommandResultMock).toHaveBeenCalledWith({
      type: "memory.reset",
      confirmed: true,
    }));
  });

  it("shows isolated external context and explicitly re-enables task memory", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/settings/personalization")) {
        return new Response(JSON.stringify({ instructions: "", path: "C:\\Users\\ago\\.minicode\\AGENTS.md", exists: false, max_bytes: 32768 }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.endsWith("/api/guidelines")) {
        return new Response(JSON.stringify({ blocks: [] }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    }));
    useAppStore.setState({
      conversationId: "conv-polluted",
      conversations: [{
        id: "conv-polluted",
        title: "External context",
        updatedAt: "2026-08-07T00:00:00.000Z",
        memoryMode: "polluted",
        memoryPolluted: true,
        memoryPollutionSources: ["web_search", "mcp__github__search_issues"],
      }],
    });
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "个性化" }));
    expect(await screen.findByText("外部上下文已隔离")).toBeTruthy();
    expect(screen.getByText("联网搜索")).toBeTruthy();
    expect(screen.getAllByText("MCP").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "重新启用" }));

    const dialog = screen.getByRole("dialog", { name: "重新启用任务记忆" });
    fireEvent.click(within(dialog).getByRole("button", { name: "重新启用" }));
    await waitFor(() => expect(awaitCommandResultMock).toHaveBeenCalledWith({
      type: "conversation.memory_mode.set",
      conversation_id: "conv-polluted",
      memory_mode: "enabled",
    }));
  });

  it("opens the existing diagnostics panel from environment settings", async () => {
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "环境" }));
    fireEvent.click(screen.getByRole("button", { name: "打开运行状态" }));

    await waitFor(() => expect(useAppStore.getState()).toMatchObject({
      rightStackTab: "diagnostics",
      rightPanelOpen: true,
      settingsOpen: false,
    }));
  });

  it("exposes existing browser, Git, and archived task capabilities in navigation", () => {
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "Git 与工作树" }));
    expect(screen.getByLabelText("Git 与工作树工具")).toBeTruthy();
    expect(screen.getByRole("button", { name: "已归档任务" })).toBeTruthy();
  });

  it("updates the existing desktop browser download policy", async () => {
    desktopModeMock.mockReturnValue(true);
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "浏览器" }));
    const downloadPolicy = await screen.findByLabelText("浏览器下载策略");
    fireEvent.change(downloadPolicy, { target: { value: "ask" } });

    await waitFor(() => expect(embeddedBrowserSetSettingsMock).toHaveBeenCalledWith({ downloadPolicy: "ask" }));
    await waitFor(() => expect((downloadPolicy as HTMLSelectElement).value).toBe("ask"));
  });

  it("keeps environment variable values when the backend rejects them", async () => {
    awaitCommandResultMock.mockResolvedValueOnce({ type: "command.result", command: "env.set", level: "error", message: "invalid", data: {} });
    useAppStore.setState({ envVars: [] });
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "环境" }));
    fireEvent.change(screen.getByPlaceholderText("变量名"), { target: { value: "DEMO_TOKEN" } });
    fireEvent.change(screen.getByPlaceholderText("变量值"), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "添加" }));

    await waitFor(() => expect(awaitCommandResultMock).toHaveBeenCalledWith(expect.objectContaining({
      type: "env.set",
      name: "DEMO_TOKEN",
      value: "secret",
    })));
    expect((screen.getByPlaceholderText("变量名") as HTMLInputElement).value).toBe("DEMO_TOKEN");
    expect((screen.getByPlaceholderText("变量值") as HTMLInputElement).value).toBe("secret");
  });

  it("names environment delete actions with their target variable", () => {
    useAppStore.setState({ envVars: [{ name: "DEMO_TOKEN", description: "Demo", scope: "global" }] });
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "环境" }));

    expect(screen.getByRole("button", { name: "删除环境变量 DEMO_TOKEN" })).toBeTruthy();
  });

  it("restores the browser download policy when desktop saving fails", async () => {
    desktopModeMock.mockReturnValue(true);
    embeddedBrowserSetSettingsMock.mockRejectedValueOnce(new Error("disk failed"));
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "浏览器" }));
    const downloadPolicy = await screen.findByLabelText("浏览器下载策略");
    fireEvent.change(downloadPolicy, { target: { value: "ask" } });

    await waitFor(() => expect((downloadPolicy as HTMLSelectElement).value).toBe("block"));
  });

  it("opens the existing browser panel from browser settings", async () => {
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "浏览器" }));
    fireEvent.click(screen.getByRole("button", { name: "打开浏览器" }));

    await waitFor(() => expect(useAppStore.getState()).toMatchObject({
      rightStackTab: "browser",
      rightPanelOpen: true,
      settingsOpen: false,
    }));
  });

  it("reopens an already selected browser panel in the compact workbench", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 900 });
    useAppStore.setState({ rightStackTab: "browser", rightPanelOpen: true });
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "浏览器" }));
    fireEvent.click(screen.getByRole("button", { name: "打开浏览器" }));

    expect(useAppStore.getState().rightPanelOpen).toBe(false);
    await waitFor(() => expect(useAppStore.getState()).toMatchObject({
      rightStackTab: "browser",
      rightPanelOpen: true,
      settingsOpen: false,
    }));
  });

  it("restores archived tasks through the existing conversation command", async () => {
    useAppStore.setState({
      conversations: [{
        id: "conv-archived",
        title: "旧任务",
        updatedAt: "2026-08-01T00:00:00.000Z",
        workspaceRoot: "C:\\Desktop\\MiniCode",
        archived: true,
      }],
    });
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "已归档任务" }));
    fireEvent.click(screen.getByRole("button", { name: "恢复 旧任务" }));

    await waitFor(() => expect(awaitCommandResultMock).toHaveBeenCalledWith({
      type: "conversation.unarchive",
      conversation_id: "conv-archived",
      archived: false,
    }));
  });

  it("omits redundant navigation and section subtitles", async () => {
    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    expect(screen.queryByText("Approvals and status")).toBeNull();
    expect(screen.queryByText("Control how tools and edits are approved")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "环境" }));
    expect(screen.queryByText("Runtime and environment")).toBeNull();
    expect(screen.queryByText("Encrypted local vault for secrets injected into tool execution")).toBeNull();
  });

  it("does not expose the removed prompt identity switch", async () => {
    fetchLLMSettingsMock.mockResolvedValueOnce({
      provider: "openai",
      active_model: "gpt-5",
      openai: {
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        available_models: ["gpt-5"],
        wire_api: "responses",
      },
    });
    useAppStore.setState({
      currentModel: "gpt-5",
      currentProvider: "openai",
      availableModels: ["gpt-5"],
    });

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    expect(screen.queryByText("Prompt Style")).toBeNull();
  });

  it("renders as a standalone settings page and returns to the workspace", async () => {
    const { container } = render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());

    expect(screen.getByRole("main", { name: "设置" })).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "设置" })).toBeNull();
    expect(container.querySelector(".overlay-backdrop")).toBeNull();
    expect(useAppStore.getState().settingsOpen).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "返回应用" }));

    expect(useAppStore.getState().settingsOpen).toBe(false);
  });

  it("hydrates the Models tab from saved provider settings", async () => {
    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 DeepSeek" }));

    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "runtime.capabilities.inspect",
      source: "settings.provider",
    }, { silent: true });
    expect(screen.getByDisplayValue("https://api.deepseek.com/v1")).toBeTruthy();
    expect(screen.getByDisplayValue("deepseek-v4-flash")).toBeTruthy();
    const apiKeyInput = screen.getByLabelText("API 密钥") as HTMLInputElement;
    expect(apiKeyInput.type).toBe("text");
    expect(apiKeyInput.value).toBe("sk-deepseek-visible");
    expect(apiKeyInput.placeholder).toBe("API 密钥");
    expect(screen.queryByText("This local key will be used for the selected endpoint.")).toBeNull();
    expect(screen.queryByPlaceholderText("Saved key in use. Paste a new key to replace it.")).toBeNull();
    expect(screen.queryByText("Saved locally for this endpoint. Leave empty to keep it, or paste a replacement.")).toBeNull();
    expect(screen.queryByText("Stored locally and currently in use.")).toBeNull();
    expect(screen.queryByDisplayValue("deepseek-chat")).toBeNull();
    const format = screen.getByLabelText("API 格式") as HTMLSelectElement;
    expect(format.value).toBe("chat");
    expect(Array.from(format.options).map((option) => option.text)).toContain("OpenAI Responses");
    expect(Array.from(format.options).map((option) => option.text)).toContain("Anthropic Messages");
    expect(screen.queryByLabelText("推理强度")).toBeNull();
    expect(screen.queryByText("模型能力")).toBeNull();

    fireEvent.change(format, { target: { value: "anthropic" } });
    expect(screen.getByDisplayValue("https://api.deepseek.com/v1")).toBeTruthy();
  });

  it("keeps provider-declared reasoning metadata out of the provider form", async () => {
    const section = {
      display_name: "Provider Catalog",
      has_api_key: true,
      api_key: "",
      base_url: "https://gateway.example/v1",
      model: "provider-model",
      available_models: ["provider-model"],
      wire_api: "responses",
      reasoning_effort: "focused",
      configured_reasoning_effort: "focused",
      effective_reasoning_effort: "focused",
      reasoning_effort_supported: true,
      reasoning_effort_levels: ["low", "focused", "ultra"] as const,
      model_metadata: {
        "provider-model": {
          context_window: 128000,
          reasoning_effort_levels: ["low", "focused", "ultra"] as const,
          source: "provider",
        },
      },
      context_window: 128000,
      context_window_source: "provider",
      context_window_verified: true,
    };
    fetchLLMSettingsMock.mockResolvedValueOnce({
      provider: "custom",
      active_model: "provider-model",
      custom: section,
      provider_history: [{
        ...section,
        provider: "custom",
        provider_id: "custom",
        updated_at: 30,
      }],
    });

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 Provider Catalog" }));

    expect(screen.queryByLabelText("推理强度")).toBeNull();
    expect(screen.getByDisplayValue("128000")).toBeTruthy();
    expect(screen.queryByText("模型能力")).toBeNull();
  });

  it("keeps the runtime capability matrix out of the provider form", async () => {
    useAppStore.setState({
      runtimeCapabilities: {
        provider_capabilities: {
          provider: "custom",
          model: "deepseek-v4-flash",
          wire_api: "chat",
          streaming: true,
          tool_calling: false,
          parallel_tool_calls: false,
          json_mode: true,
          vision: false,
          native_pdf: false,
          confidence: "known",
          limitations: ["tool_calling_disabled_for_test"],
        },
      },
    });

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 DeepSeek" }));

    expect(screen.queryByText("Provider Capabilities")).toBeNull();
    expect(screen.queryByText("Tool calling")).toBeNull();
  });

  it("restores a provider from recent configuration history", async () => {
    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 OpenRouter" }));

    expect(screen.getByDisplayValue("https://openrouter.ai/api/v1")).toBeTruthy();
    expect(screen.getByDisplayValue("anthropic/claude-sonnet-4")).toBeTruthy();
    expect((screen.getByLabelText("API 密钥") as HTMLInputElement).value).toBe("sk-openrouter-visible");
    expect(screen.getAllByText("OpenRouter").length).toBeGreaterThan(0);
  });

  it("uses editable display names for custom provider cards", async () => {
    fetchLLMSettingsMock.mockResolvedValueOnce({
      provider: "custom",
      active_model: "gpt-5",
      custom: {
        display_name: "api.bbe.to",
        has_api_key: true,
        api_key: "",
        base_url: "https://api.bbe.to/v1",
        model: "gpt-5",
        available_models: ["gpt-5"],
        wire_api: "responses",
      },
      provider_history: [{
        provider: "custom",
        provider_id: "custom_openai",
        display_name: "api.bbe.to",
        has_api_key: true,
        api_key: "",
        base_url: "https://api.bbe.to/v1",
        model: "gpt-5",
        available_models: ["gpt-5"],
        wire_api: "responses",
        updated_at: 10,
      }],
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/llm/settings") && init?.method === "PUT") {
        return new Response(JSON.stringify({
          provider: "custom",
          active_model: "gpt-5",
          custom: {
            display_name: "Work gateway",
            has_api_key: true,
            api_key: "",
            base_url: "https://api.bbe.to/v1",
            model: "gpt-5",
            available_models: ["gpt-5"],
            wire_api: "responses",
          },
          provider_history: [],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));

    expect(screen.getByRole("button", { name: "api.bbe.to 已启用" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "编辑 api.bbe.to" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "编辑 自定义" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "编辑 api.bbe.to" }));
    const displayNameInput = screen.getByLabelText("提供商显示名称") as HTMLInputElement;
    expect(displayNameInput.value).toBe("api.bbe.to");

    fireEvent.change(displayNameInput, { target: { value: "Work gateway" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    expect(body.custom.display_name).toBe("Work gateway");
    expect(body.custom.api_key).toBeUndefined();
    expect(body.custom.wire_api).toBe("responses");
    expect(body.custom.prompt_cache_retention).toBe("");
  });

  it("shows one provider card when one endpoint has multiple API formats", async () => {
    fetchLLMSettingsMock.mockResolvedValueOnce({
      provider: "custom",
      active_model: "gpt-5.5",
      custom: {
        display_name: "api.bbe.to",
        has_api_key: true,
        api_key: "",
        base_url: "https://api.bbe.to/v1",
        model: "gpt-5.5",
        available_models: ["gpt-5.5"],
        wire_api: "chat",
      },
      provider_history: [
        {
          provider: "custom",
          provider_id: "custom_openai",
          display_name: "api.bbe.to",
          has_api_key: true,
          api_key: "",
          base_url: "https://api.bbe.to/v1",
          model: "gpt-5.5",
          available_models: ["gpt-5.5"],
          wire_api: "chat",
          updated_at: 20,
        },
        {
          provider: "custom",
          provider_id: "custom_openai",
          display_name: "api.bbe.to",
          has_api_key: true,
          api_key: "",
          base_url: "https://api.bbe.to/v1",
          model: "gpt-5.5",
          available_models: ["gpt-5.5"],
          wire_api: "responses",
          updated_at: 10,
        },
      ],
    });

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));

    const activeCard = screen.getByRole("button", { name: "api.bbe.to 已启用" }).parentElement?.parentElement as HTMLElement;
    expect(activeCard.dataset.active).toBe("true");
    expect(activeCard.classList.contains("provider-card")).toBe(true);
    expect(screen.getAllByRole("button", { name: "编辑 api.bbe.to" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "使用 api.bbe.to" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "编辑 api.bbe.to" }));
    expect((screen.getByLabelText("API 格式") as HTMLSelectElement).value).toBe("chat");
  });

  it("keeps explicitly saved Chat and Messages profiles distinct", async () => {
    fetchLLMSettingsMock.mockResolvedValueOnce({
      provider: "custom",
      active_model: "deepseek-v4-pro",
      custom: {
        has_api_key: true,
        api_key: "test-deepseek-key",
        base_url: "https://api.deepseek.com/anthropic",
        model: "deepseek-v4-pro",
        available_models: ["deepseek-v4-pro", "deepseek-v4-flash"],
        wire_api: "anthropic",
      },
      provider_history: [
        {
          provider: "custom",
          provider_id: "deepseek",
          has_api_key: true,
          api_key: "test-deepseek-key",
          base_url: "https://api.deepseek.com/anthropic",
          model: "deepseek-v4-pro",
          available_models: ["deepseek-v4-pro", "deepseek-v4-flash"],
          wire_api: "anthropic",
          updated_at: 20,
        },
        {
          provider: "custom",
          provider_id: "deepseek",
          has_api_key: true,
          api_key: "test-deepseek-key",
          base_url: "https://api.deepseek.com/v1",
          model: "deepseek-v4-flash",
          available_models: ["deepseek-v4-pro", "deepseek-v4-flash"],
          wire_api: "chat",
          updated_at: 10,
        },
      ],
    });

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));

    expect(screen.getAllByRole("button", { name: "编辑 api.deepseek.com" })).toHaveLength(2);
    expect(document.body.textContent).toContain("deepseek-v4-pro");
    expect(screen.queryByText("claude-sonnet-4-6")).toBeNull();
  });

  it("does not infer an API format from a custom model id", async () => {
    fetchLLMSettingsMock.mockResolvedValueOnce({
      provider: "custom",
      active_model: "gpt-5.5",
      custom: {
        display_name: "api.bbe.to",
        has_api_key: true,
        api_key: "",
        base_url: "https://api.bbe.to/v1",
        model: "gpt-5.5",
        available_models: ["gpt-5.5"],
        wire_api: "chat",
      },
      provider_history: [
        {
          provider: "custom",
          provider_id: "custom",
          display_name: "api.bbe.to",
          has_api_key: true,
          api_key: "",
          base_url: "https://api.bbe.to/v1",
          model: "gpt-5.5",
          available_models: ["gpt-5.5"],
          wire_api: "chat",
          updated_at: 20,
        },
      ],
    });

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 api.bbe.to" }));

    expect((screen.getByLabelText("API 格式") as HTMLSelectElement).value).toBe("chat");
    expect(screen.queryByText(/GPT 类模型使用 OpenAI Responses/)).toBeNull();
    expect(Array.from((screen.getByLabelText("API 格式") as HTMLSelectElement).options).map((option) => option.text)).toContain("OpenAI Responses");
  });

  it("starts with an empty provider list and adds a provider profile", async () => {
    fetchLLMSettingsMock.mockResolvedValueOnce({
      provider: "openai",
      active_model: "",
      provider_history: [],
      openai: {},
      custom: {},
      anthropic: {},
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/llm/settings") && init?.method === "PUT") {
        return new Response(JSON.stringify({
          provider: "openai",
          active_model: "",
          provider_history: [{
            provider: "custom",
            provider_id: "custom_openai",
            display_name: "Work gateway",
            base_url: "https://api.bbe.to/v1",
            model: "gpt-5",
            available_models: ["gpt-5"],
            wire_api: "responses",
            updated_at: 10,
          }],
          custom: {
            display_name: "Work gateway",
            base_url: "https://api.bbe.to/v1",
            model: "gpt-5",
            available_models: ["gpt-5"],
            wire_api: "responses",
          },
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));

    expect(screen.getByText("尚未配置提供商")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "编辑 OpenAI" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "添加提供商" }));
    const discoverButton = screen.getByRole("button", { name: "获取模型列表" }) as HTMLButtonElement;
    const saveButton = screen.getByRole("button", { name: "保存" }) as HTMLButtonElement;
    expect(discoverButton.disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "检查连接" })).toBeNull();
    expect(saveButton.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("提供商显示名称"), { target: { value: "Work gateway" } });
    fireEvent.change(screen.getByPlaceholderText("https://api.example.com/v1"), { target: { value: "https://api.bbe.to/v1" } });
    expect(discoverButton.disabled).toBe(false);
    expect(saveButton.disabled).toBe(true);
    expect(screen.getByText("尚未添加模型")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "添加模型" }));
    const modelIdInput = screen.getByPlaceholderText("实际请求模型 ID");
    fireEvent.change(modelIdInput, { target: { value: "gpt-5" } });
    fireEvent.blur(modelIdInput);
    expect((screen.getByRole("button", { name: "鉴权" }) as HTMLButtonElement).disabled).toBe(false);
    expect(saveButton.disabled).toBe(false);
    fireEvent.change(screen.getByLabelText("API 格式"), { target: { value: "responses" } });
    expect(screen.queryByLabelText("推理强度")).toBeNull();
    expect(screen.queryByText("模型能力")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    expect(body.provider).toBeUndefined();
    expect(body.custom.display_name).toBe("Work gateway");
    expect(body.custom.base_url).toBe("https://api.bbe.to/v1");
    expect(body.custom.model).toBe("gpt-5");
    expect(body.custom.model_labels).toEqual({ "gpt-5": "gpt-5" });
    expect(body.custom.wire_api).toBe("responses");
    expect(body.custom.reasoning_effort).toBe("");
    expect(body.custom.prompt_cache_retention).toBe("");
    expect(body.openai).toBeUndefined();
    expect(body.anthropic).toBeUndefined();
  });

  it("removes a saved provider configuration from history", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/llm/provider-history") && init?.method === "DELETE") {
        return new Response(JSON.stringify({
          provider: "custom",
          active_model: "deepseek-v4-flash",
          custom: {
            has_api_key: true,
            api_key: "",
            base_url: "https://api.deepseek.com/v1",
            model: "deepseek-v4-flash",
            available_models: ["deepseek-v4-flash"],
            wire_api: "chat",
          },
          provider_history: [],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));

    fireEvent.click(screen.getByRole("button", { name: /删除已保存的提供商 OpenRouter/i }));
    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/llm/provider-history");
    expect(init?.method).toBe("DELETE");
    expect(JSON.parse(String(init?.body))).toMatchObject({
      confirm_sensitive_change: true,
      provider: "custom",
      provider_id: "openrouter",
      base_url: "https://openrouter.ai/api/v1",
      wire_api: "chat",
      clear_api_key: true,
    });
    await waitFor(() => expect(screen.queryByRole("button", { name: /删除已保存的提供商 OpenRouter/i })).toBeNull());
  });

  it("shows explicit API formats for OpenAI and fixed Anthropic Messages", async () => {
    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));

    fireEvent.click(screen.getByRole("button", { name: "编辑 OpenAI" }));
    const openAiFormat = screen.getByLabelText("API 格式") as HTMLSelectElement;
    expect(openAiFormat.value).toBe("responses");
    expect(Array.from(openAiFormat.options).map((option) => option.text)).toEqual([
      "OpenAI Chat Completions",
      "OpenAI Responses",
    ]);
    expect(screen.getByText("Responses 提示词缓存")).toBeTruthy();
    expect((screen.getByLabelText("提示词缓存") as HTMLSelectElement).value).toBe("off");

    fireEvent.change(openAiFormat, { target: { value: "chat" } });
    expect(openAiFormat.value).toBe("chat");
    expect(screen.queryByText("Responses 提示词缓存")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "返回提供商列表" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 Anthropic" }));
    expect(screen.getByText("Anthropic Messages")).toBeTruthy();
    expect(screen.queryByLabelText("API 格式")).toBeNull();
  });

  it("saves the selected OpenAI API format", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/llm/settings") && init?.method === "PUT") {
        return new Response(JSON.stringify({
          provider: "openai",
          active_model: "gpt-5",
          openai: {
            base_url: "https://api.openai.com/v1",
            model: "gpt-5",
            available_models: ["gpt-5"],
            wire_api: "chat",
          },
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 OpenAI" }));
    fireEvent.change(screen.getByLabelText("API 格式"), { target: { value: "chat" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    expect(body.openai.wire_api).toBe("chat");
    expect(sendClientCommand).not.toHaveBeenCalledWith({
      type: "runtime.capabilities.inspect",
      source: "settings.provider.save",
    }, { silent: true });
  });

  it("discovers models without running the auth check", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/llm/models/refresh")) {
        return new Response(JSON.stringify({
          provider: "custom",
          provider_id: "deepseek",
          models: ["deepseek-v4-pro", "deepseek-v4-flash"],
          selected_model: "deepseek-v4-pro",
          source: "live",
          source_message: "Fetched models from provider.",
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 DeepSeek" }));
    fireEvent.click(screen.getByRole("button", { name: "获取模型列表" }));

    await waitFor(() => expect(screen.getByText(/已获取 2 个候选模型/)).toBeTruthy());
    expect(screen.queryByDisplayValue("deepseek-v4-pro")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "添加模型" }));
    const discoveredModelSelect = screen.getByLabelText("__draft_model_1 实际请求模型") as HTMLSelectElement;
    expect(discoveredModelSelect.tagName).toBe("SELECT");
    fireEvent.change(discoveredModelSelect, { target: { value: "deepseek-v4-pro" } });
    expect(screen.getByDisplayValue("deepseek-v4-pro")).toBeTruthy();
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/api/llm/models/refresh");
    expect(useAppStore.getState().availableModels).toEqual(["deepseek-v4-flash"]);
  });

  it("waits for an explicit model-list request and keeps discovered models as candidates", async () => {
    const initialPayload: LLMSettingsPayload = {
      provider: "custom",
      active_model: "",
      custom: {
        display_name: "Local gateway",
        has_api_key: true,
        api_key: "test-local-key",
        base_url: "https://gateway.example/v1",
        model: "",
        available_models: [],
        wire_api: "chat",
      },
      provider_history: [{
        provider: "custom",
        provider_id: "local-gateway",
        display_name: "Local gateway",
        has_api_key: true,
        api_key: "test-local-key",
        base_url: "https://gateway.example/v1",
        model: "",
        available_models: [],
        wire_api: "chat",
        updated_at: 10,
      }],
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      expect(url).toContain("/api/llm/models/refresh");
      const body = JSON.parse(String(init?.body));
      expect(body.custom.api_key).toBe("test-local-key");
      return new Response(JSON.stringify({
        provider: "custom",
        provider_id: "local-gateway",
        models: ["model-a", "model-b"],
        selected_model: "model-a",
        source: "live",
        source_message: "Fetched models from provider.",
      }), { status: 200, headers: { "content-type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ProviderTab
        selectedProvider="custom"
        settingsPayload={initialPayload}
        settingsPayloadRef={{ current: initialPayload }}
        onProviderChange={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "编辑 Local gateway" }));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByText("尚未添加模型")).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: /实际请求模型/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "获取模型列表" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(screen.getByText("尚未添加模型")).toBeTruthy();
    expect(screen.queryByDisplayValue("model-a")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "添加模型" }));
    const modelSelect = screen.getByLabelText("__draft_model_1 实际请求模型") as HTMLSelectElement;
    expect(modelSelect.tagName).toBe("SELECT");
    fireEvent.click(screen.getByRole("button", { name: /__draft_model_1 实际请求模型，当前/ }));
    expect(screen.getByRole("option", { name: "model-a" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "model-b" })).toBeTruthy();
  });

  it("discovers models for the provider card being edited instead of the active custom provider", async () => {
    const initialPayload: LLMSettingsPayload = {
      provider: "custom",
      active_model: "gpt-5.4",
      custom: {
        display_name: "Lucen",
        has_api_key: true,
        api_key: "test-lucen-key",
        base_url: "https://lucen.cc/v1",
        model: "gpt-5.4",
        available_models: ["gpt-5.4"],
        wire_api: "responses",
      },
      provider_history: [
        {
          provider: "custom",
          provider_id: "lucen",
          display_name: "Lucen",
          has_api_key: true,
          api_key: "test-lucen-key",
          base_url: "https://lucen.cc/v1",
          model: "gpt-5.4",
          available_models: ["gpt-5.4"],
          wire_api: "responses",
          updated_at: 20,
        },
        {
          provider: "custom",
          provider_id: "custom_openai",
          display_name: "bbe.to",
          has_api_key: true,
          api_key: "test-bbe-key",
          base_url: "https://api.bbe.to/v1",
          model: "gpt-5.5",
          available_models: ["gpt-5.5"],
          wire_api: "chat",
          updated_at: 10,
        },
      ],
    };
    const activeLucenPayload: LLMSettingsPayload = {
      provider: "custom",
      active_model: "gpt-5.4",
      custom: {
        display_name: "Lucen",
        has_api_key: true,
        api_key: "test-lucen-key",
        base_url: "https://lucen.cc/v1",
        model: "gpt-5.4",
        available_models: ["gpt-5.4"],
        wire_api: "responses",
      },
      provider_history: [],
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/llm/models/refresh")) {
        const body = JSON.parse(String(init?.body));
        expect(body.provider).toBe("custom");
        expect(body.custom.base_url).toBe("https://api.bbe.to/v1");
        expect(body.custom.model).toBe("gpt-5.5");
        return new Response(JSON.stringify({
          provider: "custom",
          provider_id: "custom_openai",
          models: ["mimo-v2.5-pro", "grok-4.3", "gpt-5.5"],
          selected_model: "gpt-5.5",
          source: "live",
          source_message: "Fetched models from provider.",
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const settingsPayloadRef = { current: initialPayload };
    const { rerender } = render(
      <ProviderTab
        selectedProvider="custom"
        settingsPayload={initialPayload}
        settingsPayloadRef={settingsPayloadRef}
        onProviderChange={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "编辑 bbe.to" }));
    expect(screen.getByDisplayValue("https://api.bbe.to/v1")).toBeTruthy();

    settingsPayloadRef.current = activeLucenPayload;
    rerender(
      <ProviderTab
        selectedProvider="custom"
        settingsPayload={activeLucenPayload}
        settingsPayloadRef={settingsPayloadRef}
        onProviderChange={vi.fn()}
      />,
    );
    expect(screen.getByDisplayValue("https://api.bbe.to/v1")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "获取模型列表" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /实际请求模型，当前/ }));
    expect(screen.getByRole("option", { name: "mimo-v2.5-pro" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "grok-4.3" })).toBeTruthy();
  });

  it("keeps the configured manual model list selectable when discovery is unavailable", async () => {
    const initialPayload: LLMSettingsPayload = {
      provider: "custom",
      active_model: "mimo-v2.5-pro",
      custom: {
        display_name: "bbe.to",
        has_api_key: true,
        api_key: "test-bbe-key",
        base_url: "https://api.bbe.to/v1",
        model: "mimo-v2.5-pro",
        available_models: ["gpt-5.5", "gpt-5.4", "mimo-v2.5-pro"],
        wire_api: "chat",
      },
      provider_history: [{
        provider: "custom",
        provider_id: "custom",
        display_name: "bbe.to",
        has_api_key: true,
        api_key: "test-bbe-key",
        base_url: "https://api.bbe.to/v1",
        model: "mimo-v2.5-pro",
        available_models: ["gpt-5.5", "gpt-5.4", "mimo-v2.5-pro"],
        wire_api: "chat",
        updated_at: 10,
      }],
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/llm/models/refresh")) {
        return new Response(JSON.stringify({
          provider: "custom",
          provider_id: "custom",
          models: ["gpt-5.5", "gpt-5.4", "mimo-v2.5-pro"],
          selected_model: "mimo-v2.5-pro",
          source: "manual",
          source_message: "Live model refresh failed or returned no models, keeping manual model list.",
          failure_kind: "model_list_empty",
          retryable: true,
          message: "The provider model-list endpoint returned no models.",
          hint: "Keep the manually configured model or retry model discovery later.",
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ProviderTab
        selectedProvider="custom"
        settingsPayload={initialPayload}
        settingsPayloadRef={{ current: initialPayload }}
        onProviderChange={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "编辑 bbe.to" }));

    expect(screen.getByDisplayValue("mimo-v2.5-pro")).toBeTruthy();
    expect(screen.getByDisplayValue("gpt-5.5")).toBeTruthy();
    expect(screen.getByDisplayValue("gpt-5.4")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "获取模型列表" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.getByText(/Provider 返回了空模型列表/)).toBeTruthy();
    expect(screen.getByText(/已保留手动输入的模型/)).toBeTruthy();
    expect(screen.getByDisplayValue("gpt-5.5")).toBeTruthy();
    expect(screen.getByDisplayValue("gpt-5.4")).toBeTruthy();
  });

  it("keeps connection checks scoped to each configured model", async () => {
    const tokenRhythmSection = {
      display_name: "TokenRhythm",
      has_api_key: true,
      api_key: "",
      base_url: "https://tokenrhythm.studio/v1",
      model: "glm-5.2",
      available_models: ["glm-5.2"],
      wire_api: "chat" as const,
      updated_at: 10,
    };
    const payload: LLMSettingsPayload = {
      provider: "custom",
      active_model: "glm-5.2",
      custom: tokenRhythmSection,
      provider_history: [{
        provider: "custom",
        provider_id: "tokenrhythm",
        ...tokenRhythmSection,
      }],
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/llm/check")) {
        return new Response(JSON.stringify({
          ok: false,
          provider: "custom",
          provider_id: "custom",
          base_url: "https://tokenrhythm.studio/v1",
          model: "glm-5.2",
          wire_api: "chat",
          has_api_key: true,
          status_code: 503,
          model_discovery_ok: true,
          generation_ok: false,
          failure_kind: "provider_unavailable",
          retryable: true,
          message: "SERVICE_BUSY",
          hint: "The model list endpoint accepted the current credentials.",
          models: ["glm-5.2"],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <ProviderTab
        selectedProvider="custom"
        settingsPayload={payload}
        settingsPayloadRef={{ current: payload }}
        onProviderChange={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "编辑 TokenRhythm" }));
    expect(screen.queryByRole("button", { name: "检查连接" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "鉴权" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("/api/llm/check");
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });

  it("uses and persists an explicit direct network path for one provider profile", async () => {
    const tokenRhythmSection = {
      display_name: "TokenRhythm",
      has_api_key: true,
      api_key: "",
      base_url: "https://tokenrhythm.studio/v1",
      model: "glm-5.2",
      available_models: ["glm-5.2"],
      wire_api: "chat" as const,
      proxy_mode: "inherit" as const,
    };
    const initialPayload: LLMSettingsPayload = {
      provider: "custom",
      active_model: "glm-5.2",
      custom: tokenRhythmSection,
      provider_history: [{
        provider: "custom",
        provider_id: "tokenrhythm",
        ...tokenRhythmSection,
        updated_at: 10,
      }],
    };
    const savedSection = {
      ...tokenRhythmSection,
      proxy_mode: "direct" as const,
    };
    fetchLLMSettingsMock.mockResolvedValueOnce(initialPayload);
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/llm/check")) {
        return new Response(JSON.stringify({
          ok: true,
          provider: "custom",
          provider_id: "custom",
          base_url: savedSection.base_url,
          model: savedSection.model,
          wire_api: savedSection.wire_api,
          proxy_mode: "direct",
          has_api_key: true,
          model_discovery_ok: true,
          generation_ok: true,
          message: "Provider connection and a small generation check succeeded.",
          models: [savedSection.model],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes("/api/llm/settings") && init?.method === "PUT") {
        return new Response(JSON.stringify({
          provider: "custom",
          active_model: savedSection.model,
          custom: savedSection,
          provider_history: [{
            provider: "custom",
            provider_id: "tokenrhythm",
            ...savedSection,
            updated_at: 20,
          }],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 TokenRhythm" }));

    const networkSelect = screen.getByLabelText("网络连接") as HTMLSelectElement;
    expect(networkSelect.value).toBe("inherit");
    fireEvent.change(networkSelect, { target: { value: "direct" } });
    expect(screen.getByText(/忽略 MiniCode 与系统进程代理/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "鉴权" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/api/llm/check"))).toBe(true));
    const checkCall = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/llm/check"));
    expect(JSON.parse(String(checkCall?.[1]?.body)).custom.proxy_mode).toBe("direct");
    expect((screen.getByLabelText("网络连接") as HTMLSelectElement).value).toBe("direct");
    expect(await screen.findByRole("button", { name: "已通过" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/api/llm/settings"))).toBe(true));
    const saveCall = fetchMock.mock.calls.find(([url]) => String(url).includes("/api/llm/settings"));
    expect(JSON.parse(String(saveCall?.[1]?.body)).custom.proxy_mode).toBe("direct");

    fireEvent.click(await screen.findByRole("button", { name: "编辑 TokenRhythm" }));
    fireEvent.click(screen.getByText("高级设置"));
    expect((screen.getByLabelText("网络连接") as HTMLSelectElement).value).toBe("direct");
  });

  it("syncs the active session when using a provider card", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/llm/settings") && init?.method === "PUT") {
        return new Response(JSON.stringify({
          provider: "openai",
          active_model: "gpt-5",
          openai: {
            display_name: "OpenAI",
            has_api_key: true,
            api_key: "test-openai-key",
            base_url: "https://api.openai.com/v1",
            model: "gpt-5",
            available_models: ["gpt-5"],
            wire_api: "responses",
          },
          provider_history: [
            {
              provider: "openai",
              provider_id: "openai_official",
              display_name: "OpenAI",
              has_api_key: true,
              api_key: "test-openai-key",
              base_url: "https://api.openai.com/v1",
              model: "gpt-5",
              available_models: ["gpt-5"],
              wire_api: "responses",
              updated_at: 20,
            },
          ],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /模型/ }));
    fireEvent.click(screen.getByRole("button", { name: "使用 OpenAI" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "llm.config.set",
      provider: "openai",
      source: "settings.provider.activate",
    }, { silent: true });
    expect(sendClientCommand).not.toHaveBeenCalledWith({
      type: "runtime.capabilities.inspect",
      source: "settings.provider.save",
    }, { silent: true });
  });

  it("honors a scheduler destination selected before settings is opened", async () => {
    useAppStore.setState({
      settingsOpen: false,
      settingsTab: "scheduler",
      scheduledTasks: [],
      conversationId: "conv-memory",
      conversations: [{ id: "conv-memory", title: "Memory", updatedAt: "2026-08-02T00:00:00.000Z" }],
    });
    render(<SettingsCenter />);

    useAppStore.setState({ settingsOpen: true });

    await waitFor(() => expect(screen.getByText("暂无定时任务")).toBeTruthy());
    expect(fetchLLMSettingsMock).not.toHaveBeenCalled();
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "scheduler.list",
      owner_conversation_id: "conv-memory",
      workspace_root: undefined,
    }, { silent: true });
  });

  it("shows and saves feature flag overrides from the settings center", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/settings/feature-flags") && (!init?.method || init.method === "GET")) {
        return new Response(JSON.stringify({
          flags: [
            {
              name: "global_search",
              default: true,
              enabled: true,
              source: "default",
              override: null,
              env_var: "MINICODE_FEATURE_GLOBAL_SEARCH",
              env_override: null,
            },
          ],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes("/api/settings/feature-flags") && init?.method === "PUT") {
        return new Response(JSON.stringify({
          flags: [
            {
              name: "global_search",
              default: true,
              enabled: false,
              source: "settings",
              override: false,
              env_var: "MINICODE_FEATURE_GLOBAL_SEARCH",
              env_override: null,
            },
          ],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /实验功能/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /^界面/ })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /^界面/ }));
    await waitFor(() => expect(screen.getByText("全局搜索")).toBeTruthy());

    fireEvent.change(screen.getByLabelText("覆盖 全局搜索"), { target: { value: "off" } });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "http://test.local/api/settings/feature-flags",
      expect.objectContaining({ method: "PUT" }),
    ));
    const putCall = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({ flags: { global_search: false } });
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "runtime.capabilities.inspect",
      source: "settings.feature_flags",
    }, { silent: true });
    expect((screen.getByLabelText("覆盖 全局搜索") as HTMLSelectElement).value).toBe("off");
  });

  it("shows local plugins and toggles plugin enablement from settings", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/plugins") && (!init?.method || init.method === "GET")) {
        return new Response(JSON.stringify({
          plugins: [
            {
              name: "demo-plugin",
              description: "Local workflow shortcuts",
              version: "1.0.0",
              path: "C:\\Users\\ago\\.minicode\\plugins\\demo-plugin",
              manifest_path: "C:\\Users\\ago\\.minicode\\plugins\\demo-plugin\\.minicode-plugin\\plugin.json",
              command_count: 2,
              skill_count: 1,
              enabled: true,
            },
          ],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes("/api/plugins/demo-plugin/state") && init?.method === "PUT") {
        return new Response(JSON.stringify({
          plugins: [
            {
              name: "demo-plugin",
              description: "Local workflow shortcuts",
              path: "C:\\Users\\ago\\.minicode\\plugins\\demo-plugin",
              command_count: 2,
              skill_count: 1,
              enabled: false,
            },
          ],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /插件/ }));
    await waitFor(() => expect(screen.getByText("demo-plugin")).toBeTruthy());
    expect(screen.getAllByText("1 个技能").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByLabelText("停用插件 demo-plugin"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "http://test.local/api/plugins/demo-plugin/state",
      expect.objectContaining({ method: "PUT" }),
    ));
    const putCall = fetchMock.mock.calls.find(([, init]) => init?.method === "PUT");
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({ enabled: false });
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "skills.list" }, { silent: true });
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "mcp.list" }, { silent: true });
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "runtime.capabilities.inspect",
      source: "settings.plugins",
    }, { silent: true });
    await waitFor(() => expect(screen.getByText("已停用")).toBeTruthy());
  });

  it("imports a local plugin folder from the plugins settings tab", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/plugins/import") && init?.method === "POST") {
        return new Response(JSON.stringify({
          imported: { name: "demo-plugin" },
          plugins: [
            {
              name: "demo-plugin",
              path: "C:\\plugins\\demo-plugin",
              command_count: 1,
              skill_count: 0,
              enabled: true,
            },
          ],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes("/api/plugins/validate") && init?.method === "POST") {
        return new Response(JSON.stringify({
          ok: true,
          plugin: {
            name: "demo-plugin",
            command_count: 1,
            skill_count: 0,
            file_count: 2,
          },
          warnings: [],
          errors: [],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes("/api/plugins/package") && init?.method === "POST") {
        return new Response(JSON.stringify({
          ok: true,
          package: {
            name: "demo-plugin-dev.minicode-plugin.zip",
            path: "C:\\plugins\\packages\\demo-plugin-dev.minicode-plugin.zip",
            file_count: 2,
          },
          validation: {
            ok: true,
            plugin: {
              name: "demo-plugin",
              command_count: 1,
              skill_count: 0,
              file_count: 2,
            },
            warnings: [],
            errors: [],
          },
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes("/api/plugins") && (!init?.method || init.method === "GET")) {
        return new Response(JSON.stringify({ plugins: [] }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /插件/ }));
    await waitFor(() => expect(screen.getByLabelText("插件文件夹或安装包路径")).toBeTruthy());

    fireEvent.change(screen.getByLabelText("插件文件夹或安装包路径"), { target: { value: "C:\\plugins\\demo-plugin" } });
    fireEvent.click(screen.getByRole("button", { name: "导入" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "http://test.local/api/plugins/import",
      expect.objectContaining({ method: "POST" }),
    ));
    const importCall = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(importCall?.[1]?.body))).toEqual({ source_path: "C:\\plugins\\demo-plugin" });
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "skills.list" }, { silent: true });
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "mcp.list" }, { silent: true });
    await waitFor(() => expect(screen.getByText("demo-plugin")).toBeTruthy());
  });

  it("imports a packaged plugin zip from the plugins settings tab", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/plugins/import") && init?.method === "POST") {
        return new Response(JSON.stringify({
          imported: { name: "zip-plugin", kind: "package" },
          plugins: [
            {
              name: "zip-plugin",
              path: "C:\\Users\\ago\\.minicode\\plugins\\zip-plugin",
              command_count: 1,
              skill_count: 0,
              enabled: true,
            },
          ],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes("/api/plugins") && (!init?.method || init.method === "GET")) {
        return new Response(JSON.stringify({ plugins: [] }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /插件/ }));
    await waitFor(() => expect(screen.getByLabelText("插件文件夹或安装包路径")).toBeTruthy());

    fireEvent.change(screen.getByLabelText("插件文件夹或安装包路径"), { target: { value: "C:\\plugins\\zip-plugin.minicode-plugin.zip" } });
    fireEvent.click(screen.getByRole("button", { name: "导入 Zip" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "http://test.local/api/plugins/import",
      expect.objectContaining({ method: "POST" }),
    ));
    const importCall = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(importCall?.[1]?.body))).toEqual({ source_path: "C:\\plugins\\zip-plugin.minicode-plugin.zip" });
    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "runtime.capabilities.inspect",
      source: "settings.plugins.import.package",
    }, { silent: true });
    await waitFor(() => expect(screen.getByText("zip-plugin")).toBeTruthy());
  });

  it("validates and packages a local plugin folder from the plugins settings tab", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/plugins/validate") && init?.method === "POST") {
        return new Response(JSON.stringify({
          ok: true,
          plugin: {
            name: "demo-plugin",
            command_count: 1,
            skill_count: 0,
            file_count: 2,
          },
          warnings: [],
          errors: [],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes("/api/plugins/package") && init?.method === "POST") {
        return new Response(JSON.stringify({
          ok: true,
          package: {
            name: "demo-plugin-dev.minicode-plugin.zip",
            path: "C:\\plugins\\packages\\demo-plugin-dev.minicode-plugin.zip",
            file_count: 2,
          },
          validation: {
            ok: true,
            plugin: {
              name: "demo-plugin",
              command_count: 1,
              skill_count: 0,
              file_count: 2,
            },
            warnings: [],
            errors: [],
          },
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes("/api/plugins") && (!init?.method || init.method === "GET")) {
        return new Response(JSON.stringify({ plugins: [] }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /插件/ }));
    await waitFor(() => expect(screen.getByLabelText("插件文件夹或安装包路径")).toBeTruthy());

    fireEvent.change(screen.getByLabelText("插件文件夹或安装包路径"), { target: { value: "C:\\plugins\\demo-plugin" } });
    fireEvent.click(screen.getByRole("button", { name: "验证" }));

    await waitFor(() => expect(screen.getByText("验证通过")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "打包" }));

    await waitFor(() => expect(screen.getByText("C:\\plugins\\packages\\demo-plugin-dev.minicode-plugin.zip")).toBeTruthy());
    expect(fetchMock).toHaveBeenCalledWith(
      "http://test.local/api/plugins/validate",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "http://test.local/api/plugins/package",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("renders owner-scoped OAuth evidence, never auto-opens it, and sends owner-complete commands", async () => {
    const payload: LLMSettingsPayload = {
      provider: "openai",
      active_model: "gpt-5",
      openai: {
        display_name: "OpenAI",
        has_api_key: false,
        api_key: "",
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        available_models: ["gpt-5"],
        wire_api: "responses",
      },
      provider_history: [{
        provider: "openai",
        provider_id: "openai_official",
        display_name: "OpenAI",
        has_api_key: false,
        api_key: "",
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        available_models: ["gpt-5"],
        wire_api: "responses",
        updated_at: 1,
      }],
    };
    const popup = { opener: window } as unknown as Window;
    const webOpen = vi.spyOn(window, "open").mockReturnValue(popup);
    awaitCommandResultMock.mockImplementation(async (command: { type: string }) => ({
      type: "command.result",
      command: command.type,
      level: "success",
      message: "",
      data: command.type === "llm.provider.oauth.status"
        ? { oauth_supported: true, configured: false }
        : {},
    }));
    useAppStore.setState({
      conversationId: "conv-oauth",
      providerOAuthFlowsByConversation: {
        "conv-oauth": {
          openai: {
            conversationId: "conv-oauth",
            provider: "openai",
            phase: "device_code",
            url: "https://auth.openai.com/oauth/authorize?state=expected",
            instructions: "Complete login in the browser.",
            userCode: "ABCD-EFGH",
            verificationUri: "https://auth.openai.com/codex/device",
            intervalSeconds: 5,
            expiresInSeconds: 900,
            expiresAt: Date.now() + 900_000,
            message: "Waiting for approval.",
            links: [{ url: "https://help.openai.com/oauth", label: "OAuth help" }],
            updatedAt: Date.now(),
            eventSeq: 40,
          },
        },
      },
    });

    render(
      <ProviderTab
        selectedProvider="openai"
        settingsPayload={payload}
        settingsPayloadRef={{ current: payload }}
        onProviderChange={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "编辑 OpenAI" }));
    expect(screen.getByText("Complete login in the browser.")).toBeTruthy();
    expect(screen.getByText("Waiting for approval.")).toBeTruthy();
    expect(screen.getByText("ABCD-EFGH")).toBeTruthy();
    expect(screen.getByText(/轮询间隔：5 秒/)).toBeTruthy();
    expect(screen.getByText("https://auth.openai.com/oauth/authorize?state=expected")).toBeTruthy();
    expect(screen.getByText("https://auth.openai.com/codex/device")).toBeTruthy();
    expect(screen.getByText("OAuth help")).toBeTruthy();
    expect(webOpen).not.toHaveBeenCalled();
    expect(openExternalMock).not.toHaveBeenCalled();

    await waitFor(() => expect(awaitCommandResultMock).toHaveBeenCalledWith({
      type: "llm.provider.oauth.status",
      provider: "openai",
      conversation_id: "conv-oauth",
    }));

    const openButtons = screen.getAllByRole("button", { name: "打开" });
    fireEvent.click(openButtons[0]);
    expect(webOpen).toHaveBeenCalledWith(
      "https://auth.openai.com/oauth/authorize?state=expected",
      "_blank",
      "noopener,noreferrer",
    );
    expect(popup.opener).toBeNull();

    desktopModeMock.mockReturnValue(true);
    fireEvent.click(openButtons[1]);
    await waitFor(() => expect(openExternalMock).toHaveBeenCalledWith(
      "https://auth.openai.com/codex/device",
    ));

    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await waitFor(() => expect(awaitCommandResultMock).toHaveBeenCalledWith({
      type: "llm.provider.oauth.login",
      provider: "openai",
      conversation_id: "conv-oauth",
    }));
    await waitFor(() => expect(
      useAppStore.getState().providerOAuthFlowsByConversation["conv-oauth"]?.openai,
    ).toBeUndefined());
  });

  it("refuses to open an unsafe OAuth projection even if local state is corrupted", () => {
    const payload: LLMSettingsPayload = {
      provider: "openai",
      active_model: "gpt-5",
      openai: {
        display_name: "OpenAI",
        has_api_key: false,
        api_key: "",
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        available_models: ["gpt-5"],
        wire_api: "responses",
      },
      provider_history: [{
        provider: "openai",
        provider_id: "openai_official",
        display_name: "OpenAI",
        has_api_key: false,
        api_key: "",
        base_url: "https://api.openai.com/v1",
        model: "gpt-5",
        available_models: ["gpt-5"],
        wire_api: "responses",
        updated_at: 1,
      }],
    };
    const webOpen = vi.spyOn(window, "open").mockReturnValue(null);
    useAppStore.setState({
      conversationId: "conv-oauth",
      providerOAuthFlowsByConversation: {
        "conv-oauth": {
          openai: {
            conversationId: "conv-oauth",
            provider: "openai",
            phase: "auth_url",
            url: "javascript:alert(document.cookie)",
            updatedAt: Date.now(),
          },
        },
      },
    });

    render(
      <ProviderTab
        selectedProvider="openai"
        settingsPayload={payload}
        settingsPayloadRef={{ current: payload }}
        onProviderChange={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "编辑 OpenAI" }));
    fireEvent.click(screen.getByRole("button", { name: "打开" }));

    expect(webOpen).not.toHaveBeenCalled();
    expect(openExternalMock).not.toHaveBeenCalled();
  });
});
