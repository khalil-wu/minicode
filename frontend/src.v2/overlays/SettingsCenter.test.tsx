/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import type { LLMSettingsPayload } from "./settingsShared";
import { ProviderTab } from "./ProviderTab";
import { SettingsCenter } from "./SettingsCenter";
import { SkillsMarketplace } from "./SkillsMarketplace";
import { DEFAULT_SHORTCUT_BINDINGS } from "../lib/keyboard-shortcuts";
import { installAppThemeTokens } from "../../test/app-theme-tokens";
import { openSettings } from "../lib/settings-navigation";
import type { ComputerUseSettingsSnapshot } from "../protocol/computer-use-settings";

let removeThemeTokens: () => void;
beforeEach(() => { removeThemeTokens = installAppThemeTokens(); });
afterEach(() => removeThemeTokens());
const desktopPreferences = vi.hoisted(() => ({ value: null as Record<string, unknown> | null }));
const pluginsFeature = vi.hoisted(() => ({ enabled: true as boolean | undefined }));
const memoryFixture = vi.hoisted(() => ({ value: { feature_enabled: true, memories: { generate_memories: true, use_memories: true, disable_on_external_context: false }, path: "/config/settings.json", feature_locked: false, locked_fields: [] as string[] }, calls: [] as Array<{ url: string; body: Record<string, boolean> }> }));
const computerUseFixture = vi.hoisted(() => ({ sessionId: null as string | null, data: null as ComputerUseSettingsSnapshot | null }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => computerUseFixture.sessionId ? { sessionId: computerUseFixture.sessionId } : null }));
vi.mock("../hooks/useDesktopPreferences", () => ({ useDesktopPreferences: () => ({ preferences: desktopPreferences.value, error: "", update: vi.fn() }) }));
vi.mock("../hooks/usePluginsFeature", () => ({ usePluginsFeature: () => ({ enabled: pluginsFeature.enabled, locked: false, error: "" }), setPluginsFeatureEnabled: vi.fn() }));

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
const embeddedBrowserSetSettingsMock = vi.fn(async (payload: { downloadPolicy?: string; browserEnabled?: boolean }) => ({ downloadPolicy: payload.downloadPolicy || "block", browserEnabled: payload.browserEnabled ?? true, origin: "", permissions: [], revision: 1 }));
const awaitCommandResultMock = vi.fn(async (command: { type: string }) => ({ type: "command.result", command: command.type, level: "info", message: "", data: {} }));
const skillsSettingsRequest = vi.fn();
const licensesRequest = vi.fn(async () => "MIT license fixture");

vi.mock("../protocol/api", () => ({
  apiBase: () => "http://test.local",
  runtimeToken: () => "",
  authHeaders: (headers?: HeadersInit) => headers ?? {},
  errorMessageFromResponseText: (text: string, fallback: string) => text || fallback,
  fetchWithTimeout: (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes("/api/settings/memory")) {
      if (init?.method === "PUT" || init?.method === "POST") {
        const body = JSON.parse(String(init.body)); memoryFixture.calls.push({ url: String(input), body });
        if (body.enabled !== undefined) { memoryFixture.value.feature_enabled = body.enabled; memoryFixture.value.memories.generate_memories = body.enabled; memoryFixture.value.memories.use_memories = body.enabled; }
        if (body.disable_on_external_context !== undefined) memoryFixture.value.memories.disable_on_external_context = body.disable_on_external_context;
      }
      return Promise.resolve(Response.json(memoryFixture.value));
    }
    return String(input).includes("/api/skills/settings") ? skillsSettingsRequest(input, init) : fetch(input, init);
  },
  LONG_HTTP_TIMEOUT_MS: 300_000,
  fetchLLMSettings: () => fetchLLMSettingsMock(),
}));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(),
  sendClientCommandAwaitResult: (command: { type: string }) => awaitCommandResultMock(command),
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed",
  sendConversationDeleteCommand: vi.fn(),
  LONG_COMMAND_RESULT_TIMEOUT_MS: 300_000,
}));

vi.mock("../desktop/runtime", () => ({
  runtime: () => undefined,
  desktop: () => desktopModeMock() ? { platformInfo: { platform: "linux" }, preferences: { getOpenSourceLicenses: licensesRequest } } : null,
  embeddedBrowserGetSettings: vi.fn(async () => ({ browserEnabled: true, downloadPolicy: "block", origin: "", permissions: [], revision: 0 })),
  browserHistorySearch: vi.fn(async () => []),
  browserHistoryRemove: vi.fn(async () => true),
  onEmbeddedBrowserSettingsChanged: vi.fn(() => () => {}),
  embeddedBrowserList: vi.fn(async () => []),
  embeddedBrowserSetSettings: (payload: { downloadPolicy?: string; browserEnabled?: boolean }) => embeddedBrowserSetSettingsMock(payload),
  envDetect: vi.fn(async () => null),
  isDesktop: () => desktopModeMock(),
  openExternal: (url: string) => openExternalMock(url),
}));

describe("SettingsCenter reasoning effort visibility", () => {
  beforeEach(() => {
    memoryFixture.calls = [];
    memoryFixture.value = { feature_enabled: true, memories: { generate_memories: true, use_memories: true, disable_on_external_context: false }, path: "/config/settings.json", feature_locked: false, locked_fields: [] };
    computerUseFixture.sessionId = null;
    computerUseFixture.data = null;
    desktopPreferences.value = null;
    pluginsFeature.enabled = true;
    // Hidden integration inventories also load while testing MCP tabs. Keep
    // those reads local so a prior case cannot leave a real network retry in
    // the shared inventory request cache used by a later marketplace case.
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      if (path === "/api/plugins") return new Response(JSON.stringify({ plugins: [] }), { status: 200 });
      if (path === "/api/plugins/marketplaces") return new Response(JSON.stringify({ marketplaces: [] }), { status: 200 });
      if (path === "/api/settings/computer-use") return new Response(JSON.stringify(computerUseFixture.data), { status: 200 });
      if (path === "/api/settings/import/detect") return Response.json({ detection_id: "empty-general", items: [], issues: [] });
      if (path === "/api/settings/import") return Response.json({ providers: [], history: [], sync: { enabled: false, item_types: [] }, connected_provider_ids: [], item_types: [], running_import: null });
      throw new Error(`Unexpected settings fixture request: ${input}`);
    }));
    skillsSettingsRequest.mockReset();
    skillsSettingsRequest.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const available = useAppStore.getState().availableSkills;
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      const skills = available.filter(skill => skill.source_level === "user").map(skill => ({ ...skill,
        enabled: body?.path === skill.path ? body.enabled : true }));
      return new Response(JSON.stringify({ skills, plugin_skills: [], available_skills: available.filter(skill => body?.path !== skill.path || body.enabled),
        workspace_root: body?.workspace_root ?? new URL(String(input)).searchParams.get("workspace_root") ?? "" }), { status: 200 });
    });
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
      skillsMarketplaceOpen: false,
      settingsTab: "general",
      browserSettingsSection: "general",
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
      codeFontBase: 12,
      reducedMotionPreference: "system",
      reducedMotion: false,
      pointerCursors: false,
      diffMarkerStyle: "color",
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
    vi.useRealTimers();
    fetchLLMSettingsMock.mockReset();
    vi.clearAllMocks();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("opens the actual provider-backed Computer use settings and search entry without changing the composer", async () => {
    computerUseFixture.sessionId = "settings-owner";
    computerUseFixture.data = { owner: { session_id: "settings-owner", conversation_id: "A", workspace_root: "C:/project" },
      provider_id: "minicode-win32", selection_id: "native-current", navigation_visible: true, available: true, status: "ready", reason: null,
      capabilities: { persistent_approvals: true, revoke_app_approval: true, persist_modes: ["session", "always"], sound_mode: false, locked_use: false },
      approved_apps: [{ app_id: "actual-app", display_name: "Actual App", icon_data_url: null }], controls: [] };
    useAppStore.setState({ isConnected: true, conversationId: "A", workingDirectory: "C:/project", draft: "Keep this draft" });
    render(<SettingsCenter />);
    fireEvent.click(await screen.findByRole("button", { name: "电脑操作" }));
    expect(await screen.findByRole("heading", { name: "始终允许的应用" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "移除 Actual App" })).toBeTruthy();
    expect(screen.queryByText("控制")).toBeNull();
    expect(useAppStore.getState().draft).toBe("Keep this draft");
    fireEvent.change(screen.getByRole("textbox", { name: "搜索设置" }), { target: { value: "始终允许" } });
    expect(screen.getByRole("button", { name: /电脑操作 \/ 始终允许的应用/ })).toBeTruthy();
  });

  it("hides unsupported provider management from navigation and search and redirects a hidden direct route", async () => {
    computerUseFixture.sessionId = "settings-owner";
    computerUseFixture.data = { owner: { session_id: "settings-owner", conversation_id: null, workspace_root: null },
      provider_id: "installed-source", selection_id: "installed-current", navigation_visible: false, available: true, status: "ready", reason: "Persistent management is unavailable for the selected helper",
      capabilities: { persistent_approvals: false, revoke_app_approval: false, persist_modes: ["session"], sound_mode: false, locked_use: false }, approved_apps: null, controls: [] };
    useAppStore.setState({ isConnected: true, conversationId: null, workingDirectory: "", settingsTab: "computerUse" });
    render(<SettingsCenter />);
    await waitFor(() => expect(useAppStore.getState().settingsTab).toBe("general"));
    expect(screen.queryByRole("button", { name: "电脑操作" })).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "搜索设置" }), { target: { value: "始终允许" } });
    expect(screen.queryByRole("button", { name: /电脑操作 \/ 始终允许的应用/ })).toBeNull();
    expect(screen.queryByRole("heading", { name: "始终允许的应用" })).toBeNull();
  });

  it("hides reasoning effort for DeepSeek chat completions because runtime will ignore it", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    render(<SettingsCenter />);
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(fetchLLMSettingsMock).toHaveBeenCalled();

    expect(screen.queryByText("推理强度")).toBeNull();
    expect(screen.queryByRole("button", { name: "Low" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Max" })).toBeNull();
  });

  it("keeps system, dark, and light theme controls in appearance settings", async () => {
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "外观" }));
    // Assert the real lazy page after its module graph has loaded; cold Vite
    // transforms are not part of the theme control's DOM query deadline.
    await act(async () => { await vi.dynamicImportSettled(); });

    const themes = within(screen.getByRole("radiogroup", { name: "应用主题" }));
    expect(themes.getByRole("radio", { name: "深色" }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(themes.getByRole("radio", { name: "浅色" }));
    expect(useAppStore.getState().themeMode).toBe("light");
    fireEvent.click(themes.getByRole("radio", { name: "系统" }));
    expect(useAppStore.getState().themeMode).toBe("system");
  });

  it("keeps a directly opened settings category visible in the narrow navigation", async () => {
    const mediaMock = vi.spyOn(window, "matchMedia");
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

  it("keeps settings navigation at the existing expanded width when the chat sidebar is collapsed and preserves page search", async () => {
    useAppStore.setState({ settingsTab: "shortcuts", leftSidebarWidth: 340, leftSidebarExpandedWidth: 340 });
    const { container } = render(<SettingsCenter />);
    fireEvent.change(await screen.findByPlaceholderText("搜索快捷键"), { target: { value: "终端" } });
    act(() => useAppStore.getState().setLeftSidebarWidth(0));
    expect(container.querySelector(".settings-workspace-sidebar")).toHaveProperty("hidden", false);
    expect(container.querySelector(".settings-workspace")?.getAttribute("style")).toContain("--settings-sidebar-width: 340px");
    expect(screen.getByRole("navigation", { name: "设置分类" })).toBeTruthy();
    expect(screen.getByPlaceholderText("搜索快捷键")).toHaveProperty("value", "终端");
    act(() => useAppStore.getState().setLeftSidebarWidth(340));
    expect(screen.getByRole("button", { name: "快捷键" }).getAttribute("aria-current")).toBe("page");
    expect(screen.getByPlaceholderText("搜索快捷键")).toHaveProperty("value", "终端");
  });

  it("allows nonmodal settings focus to reach the rail and restores only the actual closing trigger", async () => {
    useAppStore.setState({ settingsOpen: false, draft: "Retained workspace draft" });
    render(<><button onClick={() => useAppStore.getState().toggleSettings()}>Open settings</button>
      <button onClick={() => useAppStore.setState({ settingsOpen: false })}>Rail destination</button><SettingsCenter /></>);
    const opener = screen.getByRole("button", { name: "Open settings" });
    const rail = screen.getByRole("button", { name: "Rail destination" });
    opener.focus(); fireEvent.click(opener);
    const search = screen.getByRole("textbox", { name: "搜索设置" });
    await waitFor(() => expect(document.activeElement).toBe(search));
    rail.focus(); expect(document.activeElement).toBe(rail);
    const tab = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    rail.dispatchEvent(tab); expect(tab.defaultPrevented).toBe(false);
    search.focus(); fireEvent.keyDown(search, { key: "Escape" });
    expect(useAppStore.getState().settingsOpen).toBe(false);
    expect(document.activeElement).toBe(opener);
    fireEvent.click(opener);
    await waitFor(() => expect(document.activeElement).toBe(search));
    rail.focus(); fireEvent.click(rail);
    expect(document.activeElement).toBe(rail);
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, draft: "Retained workspace draft" });
  });

  it("gives an actual nested settings dialog Escape priority before closing the nonmodal page", async () => {
    useAppStore.setState({ settingsTab: "shortcuts", draft: "Retained chat draft" });
    useAppStore.getState().setShortcutBinding("commandPalette", "");
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "恢复默认" }));
    const dialog = await screen.findByRole("dialog", { name: "恢复默认" });
    const cancel = within(dialog).getByRole("button", { name: "取消" });
    cancel.focus(); fireEvent.keyDown(cancel, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "恢复默认" })).toBeNull());
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, draft: "Retained chat draft" });
    const search = screen.getByRole("textbox", { name: "搜索设置" });
    search.focus(); fireEvent.keyDown(search, { key: "Escape" });
    expect(useAppStore.getState().settingsOpen).toBe(false);
  });

  it("opens the local instructions page and returns only after the actual save completes", async () => {
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
    fireEvent.click(await screen.findByRole("button", { name: "编辑所选主机指令" }));

    const editor = await screen.findByRole("textbox", { name: "自定义指令" }) as HTMLTextAreaElement;
    expect(editor.value).toBe("复用现有结构");

    fireEvent.change(editor, { target: { value: "复用现有结构\n统一运行测试" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      const put = fetchMock.mock.calls.find((call) => call[1]?.method === "PUT");
      expect(put).toBeTruthy();
      expect(JSON.parse(String(put?.[1]?.body)).instructions).toBe("复用现有结构\n统一运行测试");
    });
    expect(await screen.findByRole("button", { name: "编辑所选主机指令" })).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "自定义指令" })).toBeNull();
  });

  it("saves host memory preferences while preserving the existing task's mode", async () => {
    useAppStore.setState({ conversationId: "conv-memory", conversations: [{ id: "conv-memory", title: "Memory", updatedAt: "", memoryMode: "enabled", memoryPolluted: false }] });
    render(<SettingsCenter />); fireEvent.click(screen.getByRole("button", { name: "个性化" }));
    const toggle = await screen.findByRole("switch", { name: "启用 MiniCode 记忆" });
    expect(toggle.getAttribute("aria-checked")).toBe("true"); fireEvent.click(toggle);
    await waitFor(() => expect(memoryFixture.calls).toContainEqual({ url: "http://test.local/api/settings/memory", body: { enabled: false } }));
    expect(useAppStore.getState().conversations[0].memoryMode).toBe("enabled");
    expect(awaitCommandResultMock).not.toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.memory_mode.set" }));
  });

  it("uses the desktop page heading without adding a subtitle or composer permission controls", () => {
    render(<SettingsCenter />);

    expect(screen.getByRole("heading", { name: "常规", level: 2 })).toBeTruthy();
    expect(screen.queryByText("设置工作方式、消息跟进、过程展示与内容加载。")).toBeNull();
    expect(screen.queryByRole("radiogroup", { name: "默认运行权限" })).toBeNull();
    expect(screen.queryByText("当前模型")).toBeNull();
    expect(screen.queryByRole("radio", { name: "使用自动审核" })).toBeNull();
    expect(screen.queryByRole("radio", { name: "使用规划模式" })).toBeNull();
  });

  it("opens General import and keeps management search available while preserving the existing workspace draft", async () => {
    useAppStore.setState({ workingDirectory: "C:/existing-project", recentWorkspaces: [], draft: "Unsaved workspace draft" });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url);
      if (url.pathname === "/api/settings/import/detect") return Response.json({ detection_id: "general-import", items: [{
        id: "general-skill", provider_id: "claude", item_type: "skills", title: "source-skill",
        source_path: "C:/source/.claude/skills/source-skill", target_path: "C:/target/skills/source-skill", workspace_root: "", supported: true, reason: "",
      }], issues: [] });
      if (url.pathname === "/api/settings/import" && init?.method === "GET") return Response.json({
        providers: [{ id: "claude", name: "Claude Code" }], history: [], sync: { enabled: false, item_types: ["skills"] }, connected_provider_ids: [], item_types: ["skills"], running_import: null,
      });
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SettingsCenter />);
    const navigation = screen.getByRole("navigation", { name: "设置分类" });
    expect(within(navigation).queryByRole("button", { name: "导入", exact: true })).toBeNull();
    expect(screen.getByText("从其他 AI 应用导入")).toBeTruthy();
    expect(within(navigation).getByRole("button", { name: "连接", exact: true })).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "导入", exact: true }));
    expect(useAppStore.getState().settingsTab).toBe("general");
    expect(screen.getByRole("switch", { name: "Claude Code" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    expect(screen.getByRole("dialog", { name: "选择要导入的内容" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "取消", exact: true }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(fetchMock.mock.calls.some(([input, init]) => new URL(String(input)).pathname === "/api/settings/import" && init?.method === "POST")).toBe(false);
    fireEvent.click(within(navigation).getByRole("button", { name: "常规", exact: true }));
    fireEvent.change(screen.getByRole("textbox", { name: "搜索设置" }), { target: { value: "自动同步" } });
    fireEvent.click(within(navigation).getByRole("button", { name: /^导入 \/ 自动同步/ }));
    expect(useAppStore.getState().settingsTab).toBe("import");
    fireEvent.click(await screen.findByRole("button", { name: "导入 Claude Code" }));
    expect(screen.getByRole("dialog", { name: "从其他 AI 应用导入" })).toBeTruthy();
    act(() => useAppStore.setState({ settingsTab: "general" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.keyDown(screen.getByRole("textbox", { name: "搜索设置" }), { key: "Escape" });
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, workingDirectory: "C:/existing-project", draft: "Unsaved workspace draft" });
  });

  it("keeps unsupported selectors out of General and preserves stored media preferences", () => {
    useAppStore.setState({ remoteImagePolicy: "block", allowedRemoteImageDomains: ["images.example.com"] });
    render(<SettingsCenter />);

    expect(screen.queryByRole("button", { name: "对话" })).toBeNull();
    expect(screen.queryByRole("button", { name: "协作" })).toBeNull();
    expect(screen.queryByRole("button", { name: "代码" })).toBeNull();
    expect(screen.queryByRole("button", { name: "详细" })).toBeNull();

    expect(screen.queryByText("高级")).toBeNull();
    expect(screen.queryByRole("button", { name: /远程 Markdown 图片/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "清除列表" })).toBeNull();
    expect(useAppStore.getState()).toMatchObject({ remoteImagePolicy: "block", allowedRemoteImageDomains: ["images.example.com"] });
  });

  it("filters the existing shortcut list", () => {
    useAppStore.setState({ appMode: "code" });
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "快捷键" }));
    const search = screen.getByRole("textbox", { name: "搜索快捷键" });
    fireEvent.change(search, { target: { value: "terminal" } });

    expect(screen.getByText("打开终端")).toBeTruthy();
    expect(screen.queryByText("发送消息")).toBeNull();
    expect(screen.getByRole("button", { name: "编辑 打开终端" }).textContent).toContain("Ctrl + `");
    fireEvent.change(search, { target: { value: "工作区" } });
    expect(screen.getByRole("button", { name: "编辑 全局搜索" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "编辑 搜索项目内容" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "编辑 打开终端" })).toBeNull();
  });

  it("persists code sizing and reduced-motion preferences with real consumers", () => {
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "外观" }));
    fireEvent.click(document.querySelector<HTMLElement>('[data-settings-tab="appearance"] .settings-appearance-advanced-trigger')!);

    fireEvent.change(screen.getByRole("spinbutton", { name: "代码字号" }), { target: { value: "16" } });
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "减少动态效果" })).getByRole("radio", { name: "开" }));

    expect(useAppStore.getState().codeTextScale).toBe(16 / 12);
    expect(document.documentElement.style.getPropertyValue("--code-text-scale")).toBe(String(16 / 12));
    expect(useAppStore.getState().reducedMotion).toBe(true);
    expect(document.documentElement.getAttribute("data-reduced-motion")).toBe("true");
  });

  it("changes the composer send shortcut and running-turn follow-up behavior", () => {
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: /^发送快捷键，当前：/ }));
    fireEvent.click(screen.getByRole("option", { name: "总是使用 ^ + Enter" }));
    fireEvent.click(screen.getByRole("radio", { name: "引导" }));

    expect(useAppStore.getState().sendShortcut).toBe("mod-enter");
    expect(useAppStore.getState().followUpBehavior).toBe("steer");
  });

  it("records shortcut edits, rejects conflicts, deletes bindings, and confirms restoring defaults", async () => {
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "快捷键" }));
    expect(screen.queryByRole("button", { name: "恢复默认" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "编辑 命令面板" }));
    const commandCapture = screen.getByRole("textbox", { name: "编辑 命令面板" });
    expect(commandCapture).toHaveProperty("readOnly", true);
    fireEvent.keyDown(commandCapture, { key: "K", code: "KeyK", ctrlKey: true, shiftKey: true });
    expect(useAppStore.getState().shortcutBindings.commandPalette).toEqual(["Mod+Shift+K"]);

    fireEvent.click(screen.getByRole("button", { name: "编辑 设置" }));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "编辑 设置" }), { key: "K", code: "KeyK", ctrlKey: true, shiftKey: true });
    expect(screen.getByRole("alert").textContent).toContain("命令面板");
    expect(useAppStore.getState().shortcutBindings.settings).toEqual(["Mod+Comma"]);

    fireEvent.click(screen.getByRole("button", { name: "取消", exact: true }));
    expect(screen.queryByRole("textbox", { name: "编辑 设置" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "删除 命令面板 快捷键" }));
    expect(useAppStore.getState().shortcutBindings.commandPalette).toEqual([]);
    expect(screen.queryByRole("button", { name: "删除 命令面板 快捷键" })).toBeNull();
    expect(screen.getByRole("button", { name: "编辑 命令面板" }).textContent).toContain("未设置");
    fireEvent.click(screen.getByRole("button", { name: "恢复默认" }));
    const confirmation = await screen.findByRole("dialog", { name: "恢复默认" });
    expect(useAppStore.getState().shortcutBindings.commandPalette).toEqual([]);
    fireEvent.click(within(confirmation).getByRole("button", { name: "恢复默认" }));
    await waitFor(() => expect(useAppStore.getState().shortcutBindings.commandPalette).toEqual(["Mod+K"]));
    expect(screen.queryByRole("button", { name: "恢复默认" })).toBeNull();
  });

  it("closes settings with Escape after a disabled control releases focus to the document", () => {
    useAppStore.setState({ draft: "Keep the current chat draft", rightPanelOpen: true, rightStackTab: "terminal" });
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "快捷键" }));
    fireEvent.click(screen.getByRole("button", { name: "删除 打开终端 快捷键" }));
    (document.activeElement as HTMLElement).blur();
    expect(document.activeElement).toBe(document.body);
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, rightPanelOpen: true,
      rightStackTab: "terminal", draft: "Keep the current chat draft" });
  });

  it("finds setting pages by their actual controls", () => {
    render(<SettingsCenter />);

    fireEvent.change(screen.getByRole("textbox", { name: "搜索设置" }), { target: { value: "下载" } });

    expect(screen.getByRole("heading", { name: "常规", level: 2 })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /浏览器 \/ 浏览数据/ }));

    expect(screen.getByRole("heading", { name: "浏览器", level: 2 })).toBeTruthy();
    expect(screen.getByRole("button", { name: "浏览器" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "模型" })).toBeNull();
  });

  it("ends shortcut recording when its page is hidden or settings is closed", () => {
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "快捷键" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 命令面板" }));
    expect(screen.getByRole("textbox", { name: "编辑 命令面板" }).getAttribute("data-shortcut-recording")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "外观" }));
    expect(document.querySelector('[data-shortcut-recording="true"]')).toBeNull();
    fireEvent.keyDown(window, { key: "K", code: "KeyK", ctrlKey: true, shiftKey: true });
    expect(useAppStore.getState().shortcutBindings.commandPalette).toEqual(["Mod+K"]);
    fireEvent.click(screen.getByRole("button", { name: "快捷键" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 命令面板" }));
    fireEvent.keyDown(screen.getByRole("main", { name: "设置" }), { key: "Escape" });
    expect(document.querySelector('[data-shortcut-recording="true"]')).toBeNull();
    fireEvent.keyDown(window, { key: "K", code: "KeyK", ctrlKey: true, shiftKey: true });
    expect(useAppStore.getState().shortcutBindings.commandPalette).toEqual(["Mod+K"]);
  });

  it("keeps a scoped configuration draft across pages and returning to the app", async () => {
    useAppStore.setState({ conversationId: "draft-owner", workingDirectory: "C:/draft-project", isConnected: true });
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "插件", exact: true }));
    fireEvent.click(screen.getByRole("button", { name: "MCP 服务", exact: true }));
    fireEvent.click(screen.getByRole("button", { name: "添加", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "添加 MCP 服务器" }));
    fireEvent.change(await screen.findByLabelText("服务名称"), { target: { value: "未保存的服务" } });
    fireEvent.change(screen.getByLabelText("启动命令"), { target: { value: "npx" } });
    fireEvent.click(screen.getByRole("button", { name: "外观" }));
    fireEvent.keyDown(screen.getByRole("main", { name: "设置" }), { key: "Escape" });
    expect(screen.queryByRole("main", { name: "设置" })).toBeNull();
    act(() => useAppStore.setState({ settingsOpen: true, settingsTab: "connectors" }));
    expect(screen.getByLabelText("服务名称")).toHaveProperty("value", "未保存的服务");
    expect(screen.getByLabelText("启动命令")).toHaveProperty("value", "npx");
  });

  it("preserves a model draft if the reopen refresh fails", async () => {
    useAppStore.setState({ settingsTab: "provider" });
    render(<SettingsCenter />);
    fireEvent.click(await screen.findByRole("button", { name: "编辑 DeepSeek" }));
    fireEvent.change(screen.getByLabelText("API 密钥"), { target: { value: "unsaved-credential" } });
    fireEvent.keyDown(screen.getByRole("main", { name: "设置" }), { key: "Escape" });
    fetchLLMSettingsMock.mockRejectedValueOnce(new Error("refresh offline"));
    act(() => useAppStore.setState({ settingsOpen: true }));
    expect(await screen.findByText("模型设置刷新失败，编辑草稿已保留")).toBeTruthy();
    expect(screen.getByLabelText("API 密钥")).toHaveProperty("value", "unsaved-credential");
  });

  it("finds reduced motion by an animation synonym and focuses the real option", async () => {
    render(<SettingsCenter />);
    fireEvent.change(screen.getByRole("textbox", { name: "搜索设置" }), { target: { value: "减少动画" } });
    fireEvent.click(screen.getByRole("button", { name: /外观 \/ 减少动态效果/ }));
    expect(screen.getByRole("heading", { name: "外观", level: 2 })).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(within(screen.getByRole("radiogroup", { name: "减少动态效果" })).getByRole("radio", { name: "系统" })));
  });

  it("searches the actual advanced typography and pointer controls without stale size entries", async () => {
    render(<SettingsCenter />);
    const search = screen.getByRole("textbox", { name: "搜索设置" });
    fireEvent.change(search, { target: { value: "字号" } });
    expect(screen.queryByRole("button", { name: /外观 \/ 正文字号/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /外观 \/ 编辑器字号/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /外观 \/ 代码字号/ }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("spinbutton", { name: "代码字号" })));
    fireEvent.change(search, { target: { value: "pointer" } });
    fireEvent.click(screen.getByRole("button", { name: /外观 \/ 使用手形指针/ }));
    const pointer = screen.getByRole("switch", { name: "使用手形指针" });
    await waitFor(() => expect(document.activeElement).toBe(pointer));
    fireEvent.click(pointer);
    expect(useAppStore.getState().pointerCursors).toBe(true);
  });

  it("opens the real licenses detail with one heading and restores the General page and draft", async () => {
    desktopModeMock.mockReturnValue(true);
    desktopPreferences.value = {
      defaultTaskDirectory: "C:/tasks", fileOpenTarget: "system", fileOpenTargetPerPath: {},
      fileOpenTargets: [{ id: "system", label: "Default app", kind: "systemDefault" }],
      terminalShell: "shell", terminalShellTargets: [{ id: "shell", label: "Shell" }], localeOverride: "zh-CN", systemLocale: "zh-CN",
      confirmBeforeClose: "never", showFullAccess: false, showBottomPanel: true, autoArchiveAfterDays: 0, localHistoryCompression: false,
      turnCompletionNotifications: "onlyWhenUnfocused", permissionNotifications: true, questionNotifications: true,
      notificationSound: "default", dotNotifications: true,
    };
    useAppStore.setState({ draft: "Original unsent draft" });
    const { container } = render(<SettingsCenter />);
    const page = container.querySelector<HTMLElement>(".settings-center-main")!;
    page.scrollTop = 210; fireEvent.scroll(page);
    const row = screen.getByText("开源许可证").closest(".settings-row")!;
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "查看" }));
    expect(await screen.findByText("MIT license fixture")).toBeTruthy();
    expect(within(page).getAllByRole("heading", { level: 2 })).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "开源许可证", level: 2 })).toBeTruthy();
    expect(page.scrollTop).toBe(0);
    fireEvent.click(within(page).getByRole("button", { name: "常规", exact: true }));
    expect(screen.getByRole("heading", { name: "常规", level: 2 })).toBeTruthy();
    expect(page.scrollTop).toBe(210);
    expect(useAppStore.getState().draft).toBe("Original unsent draft");
  });

  it("presents model load failures as a recoverable settings state", async () => {
    fetchLLMSettingsMock.mockRejectedValueOnce(new Error("Bad Gateway"));
    render(<SettingsCenter />);

    act(() => useAppStore.getState().setSettingsTab("provider"));

    expect(await screen.findByText("模型设置暂时不可用")).toBeTruthy();
    expect(screen.getByText("检查后端连接后重试。")).toBeTruthy();
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });

  it("renders the settings destination selected before the lazy panel mounts", () => {
    useAppStore.setState({ settingsTab: "connectors" });

    render(<SettingsCenter />);

    expect(screen.getByRole("region", { name: "插件" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "MCP 服务", exact: true }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.queryByRole("region", { name: "常规" })).toBeNull();
  });

  it("groups installed capabilities under plugins while keeping real MCP state searchable", () => {
    useAppStore.setState({ mcpServers: [{ name: "node_repl", status: "connected", tools: 7 }, { name: "browser", status: "connected", tools: 3 }] });
    render(<SettingsCenter />);
    const navigation = screen.getByRole("navigation", { name: "设置分类" });
    expect(within(navigation).queryByRole("button", { name: "MCP", exact: true })).toBeNull();
    expect(within(navigation).queryByRole("button", { name: "技能", exact: true })).toBeNull();
    fireEvent.click(within(navigation).getByRole("button", { name: "插件", exact: true }));
    fireEvent.click(screen.getByRole("button", { name: "MCP 服务", exact: true }));
    expect(screen.getByText("node_repl")).toBeTruthy();
    expect(screen.queryByText("7 个工具")).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "搜索 MCP 服务器" }), { target: { value: "node" } });
    expect(screen.getByText("node_repl")).toBeTruthy();
    expect(screen.queryByText("browser", { exact: true })).toBeNull();
  });

  it("keeps MCP permanent and shows only counted installed extension types", async () => {
    skillsSettingsRequest.mockImplementation(async () => new Response(JSON.stringify({ skills: [{ name: "disabled-user", path: "C:/skills/user/SKILL.md", description: "User workflow", source_level: "user", enabled: false }], plugin_skills: [], available_skills: [], workspace_root: "" }), { status: 200 }));
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ plugins: [], marketplaces: [] }), { status: 200 })));
    useAppStore.setState({ settingsTab: "plugins" });
    render(<SettingsCenter />);
    const controls = screen.getByRole("navigation", { name: "能力" });
    await waitFor(() => expect(within(controls).getByRole("button", { name: "已安装技能" })).toBeTruthy());
    expect(within(controls).queryByRole("button", { name: "已安装插件" })).toBeNull();
    expect(within(controls).getByRole("button", { name: "MCP 服务" })).toBeTruthy();
    fireEvent.click(within(controls).getByRole("button", { name: "已安装技能" }));
    expect(await screen.findByRole("switch", { name: "启用 disabled-user" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "搜索技能" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "浏览目录" }));
    expect(useAppStore.getState().skillsMarketplaceTab).toBe("skills");
    expect(useAppStore.getState().skillsMarketplaceReturnTarget).toBe("settings");
  });

  it("uses one configured marketplace inventory for its conditional tab, count and search", async () => {
    const markets = [{ name: "git-team", interface: { displayName: "Team tools" }, status: "ready", source: { source: "git", url: "https://test.local/team.git" }, plugins: [], materialized_path: "C:/catalogs/team" },
      { name: "local-team", status: "ready", source: { source: "directory", path: "C:/catalogs/local" }, plugins: [] }];
    vi.stubGlobal("fetch", vi.fn(async input => new Response(JSON.stringify(String(input).endsWith("/marketplaces") ? { marketplaces: markets } : { plugins: [] }), { status: 200 })));
    useAppStore.setState({ settingsTab: "marketplace" });
    render(<SettingsCenter />);
    const tab = await screen.findByRole("button", { name: "插件市场", exact: true });
    expect(tab.textContent).toContain("2");
    expect(tab.getAttribute("aria-pressed")).toBe("true");
    expect(await screen.findByText("Team tools")).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "搜索插件市场" }), { target: { value: "catalogs/team" } });
    expect(screen.queryByText("local-team", { exact: true })).toBeNull();
    expect(tab.textContent).toContain("2");
    await waitFor(() => expect(screen.getByRole("button", { name: "升级插件市场 Team tools" })).toHaveProperty("disabled", false));
  });

  it("keeps MCP available when the real plugins feature is disabled", async () => {
    pluginsFeature.enabled = false;
    vi.stubGlobal("fetch", vi.fn(async input => new Response(JSON.stringify(String(input).endsWith("/marketplaces")
      ? { marketplaces: [{ name: "team", status: "ready", source: { source: "git" }, plugins: [] }] } : { plugins: [] }), { status: 200 })));
    useAppStore.setState({ settingsTab: "marketplace" });
    render(<SettingsCenter />);
    await waitFor(() => expect(useAppStore.getState().settingsTab).toBe("connectors"));
    const controls = screen.getByRole("navigation", { name: "能力" });
    expect(within(controls).getByRole("button", { name: "MCP 服务" })).toBeTruthy();
    expect(within(controls).queryByRole("button", { name: "插件市场" })).toBeNull();
    expect(within(controls).queryByRole("button", { name: "已安装技能" })).toBeNull();
  });

  it("shows the real installed-user-skill settings route and toggles its effective state", async () => {
    useAppStore.setState({
      settingsTab: "skills",
      availableSkills: [{
        name: "code-review",
        display_name: "代码审查",
        description: "审查当前修改",
        source_level: "user",
        path: "C:\\Users\\ago\\.minicode\\skills\\code-review\\SKILL.md",
      }],
    });
    render(<SettingsCenter />);

    expect(screen.getByRole("heading", { name: "插件", level: 2 })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "技能管理", exact: true })).toBeNull();
    expect(screen.queryByText("插件是能力包；技能是工作流；MCP 是外部工具服务。")).toBeNull();
    const toggle = await screen.findByRole("switch", { name: "停用 代码审查" });
    expect(screen.getByRole("button", { name: "已安装技能" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.queryByRole("button", { name: "使用" })).toBeNull();
    fireEvent.click(toggle);
    await screen.findByRole("switch", { name: "启用 代码审查" });
    expect(skillsSettingsRequest).toHaveBeenCalledWith(expect.stringContaining("/api/skills/settings"), expect.objectContaining({ method: "PUT" }));
    expect(useAppStore.getState().availableSkills).toEqual([]);
    expect(useAppStore.getState().selectedSkills).toEqual([]);
    expect(useAppStore.getState().settingsOpen).toBe(true);
  });

  it("keeps host memories available on Home without creating a conversation", async () => {
    useAppStore.setState({ conversationId: null, conversations: [] });
    render(<SettingsCenter />); fireEvent.click(screen.getByRole("button", { name: "个性化" }));
    fireEvent.click(await screen.findByRole("switch", { name: "启用 MiniCode 记忆" }));
    await waitFor(() => expect(memoryFixture.calls.at(-1)?.body).toEqual({ enabled: false }));
    expect(useAppStore.getState().conversationId).toBeNull();
  });

  it("requires confirmation before deleting memories on the selected host", async () => {
    render(<SettingsCenter />); fireEvent.click(screen.getByRole("button", { name: "个性化" }));
    fireEvent.click(await screen.findByRole("button", { name: "删除", exact: true }));
    const dialog = screen.getByRole("dialog", { name: "删除全部 MiniCode 记忆？" });
    expect(memoryFixture.calls).toEqual([]);
    fireEvent.click(within(dialog).getByRole("button", { name: "删除", exact: true }));
    await waitFor(() => expect(memoryFixture.calls).toContainEqual({ url: "http://test.local/api/settings/memory/reset", body: { confirmed: true } }));
    expect(awaitCommandResultMock).not.toHaveBeenCalledWith({ type: "memory.reset", confirmed: true });
  });

  it("changes the host external-context preference without clearing a task's recorded pollution", async () => {
    memoryFixture.value.memories.disable_on_external_context = true;
    useAppStore.setState({ conversationId: "conv-polluted", conversations: [{ id: "conv-polluted", title: "External context", updatedAt: "", memoryMode: "polluted", memoryPolluted: true, memoryPollutionSources: ["web_search"] }] });
    render(<SettingsCenter />); fireEvent.click(screen.getByRole("button", { name: "个性化" }));
    fireEvent.click(await screen.findByRole("switch", { name: "允许从使用工具的聊天中生成记忆" }));
    await waitFor(() => expect(memoryFixture.calls.at(-1)?.body).toEqual({ disable_on_external_context: false }));
    expect(useAppStore.getState().conversations[0]).toMatchObject({ memoryMode: "polluted", memoryPolluted: true, memoryPollutionSources: ["web_search"] });
    expect(screen.queryByRole("button", { name: "重新启用" })).toBeNull();
  });

  it("opens the Source local environment project list without extra diagnostic controls", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ projects: [{ path: "C:/demo", name: "Demo" }] }), { status: 200 })));
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "环境" }));
    expect(await screen.findByRole("button", { name: "Demo" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "打开运行状态" })).toBeNull();
    expect(screen.queryByText("运行环境")).toBeNull();
    expect(useAppStore.getState().settingsOpen).toBe(true);
  });

  it("shows Source Git preferences without adding workspace or login launchers", async () => {
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "Git" }));
    expect(await screen.findByRole("textbox", { name: "分支前缀" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "打开 Git 工作区" })).toBeNull();
    expect(screen.getByRole("button", { name: "已归档聊天" })).toBeTruthy();
    expect(useAppStore.getState().settingsOpen).toBe(true);
  });

  it("updates the real model browser-control setting", async () => {
    desktopModeMock.mockReturnValue(true);
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "浏览器" }));
    fireEvent.click(await screen.findByRole("switch", { name: "让 MiniCode 控制内置浏览器" }));
    await waitFor(() => expect(embeddedBrowserSetSettingsMock).toHaveBeenCalledWith({ browserEnabled: false }));
  });

  it("keeps the local environment editor draft across settings pages", async () => {
    const configPath = "C:/demo/.minicode/environments/environment.toml";
    const environment = { version: 1, name: "Demo", setup: { script: "npm install" }, cleanup: { script: "" }, actions: [] };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      const payload = url.searchParams.has("config_path")
        ? { config_path: configPath, revision: "saved", environment }
        : url.searchParams.has("workspace_root")
          ? { configs: [{ config_path: configPath, revision: "saved", environment }], default_config_path: configPath, selected_config_path: configPath, worktree_path: "" }
          : { projects: [{ path: "C:/demo", name: "Demo" }] };
      return new Response(JSON.stringify(payload), { status: 200 });
    }));
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "环境" }));
    fireEvent.click(await screen.findByRole("button", { name: "Demo" }));
    fireEvent.click(await screen.findByRole("button", { name: "编辑环境 Demo" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "环境名称" }), { target: { value: "Unsaved environment" } });
    fireEvent.click(screen.getByRole("button", { name: "外观" }));
    fireEvent.click(screen.getByRole("button", { name: "环境" }));
    expect((screen.getByRole("textbox", { name: "环境名称" }) as HTMLInputElement).value).toBe("Unsaved environment");
  });

  it("reports environment read failure without rendering unrelated global secrets", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Environment read denied", { status: 403 })));
    useAppStore.setState({ envVars: [{ name: "DEMO_TOKEN", description: "Demo", scope: "global" }] });
    render(<SettingsCenter />);

    fireEvent.click(screen.getByRole("button", { name: "环境" }));

    expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("Environment read denied"));
    expect(screen.queryByText("DEMO_TOKEN")).toBeNull();
    expect(awaitCommandResultMock).not.toHaveBeenCalledWith(expect.objectContaining({ type: "env.list" }));
  });

  it("preserves browser-control state when desktop saving fails", async () => {
    desktopModeMock.mockReturnValue(true);
    embeddedBrowserSetSettingsMock.mockRejectedValueOnce(new Error("disk failed"));
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "浏览器" }));
    fireEvent.click(await screen.findByRole("switch", { name: "让 MiniCode 控制内置浏览器" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("switch", { name: "让 MiniCode 控制内置浏览器" }).getAttribute("aria-checked")).toBe("true");
  });

  it("opens the Source browser-history subpage", async () => {
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "浏览器" }));
    fireEvent.click(screen.getByRole("button", { name: "管理浏览历史" }));
    await waitFor(() => expect(useAppStore.getState().browserSettingsSection).toBe("history"));
    expect(screen.getByRole("textbox", { name: "搜索浏览历史" })).toBeTruthy();
  });

  it("keeps unsupported workspace launchers out of Browser settings", () => {
    render(<SettingsCenter />);
    fireEvent.click(screen.getByRole("button", { name: "浏览器" }));
    expect(screen.queryByRole("button", { name: "打开浏览器" })).toBeNull();
    expect(screen.queryByText("浏览器工作区")).toBeNull();
    expect(screen.queryByText("浏览器下载策略")).toBeNull();
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

    fireEvent.click(screen.getByRole("button", { name: "已归档聊天" }));
    fireEvent.click(await screen.findByRole("button", { name: "恢复 旧任务" }));

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

    fireEvent.keyDown(screen.getByRole("main", { name: "设置" }), { key: "Escape" });

    expect(useAppStore.getState().settingsOpen).toBe(false);
  });

  it("hydrates the Models tab from saved provider settings", async () => {
    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    act(() => useAppStore.getState().setSettingsTab("provider"));
    fireEvent.click(screen.getByRole("button", { name: "编辑 DeepSeek" }));

    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "runtime.capabilities.inspect",
      source: "settings.provider",
    }, { silent: true });
    expect(screen.getByDisplayValue("https://api.deepseek.com/v1")).toBeTruthy();
    // The saved model shows in two controls: the default-model select and the
    // per-model request-id mapping row. Assert each by its own label.
    expect((screen.getByLabelText("默认模型") as HTMLSelectElement).value).toBe("deepseek-v4-flash");
    expect((screen.getByLabelText("deepseek-v4-flash 实际请求模型") as HTMLInputElement).value).toBe("deepseek-v4-flash");
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

  it("keeps ordinary Configuration separate from Provider and internal gates while retaining the existing model-settings route and draft", async () => {
    const fields = { approval_policy: "on-request", sandbox_mode: "read-only", network_access: false, web_search: "cached", model_verbosity: "medium", model_reasoning_summary: "auto" };
    const config = { path: "C:/settings.json", ...fields, effective: fields, origins: {}, warnings: [], requirements: {}, default_scope_id: "user",
      scopes: [{ id: "user", kind: "user", path: "C:/settings.json", workspace_root: null, disabled_reason: null, version: "v1", readonly: false, values: {}, source: { type: "user", display: "User" } }] };
    const existingFetch = vi.mocked(fetch).getMockImplementation()!;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => new URL(String(input)).pathname === "/api/settings/agent-config"
      ? Response.json(config) : existingFetch(input, init));
    vi.stubGlobal("fetch", fetchMock);
    fetchLLMSettingsMock.mockClear();
    useAppStore.setState({ settingsTab: "config", availableModelOptions: [], draft: "Retained current conversation input" });
    render(<SettingsCenter />);
    await screen.findByRole("button", { name: "配置作用域，当前：用户" });
    expect(fetchLLMSettingsMock).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/settings/feature-flags"))).toBe(false);
    expect(screen.queryByRole("button", { name: "编辑 DeepSeek" })).toBeNull();
    expect(within(screen.getByRole("navigation", { name: "设置分类" })).queryByRole("button", { name: "模型与 API" })).toBeNull();
    expect(screen.queryByText("响应式上下文压缩")).toBeNull();
    act(() => openSettings("provider"));
    fireEvent.click(await screen.findByRole("button", { name: "编辑 DeepSeek" }));
    const endpoint = screen.getByDisplayValue("https://api.deepseek.com/v1") as HTMLInputElement;
    fireEvent.change(endpoint, { target: { value: "https://retained-provider.invalid/v1" } });
    act(() => openSettings("config"));
    expect(endpoint.isConnected).toBe(true); expect(endpoint.closest("[hidden]")).toBeTruthy();
    act(() => openSettings("provider"));
    expect(screen.getByDisplayValue("https://retained-provider.invalid/v1")).toBe(endpoint);
    expect(useAppStore.getState().draft).toBe("Retained current conversation input");
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
    act(() => useAppStore.getState().setSettingsTab("provider"));
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
    act(() => useAppStore.getState().setSettingsTab("provider"));
    fireEvent.click(screen.getByRole("button", { name: "编辑 DeepSeek" }));

    expect(screen.queryByText("Provider Capabilities")).toBeNull();
    expect(screen.queryByText("Tool calling")).toBeNull();
  });

  it("restores a provider from recent configuration history", async () => {
    render(<SettingsCenter />);

    await waitFor(() => expect(fetchLLMSettingsMock).toHaveBeenCalled());
    act(() => useAppStore.getState().setSettingsTab("provider"));
    fireEvent.click(screen.getByRole("button", { name: "编辑 OpenRouter" }));

    expect(screen.getByDisplayValue("https://openrouter.ai/api/v1")).toBeTruthy();
    expect((screen.getByLabelText("默认模型") as HTMLSelectElement).value).toBe("anthropic/claude-sonnet-4");
    expect((screen.getByLabelText("anthropic/claude-sonnet-4 实际请求模型") as HTMLInputElement).value).toBe("anthropic/claude-sonnet-4");
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
    act(() => useAppStore.getState().setSettingsTab("provider"));

    expect(screen.getByRole("button", { name: "api.bbe.to 已启用" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "编辑 api.bbe.to" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "编辑 自定义" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "编辑 api.bbe.to" }));
    const displayNameInput = screen.getByLabelText("提供商显示名称") as HTMLInputElement;
    expect(displayNameInput.value).toBe("api.bbe.to");

    fireEvent.change(displayNameInput, { target: { value: "Work gateway" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => new URL(String(input)).pathname === "/api/llm/settings" && init?.method === "PUT")).toBe(true));
    const saveCall = fetchMock.mock.calls.find(([input, init]) => new URL(String(input)).pathname === "/api/llm/settings" && init?.method === "PUT")!;
    const body = JSON.parse(String(saveCall[1]!.body));
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
    act(() => useAppStore.getState().setSettingsTab("provider"));

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
    act(() => useAppStore.getState().setSettingsTab("provider"));

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
    act(() => useAppStore.getState().setSettingsTab("provider"));
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
    act(() => useAppStore.getState().setSettingsTab("provider"));

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

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => new URL(String(input)).pathname === "/api/llm/settings" && init?.method === "PUT")).toBe(true));
    const saveCall = fetchMock.mock.calls.find(([input, init]) => new URL(String(input)).pathname === "/api/llm/settings" && init?.method === "PUT")!;
    const body = JSON.parse(String(saveCall[1]!.body));
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
    act(() => useAppStore.getState().setSettingsTab("provider"));

    fireEvent.click(screen.getByRole("button", { name: /删除已保存的提供商 OpenRouter/i }));
    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => new URL(String(input)).pathname === "/api/llm/provider-history" && init?.method === "DELETE")).toBe(true));
    const [url, init] = fetchMock.mock.calls.find(([input, init]) => new URL(String(input)).pathname === "/api/llm/provider-history" && init?.method === "DELETE")!;
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
    act(() => useAppStore.getState().setSettingsTab("provider"));

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
    act(() => useAppStore.getState().setSettingsTab("provider"));
    fireEvent.click(screen.getByRole("button", { name: "编辑 OpenAI" }));
    fireEvent.change(screen.getByLabelText("API 格式"), { target: { value: "chat" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => new URL(String(input)).pathname === "/api/llm/settings" && init?.method === "PUT")).toBe(true));
    const saveCall = fetchMock.mock.calls.find(([input, init]) => new URL(String(input)).pathname === "/api/llm/settings" && init?.method === "PUT")!;
    const body = JSON.parse(String(saveCall[1]!.body));
    expect(body.openai.wire_api).toBe("chat");
    expect(sendClientCommand).not.toHaveBeenCalledWith({
      type: "runtime.capabilities.inspect",
      source: "settings.provider.save",
    }, { silent: true });
  });

  it("discovers models without running the auth check", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/llm/models/refresh") && init?.method === "POST") {
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
    act(() => useAppStore.getState().setSettingsTab("provider"));
    fireEvent.click(screen.getByRole("button", { name: "编辑 DeepSeek" }));
    fireEvent.click(screen.getByRole("button", { name: "获取模型列表" }));

    await waitFor(() => expect(screen.getByText(/已获取 2 个候选模型/)).toBeTruthy());
    expect(screen.queryByDisplayValue("deepseek-v4-pro")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "添加模型" }));
    const discoveredModelSelect = screen.getByLabelText("__draft_model_1 实际请求模型") as HTMLSelectElement;
    expect(discoveredModelSelect.tagName).toBe("SELECT");
    fireEvent.change(discoveredModelSelect, { target: { value: "deepseek-v4-pro" } });
    expect(screen.getByDisplayValue("deepseek-v4-pro")).toBeTruthy();
    expect(fetchMock.mock.calls.some(([input, init]) => new URL(String(input)).pathname === "/api/llm/models/refresh" && init?.method === "POST")).toBe(true);
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

  it("preserves model capabilities when leaving an unchanged model ID", async () => {
    const initialPayload = await fetchLLMSettingsMock() as LLMSettingsPayload;
    const metadata = { context_window: 128000, reasoning_effort_levels: ["low", "high"], source: "provider" };
    initialPayload.provider_history![0].model_metadata = { "deepseek-v4-flash": metadata };
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(initialPayload)));
    vi.stubGlobal("fetch", fetchMock);
    render(<ProviderTab selectedProvider="custom" settingsPayload={initialPayload} settingsPayloadRef={{ current: initialPayload }} onProviderChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 DeepSeek" }));

    const modelInput = screen.getByRole("textbox", { name: "deepseek-v4-flash 实际请求模型" });
    fireEvent.focus(modelInput);
    fireEvent.blur(modelInput);
    expect((screen.getByRole("spinbutton", { name: "deepseek-v4-flash 上下文窗口" }) as HTMLInputElement).value).toBe("128000");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(JSON.parse(String(init.body)).custom.model_metadata["deepseek-v4-flash"]).toEqual(metadata);
  });

  it("keeps discovered capabilities in the draft until an explicit save", async () => {
    const initialPayload = await fetchLLMSettingsMock() as LLMSettingsPayload;
    initialPayload.provider_history![0].model_metadata = { "deepseek-v4-flash": { context_window: 128000 } };
    const before = structuredClone(initialPayload);
    const payloadRef = { current: initialPayload };
    const onChange = vi.fn();
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      source: "live", models: ["deepseek-v4-flash"], selected_model: "deepseek-v4-flash", model_metadata: {},
    })));
    vi.stubGlobal("fetch", fetchMock);
    render(<ProviderTab selectedProvider="custom" settingsPayload={initialPayload} settingsPayloadRef={payloadRef} onProviderChange={vi.fn()} onSettingsPayloadChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 DeepSeek" }));
    expect((screen.getByRole("spinbutton", { name: "deepseek-v4-flash 上下文窗口" }) as HTMLInputElement).value).toBe("128000");
    fireEvent.click(screen.getByRole("button", { name: "获取模型列表" }));

    await screen.findByText("已获取 1 个候选模型");
    expect((screen.getByRole("spinbutton", { name: "deepseek-v4-flash 上下文窗口" }) as HTMLInputElement).value).toBe("");
    expect(payloadRef.current).toEqual(before);
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "返回提供商列表" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 DeepSeek" }));
    expect((screen.getByRole("spinbutton", { name: "deepseek-v4-flash 上下文窗口" }) as HTMLInputElement).value).toBe("");
    expect(payloadRef.current).toEqual(before);
    expect(onChange).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each(["接口地址", "API 密钥"])("serializes model checks and clears their result when %s changes", async (field) => {
    const initialPayload = await fetchLLMSettingsMock() as LLMSettingsPayload;
    let completeCheck!: (response: Response) => void;
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => { completeCheck = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ProviderTab selectedProvider="custom" settingsPayload={initialPayload} settingsPayloadRef={{ current: initialPayload }} onProviderChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 DeepSeek" }));
    fireEvent.click(screen.getByRole("button", { name: "鉴权" }));

    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "返回提供商列表" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("textbox", { name: field }) as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => { completeCheck(new Response(JSON.stringify({ ok: true }))); });
    expect(await screen.findByRole("button", { name: "已通过" })).toBeTruthy();

    fireEvent.change(screen.getByRole("textbox", { name: field }), { target: { value: field === "接口地址" ? "https://new.example/v1" : "changed-test-key" } });
    expect(screen.queryByRole("button", { name: "已通过" })).toBeNull();
    expect(screen.getByRole("button", { name: "鉴权" })).toBeTruthy();
  });

  it("drops old model capabilities when the actual model ID changes", async () => {
    const initialPayload = await fetchLLMSettingsMock() as LLMSettingsPayload;
    initialPayload.provider_history![0].model_metadata = { "deepseek-v4-flash": { context_window: 128000 } };
    render(<ProviderTab selectedProvider="custom" settingsPayload={initialPayload} settingsPayloadRef={{ current: initialPayload }} onProviderChange={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 DeepSeek" }));
    const modelInput = screen.getByRole("textbox", { name: "deepseek-v4-flash 实际请求模型" });
    fireEvent.change(modelInput, { target: { value: "another-model" } });
    fireEvent.blur(modelInput);
    expect((screen.getByRole("spinbutton", { name: "another-model 上下文窗口" }) as HTMLInputElement).value).toBe("");
    expect(screen.queryByRole("textbox", { name: "deepseek-v4-flash 实际请求模型" })).toBeNull();
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

    const mappingValue = (modelId: string) =>
      (screen.getByLabelText(`${modelId} 实际请求模型`) as HTMLInputElement).value;
    expect((screen.getByLabelText("默认模型") as HTMLSelectElement).value).toBe("mimo-v2.5-pro");
    expect(mappingValue("mimo-v2.5-pro")).toBe("mimo-v2.5-pro");
    expect(mappingValue("gpt-5.5")).toBe("gpt-5.5");
    expect(mappingValue("gpt-5.4")).toBe("gpt-5.4");

    fireEvent.click(screen.getByRole("button", { name: "获取模型列表" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.getByText(/Provider 返回了空模型列表/)).toBeTruthy();
    expect(screen.getByText(/已保留手动输入的模型/)).toBeTruthy();
    expect(mappingValue("gpt-5.5")).toBe("gpt-5.5");
    expect(mappingValue("gpt-5.4")).toBe("gpt-5.4");
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
    act(() => useAppStore.getState().setSettingsTab("provider"));
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
    act(() => useAppStore.getState().setSettingsTab("provider"));
    fireEvent.click(screen.getByRole("button", { name: "使用 OpenAI" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await waitFor(() => expect(awaitCommandResultMock).toHaveBeenCalledWith(expect.objectContaining({
      type: "llm.config.set",
      provider: "openai",
      source: "settings.provider.activate",
    })));
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
      workingDirectory: "C:/A",
      isConnected: true,
      conversations: [{ id: "conv-memory", title: "Memory", updatedAt: "2026-08-02T00:00:00.000Z" }],
    });
    render(<SettingsCenter />);

    useAppStore.setState({ settingsOpen: true });

    await waitFor(() => expect(screen.getByText("暂无定时任务")).toBeTruthy());
    expect(fetchLLMSettingsMock).not.toHaveBeenCalled();
    expect(awaitCommandResultMock).toHaveBeenCalledWith({
      type: "scheduler.list",
      owner_conversation_id: "conv-memory",
      workspace_root: "C:/A",
    });
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
    act(() => useAppStore.getState().setSettingsTab("features"));
    await waitFor(() => expect(screen.getByRole("button", { name: /^界面/ })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /^界面/ }));
    await waitFor(() => expect(screen.getByText("全局搜索")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: /^覆盖 全局搜索，当前：/ }));
    fireEvent.click(screen.getByRole("option", { name: "关闭" }));

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
    expect(screen.getByRole("button", { name: "覆盖 全局搜索，当前：关闭" })).toBeTruthy();
  });

  it("shows local plugins and toggles plugin enablement from settings", async () => {
    let pluginEnabled = true;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.endsWith("/api/plugins/marketplaces")) return new Response(JSON.stringify({ marketplaces: [] }), { status: 200 });
      if (url.split("?")[0].endsWith("/api/plugins") && (!init?.method || init.method === "GET")) {
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
              enabled: pluginEnabled,
            },
          ],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.includes("/api/plugins/demo-plugin/state") && init?.method === "PUT") {
        pluginEnabled = JSON.parse(String(init.body)).enabled;
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
    expect(screen.queryByText("1 个技能")).toBeNull();

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
    await waitFor(() => expect(screen.getByRole("switch", { name: "启用插件 demo-plugin" }).getAttribute("aria-checked")).toBe("false"));
  });

  it("adds a local marketplace and installs its canonical catalog plugin without a custom developer panel", async () => {
    let registered = false, installed = false;
    const market = { name: "local-team", status: "ready", source: { source: "directory", path: "C:/team-market" },
      materialized_path: "C:/cached/team", plugins: [{ id: "review@local-team", name: "review", description: "Local review workflow" }] };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/marketplaces") && init?.method === "POST") {
        registered = true;
        return new Response(JSON.stringify({ marketplace: market, already_added: false }), { status: 200 });
      }
      if (url.pathname.endsWith("/marketplaces")) return new Response(JSON.stringify({ marketplaces: registered ? [market] : [] }), { status: 200 });
      if (url.pathname.endsWith("/install")) {
        installed = true;
        return new Response(JSON.stringify({ plugins: [], runtime_refresh: { ok: true } }), { status: 200 });
      }
      return new Response(JSON.stringify({ plugins: installed ? [{ id: "review@local-team", name: "review", marketplace: "local-team", path: "C:/plugins/review", enabled: true }] : [] }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    useAppStore.setState({ settingsTab: "connectors" });
    render(<><SettingsCenter /><SkillsMarketplace /></>);
    fireEvent.click(screen.getByRole("button", { name: "添加", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "添加插件市场" }));
    fireEvent.change(screen.getByRole("textbox", { name: "来源", exact: true }), { target: { value: "C:/team-market" } });
    fireEvent.click(screen.getByRole("button", { name: "添加市场", exact: true }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(false);
    expect(await screen.findByRole("button", { name: "插件市场", exact: true })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "浏览目录" }));
    const install = await screen.findByRole("button", { name: "安装插件 review@local-team" });
    await waitFor(() => expect(install).toHaveProperty("disabled", false));
    fireEvent.click(install);
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => String(input).endsWith("/install") && init?.method === "POST")).toBe(true));
    const add = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/marketplaces") && init?.method === "POST")!;
    expect(JSON.parse(String(add[1]?.body))).toEqual({ source: "C:/team-market", ref_name: null, sparse_paths: null });
    const installation = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith("/install") && init?.method === "POST")!;
    expect(JSON.parse(String(installation[1]?.body))).toEqual({ plugin_name: "review", marketplace: "local-team", refresh_marketplace: false });
    expect(screen.queryByRole("textbox", { name: "插件文件夹或安装包路径" })).toBeNull();
    expect(screen.queryByRole("button", { name: "打包", exact: true })).toBeNull();
    expect(fetchMock.mock.calls.some(([input]) => /\/(?:import|validate|package)$/.test(String(input)))).toBe(false);
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
