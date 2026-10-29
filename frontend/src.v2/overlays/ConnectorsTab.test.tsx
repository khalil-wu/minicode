/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { ConnectorsTab } from "./ConnectorsTab";
import { handlePeripheralEvent } from "../chat/peripheralEvents";
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

const awaitCommandResult = vi.hoisted(() => vi.fn());
const confirmAction = vi.hoisted(() => vi.fn().mockResolvedValue(true));
const pushToastMock = vi.hoisted(() => vi.fn());
const nativeSandbox = vi.hoisted(() => ({ desktop: vi.fn(), clearData: vi.fn() }));

vi.mock("../desktop/runtime", async original => ({
  ...await original<typeof import("../desktop/runtime")>(), desktop: nativeSandbox.desktop,
}));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(() => true),
  sendClientCommandAwaitResult: awaitCommandResult,
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed",
  LONG_COMMAND_RESULT_TIMEOUT_MS: 300_000,
}));

vi.mock("./DialogService", () => ({ showConfirm: confirmAction }));
vi.mock("./ToastContainer", () => ({ pushToast: pushToastMock }));

describe("ConnectorsTab MCP lifecycle states", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    nativeSandbox.desktop.mockReturnValue(undefined);
    awaitCommandResult.mockResolvedValue({ type: "command.result", command: "mcp.add", level: "info", message: "", data: {} });
    useAppStore.setState({
      conversationId: "mcp-test-owner",
      workingDirectory: "C:/mcp-test-workspace",
      marketplaceConnectors: [],
      mcpServers: [
        {
          name: "needs-login",
          status: "error",
          phase: "auth_required",
          authStatus: "not_logged_in",
          requiresUserAction: true,
          recoverable: false,
          lastError: "Authentication required",
        },
        { name: "down", status: "error", phase: "failed", lastError: "connection refused" },
        { name: "flaky", status: "reconnecting", phase: "reconnecting" },
      ],
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps runtime evidence in each server detail and the list focused on settings and enablement", () => {
    render(<ConnectorsTab />);
    expect(screen.queryByText("Authentication required")).toBeNull();
    expect(screen.queryByRole("button", { name: "重启 down" })).toBeNull();
    expect(screen.queryByRole("button", { name: "刷新 MCP 服务" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "编辑 needs-login" }));
    expect(screen.getByText("需要登录")).toBeTruthy();
    expect(screen.getByText("未登录")).toBeTruthy();
    expect(screen.getByText("Authentication required")).toBeTruthy();
    expect(screen.getByText("需要处理")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 down" }));
    expect(screen.getByText("失败")).toBeTruthy();
    expect(screen.getByText("connection refused")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 flaky" }));
    expect(screen.getByText("正在重连")).toBeTruthy();
  });

  it("shows configured MCP services without a static marketplace", () => {
    render(<ConnectorsTab />);

    expect(screen.getByRole("region", { name: "MCP 服务" })).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "服务名称" })).toBeNull();
    expect(screen.queryByRole("tablist", { name: "MCP 视图" })).toBeNull();
    expect(screen.queryByText(/^市场/)).toBeNull();
  });

  it("groups the backend's actual plugin servers and opens their original runtime evidence", async () => {
    const event = normalizeInboundServerEvent({ type: "mcp_status", servers: [
      { name: "node_repl", status: "connected", source: "user", plugin_id: null, plugin_server_name: null },
      { name: "plugin:review@local:code-review", status: "error", source: "plugin:review@local",
        plugin_id: "review@local", plugin_server_name: "code-review", phase: "failed", error: "actual plugin server error" },
      { name: "cua_repl", status: "connected", source: "builtin:cua_repl:local", plugin_id: "computer-use@local", plugin_server_name: "cua_repl" },
    ] });
    expect(event).toBeTruthy();
    expect(handlePeripheralEvent(event!)).toBe(true);
    render(<ConnectorsTab />);
    const servers = screen.getByRole("region", { name: "MCP 服务" });
    const plugins = screen.getByRole("region", { name: "来自插件" });
    expect(within(servers).getByText("node_repl")).toBeTruthy();
    expect(within(servers).queryByText("code-review")).toBeNull();
    expect(within(plugins).getByText("code-review")).toBeTruthy();
    expect(within(plugins).getByText("cua_repl")).toBeTruthy();
    fireEvent.click(within(plugins).getByRole("button", { name: "编辑 code-review" }));
    expect(screen.getByText("actual plugin server error").style.whiteSpace).toBe("pre-wrap");
    expect(screen.getByRole("heading", { name: "code-review" })).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: "启动命令" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重启 code-review" }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({
      type: "mcp.restart", name: "plugin:review@local:code-review",
    }), "mcp.restart", expect.anything()));
  });

  it("offers app data clearing only with its native sandbox and waits for its actual result", async () => {
    let complete!: () => void;
    nativeSandbox.clearData.mockImplementation(() => new Promise<void>(resolve => { complete = resolve; }));
    nativeSandbox.desktop.mockReturnValue({ mcpAppSandbox: { clearData: nativeSandbox.clearData } });
    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "清除所有应用数据" }));
    expect(nativeSandbox.clearData).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "清除所有应用数据" })).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "清除所有应用数据" }).getAttribute("aria-busy")).toBe("true");
    expect(pushToastMock).not.toHaveBeenCalled();
    await act(async () => complete());
    expect(pushToastMock).toHaveBeenCalledWith("MCP 应用数据已清除", "success");
    expect(screen.getByRole("button", { name: "清除所有应用数据" })).toHaveProperty("disabled", false);
    expect(screen.getByRole("button", { name: "清除所有应用数据" }).getAttribute("aria-busy")).toBe("false");
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("preserves the real app data clearing failure and does not claim success", async () => {
    nativeSandbox.clearData.mockRejectedValueOnce(new Error("partition file busy"));
    nativeSandbox.desktop.mockReturnValue({ mcpAppSandbox: { clearData: nativeSandbox.clearData } });
    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "清除所有应用数据" }));
    await waitFor(() => expect(pushToastMock).toHaveBeenCalledWith("无法清除 MCP 应用数据：partition file busy", "error"));
    expect(pushToastMock).toHaveBeenCalledTimes(1);
  });

  it("does not show an app data action in the browser without a native consumer", () => {
    render(<ConnectorsTab />);
    expect(screen.queryByRole("button", { name: "清除所有应用数据" })).toBeNull();
  });

  it("starts OAuth only from the explicit login action and waits for completion", async () => {
    render(<ConnectorsTab />);

    fireEvent.click(screen.getByRole("button", { name: "编辑 needs-login" }));
    fireEvent.click(screen.getByRole("button", { name: "登录 needs-login" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.oauth.login",
      name: "needs-login",
      conversation_id: "mcp-test-owner",
      workspace_root: "C:/mcp-test-workspace",
    }, "mcp.oauth.login", { timeoutMs: 300_000 }));
  });

  it("shows a compact progress line while connecting", () => {
    useAppStore.setState({
      mcpServers: [
        {
          name: "booting",
          status: "starting",
          phase: "connecting",
          progress: { operation: "connect", message: "Connecting...", status: "running" },
        },
      ],
    });

    render(<ConnectorsTab />);

    fireEvent.click(screen.getByRole("button", { name: "编辑 booting" }));
    expect(screen.getByText("连接中")).toBeTruthy();
    expect(screen.getByText("正在连接…")).toBeTruthy();
  });

  it("shows verified OAuth state only when the runtime reports it", () => {
    useAppStore.setState({
      mcpServers: [{ name: "linear", status: "connected", phase: "connected", authStatus: "oauth", tools: 4 }],
    });

    render(<ConnectorsTab />);

    fireEvent.click(screen.getByRole("button", { name: "编辑 linear" }));
    expect(screen.getByText("OAuth 已登录")).toBeTruthy();
    expect(screen.queryByText("4 个工具")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 linear" }));
    expect(screen.getByText("4 个工具")).toBeTruthy();
  });

  it("submits independent STDIO argument rows and the local process fields", async () => {
    useAppStore.setState({ mcpServers: [] });
    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));
    fireEvent.change(screen.getByRole("textbox", { name: "服务名称" }), { target: { value: "quoted" } });
    fireEvent.change(screen.getByRole("textbox", { name: "启动命令" }), { target: { value: "node" } });
    fireEvent.change(screen.getByRole("textbox", { name: "命令参数 1" }), { target: { value: "two words" } });
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    fireEvent.change(screen.getByRole("textbox", { name: "命令参数 2" }), { target: { value: '--path="C:\\Program Files\\tool"' } });
    fireEvent.change(screen.getByRole("textbox", { name: "环境变量名称" }), { target: { value: "TOKEN" } });
    fireEvent.change(screen.getByRole("textbox", { name: "TOKEN的值" }), { target: { value: "local-secret" } });
    fireEvent.change(screen.getByRole("textbox", { name: "透传变量 1" }), { target: { value: "USERPROFILE" } });
    fireEvent.change(screen.getByRole("textbox", { name: "工作目录" }), { target: { value: "C:\\workspace" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.add", name: "quoted", transport: "stdio", command: "node",
      args: ["two words", '--path="C:\\Program Files\\tool"'], env: { TOKEN: "local-secret" },
      env_vars: ["USERPROFILE"], cwd: "C:\\workspace", auto_start: true,
    }, "mcp.add", { timeoutMs: 300_000 }));
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "服务名称" })).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));
    expect(screen.getByRole("textbox", { name: "服务名称" })).toHaveProperty("value", "");
  });

  it("opens an independent editor on the parent request and returns without losing its draft", () => {
    const onEditorChange = vi.fn();
    useAppStore.setState({ mcpServers: [{ name: "docs", status: "offline" }] });
    const { rerender } = render(<ConnectorsTab editorRequest={0} search="docs" onEditorChange={onEditorChange} />);
    expect(screen.queryByRole("button", { name: "添加服务" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "搜索 MCP 服务器" })).toBeNull();
    rerender(<ConnectorsTab editorRequest={1} search="docs" onEditorChange={onEditorChange} />);
    expect(screen.getByRole("heading", { name: "连接到自定义 MCP" })).toBeTruthy();
    expect(screen.queryByText("docs")).toBeNull();
    expect(screen.queryByRole("switch", { name: "自动启动 MCP 服务" })).toBeNull();
    expect(screen.getByRole("button", { name: "STDIO" }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.change(screen.getByRole("textbox", { name: "服务名称" }), { target: { value: "unfinished" } });
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    expect(screen.getByText("docs")).toBeTruthy();
    rerender(<ConnectorsTab editorRequest={2} search="docs" onEditorChange={onEditorChange} />);
    expect(screen.getByRole("textbox", { name: "服务名称" })).toHaveProperty("value", "unfinished");
    expect(onEditorChange.mock.calls.map(([open]) => open)).toEqual([false, true, false, true]);
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("keeps the editor open until the save receipt", async () => {
    let finish!: (value: unknown) => void;
    awaitCommandResult.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    useAppStore.setState({ mcpServers: [] });
    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));
    fireEvent.change(screen.getByRole("textbox", { name: "服务名称" }), { target: { value: "local-review" } });
    fireEvent.change(screen.getByRole("textbox", { name: "启动命令" }), { target: { value: "node" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(screen.getByRole("textbox", { name: "服务名称" })).toHaveProperty("value", "local-review");
    expect(screen.getByRole("button", { name: "正在保存…" })).toHaveProperty("disabled", true);
    await act(async () => finish({ type: "command.result", command: "mcp.add", level: "success", message: "saved", data: {} }));
    expect(screen.queryByRole("textbox", { name: "服务名称" })).toBeNull();
    expect(screen.getByRole("button", { name: "添加服务" })).toBeTruthy();
  });

  it("does not reopen or notify on unrelated parent renders and preserves the hidden editor draft", () => {
    useAppStore.setState({ mcpServers: [] });
    const changes = vi.fn();
    const { rerender } = render(<div><ConnectorsTab search="" editorRequest={0} onEditorChange={open => changes(open)} /></div>);
    expect(screen.queryByRole("textbox", { name: "服务名称" })).toBeNull();
    rerender(<div><ConnectorsTab search="changed" editorRequest={0} onEditorChange={open => changes(open)} /></div>);
    expect(changes.mock.calls).toEqual([[false]]);
    rerender(<div><ConnectorsTab search="changed" editorRequest={1} onEditorChange={open => changes(open)} /></div>);
    fireEvent.change(screen.getByRole("textbox", { name: "服务名称" }), { target: { value: "hidden-draft" } });
    rerender(<div hidden><ConnectorsTab search="another" editorRequest={1} onEditorChange={open => changes(open)} /></div>);
    rerender(<div><ConnectorsTab search="" editorRequest={1} onEditorChange={open => changes(open)} /></div>);
    expect(screen.getByRole("textbox", { name: "服务名称" })).toHaveProperty("value", "hidden-draft");
    expect(changes.mock.calls).toEqual([[false], [true]]);
  });

  it("preserves the execution environment and remote pass-through source when editing STDIO", async () => {
    useAppStore.setState({ mcpServers: [{
      name: "docs", status: "offline", transport: "stdio", command: "node", args: ["server.js"],
      cwd: "C:\\workspace", env: { TOKEN: "secret" }, envVars: [{ name: "REMOTE_TOKEN", source: "remote" }],
      environmentId: "remote-owner", autoStart: false, editable: true,
    }] });
    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 docs" }));
    expect(screen.queryByRole("switch", { name: "自动启动 MCP 服务" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    fireEvent.change(screen.getByRole("textbox", { name: "命令参数 2" }), { target: { value: "--updated" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.update", original_name: "docs", name: "docs", transport: "stdio", command: "node", args: ["server.js", "--updated"],
      cwd: "C:\\workspace", env: { TOKEN: "secret" }, env_vars: [{ name: "REMOTE_TOKEN", source: "remote" }],
      environment_id: "remote-owner", auto_start: false,
    }, "mcp.update", { timeoutMs: 300_000 }));
  });

  it.each(["sse", "ws"] as const)("preserves existing %s configuration without offering unsupported creation controls", async transport => {
    useAppStore.setState({ mcpServers: [{
      name: "legacy", status: "offline", transport, url: "https://mcp.example/legacy", editable: true,
      headers: { "X-Region": "cn" }, headersHelper: "node headers.js", autoStart: false,
      ...(transport === "sse" ? { oauth: { clientId: "client-1", callbackPort: 4545 } } : {}),
    }] });
    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 legacy" }));
    expect(screen.queryByRole("combobox", { name: "传输方式" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "动态请求头助手" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "OAuth 客户端 ID" })).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "服务地址" }), { target: { value: "https://mcp.example/updated" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.update", original_name: "legacy", name: "legacy", transport,
      url: "https://mcp.example/updated", headers: { "X-Region": "cn" }, headers_helper: "node headers.js", auto_start: false,
      ...(transport === "sse" ? { oauth: { client_id: "client-1", callback_port: 4545 } } : {}),
    }, "mcp.update", { timeoutMs: 300_000 }));
  });

  it("submits HTTP token and both header forms without STDIO fields", async () => {
    useAppStore.setState({ mcpServers: [] });
    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));
    fireEvent.change(screen.getByRole("textbox", { name: "服务名称" }), { target: { value: "modern-http" } });
    fireEvent.change(screen.getByRole("textbox", { name: "启动命令" }), { target: { value: "node" } });
    fireEvent.click(screen.getByRole("button", { name: "Streamable HTTP" }));
    fireEvent.change(screen.getByRole("textbox", { name: "服务地址" }), { target: { value: "https://mcp.example/mcp" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Bearer 令牌环境变量" }), { target: { value: "MCP_TOKEN" } });
    fireEvent.change(screen.getByRole("textbox", { name: "请求头名称" }), { target: { value: "X-Region" } });
    fireEvent.change(screen.getByRole("textbox", { name: "X-Region的值" }), { target: { value: "cn" } });
    fireEvent.change(screen.getByRole("textbox", { name: "环境变量请求头名称" }), { target: { value: "X-Tenant" } });
    fireEvent.change(screen.getByRole("textbox", { name: "X-Tenant的值" }), { target: { value: "TENANT_ID" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.add", name: "modern-http", transport: "http", url: "https://mcp.example/mcp", auto_start: true,
      bearer_token_env_var: "MCP_TOKEN", headers: { "X-Region": "cn" }, env_http_headers: { "X-Tenant": "TENANT_ID" },
    }, "mcp.add", { timeoutMs: 300_000 }));
  });

  it("clears configured credential fields and cwd through the backend deletion contract", async () => {
    useAppStore.setState({ mcpServers: [{
      name: "executor-docs", status: "offline", transport: "http", url: "https://executor.example/mcp",
      environmentId: "remote-owner", enabled: false, bearerTokenEnvVar: "EXECUTOR_TOKEN",
      envHttpHeaders: { "x-tenant": "HOST_TENANT" }, autoStart: false, editable: true,
    }] });
    const { unmount } = render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 executor-docs" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Bearer 令牌环境变量" }), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "删除请求头变量" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.update", original_name: "executor-docs", name: "executor-docs", transport: "http",
      url: "https://executor.example/mcp", auto_start: false, enabled: false, environment_id: "remote-owner",
      bearer_token_env_var: null, env_http_headers: {},
    }, "mcp.update", { timeoutMs: 300_000 }));
    unmount();
    awaitCommandResult.mockClear();
    useAppStore.setState({ mcpServers: [{ name: "local", status: "offline", transport: "stdio", command: "node", cwd: "C:\\old", editable: true }] });
    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 local" }));
    fireEvent.change(screen.getByRole("textbox", { name: "工作目录" }), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.update", original_name: "local", name: "local", transport: "stdio", command: "node", args: [], cwd: "", auto_start: true,
    }, "mcp.update", { timeoutMs: 300_000 }));
  });

  it("retains each transport draft while switching segments", () => {
    useAppStore.setState({ mcpServers: [] });
    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));
    fireEvent.change(screen.getByRole("textbox", { name: "启动命令" }), { target: { value: "node" } });
    fireEvent.change(screen.getByRole("textbox", { name: "命令参数 1" }), { target: { value: "server.js" } });
    fireEvent.change(screen.getByRole("textbox", { name: "工作目录" }), { target: { value: "C:\\tools" } });
    fireEvent.click(screen.getByRole("button", { name: "Streamable HTTP" }));
    fireEvent.change(screen.getByRole("textbox", { name: "服务地址" }), { target: { value: "https://mcp.example/mcp" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Bearer 令牌环境变量" }), { target: { value: "MCP_TOKEN" } });
    fireEvent.click(screen.getByRole("button", { name: "STDIO" }));
    expect(screen.getByRole("textbox", { name: "启动命令" })).toHaveProperty("value", "node");
    expect(screen.getByRole("textbox", { name: "命令参数 1" })).toHaveProperty("value", "server.js");
    expect(screen.getByRole("textbox", { name: "工作目录" })).toHaveProperty("value", "C:\\tools");
    fireEvent.click(screen.getByRole("button", { name: "Streamable HTTP" }));
    expect(screen.getByRole("textbox", { name: "服务地址" })).toHaveProperty("value", "https://mcp.example/mcp");
    expect(screen.getByRole("textbox", { name: "Bearer 令牌环境变量" })).toHaveProperty("value", "MCP_TOKEN");
  });

  it("keeps separate new and existing server drafts when returning to the list", () => {
    useAppStore.setState({ mcpServers: [{ name: "docs", status: "offline", transport: "stdio", command: "node", editable: true }] });
    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));
    fireEvent.change(screen.getByRole("textbox", { name: "服务名称" }), { target: { value: "new-draft" } });
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 docs" }));
    fireEvent.change(screen.getByRole("textbox", { name: "启动命令" }), { target: { value: "uvx" } });
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));
    expect(screen.getByRole("textbox", { name: "服务名称" })).toHaveProperty("value", "new-draft");
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 docs" }));
    expect(screen.getByRole("textbox", { name: "启动命令" })).toHaveProperty("value", "uvx");
  });

  it("disables Save for original values and restores the same baseline with its workspace draft", () => {
    useAppStore.setState({ conversationId: "A", workingDirectory: "C:/A", mcpServers: [
      { name: "docs", status: "offline", command: "node", transport: "stdio", args: ["two words"], editable: true },
    ] });
    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 docs" }));
    expect(screen.getByRole("button", { name: "保存" })).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole("textbox", { name: "启动命令" }), { target: { value: "uvx" } });
    expect(screen.getByRole("button", { name: "保存" })).toHaveProperty("disabled", false);
    fireEvent.change(screen.getByRole("textbox", { name: "启动命令" }), { target: { value: "node" } });
    expect(screen.getByRole("button", { name: "保存" })).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByRole("textbox", { name: "命令参数 1" }), { target: { value: "changed" } });
    act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B" }));
    expect(screen.queryByRole("textbox", { name: "启动命令" })).toBeNull();
    act(() => useAppStore.setState({ conversationId: "A", workingDirectory: "C:/A" }));
    expect(screen.getByRole("textbox", { name: "命令参数 1" })).toHaveProperty("value", "changed");
    expect(screen.getByRole("button", { name: "保存" })).toHaveProperty("disabled", false);
    fireEvent.change(screen.getByRole("textbox", { name: "命令参数 1" }), { target: { value: "two words" } });
    expect(screen.getByRole("button", { name: "保存" })).toHaveProperty("disabled", true);
  });

  it("uses the server switch to persist enabled state", async () => {
    useAppStore.setState({
      marketplaceConnectors: [],
      mcpServers: [{ name: "docs", status: "offline", autoStart: false, editable: true }],
    });
    render(<ConnectorsTab />);

    fireEvent.click(screen.getByRole("switch", { name: "启用 docs" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.toggle",
      name: "docs",
      enabled: true,
    }, "mcp.toggle", { timeoutMs: 300_000 }));
  });

  it("surfaces managed-policy disablement and prevents the start toggle", () => {
    useAppStore.setState({
      marketplaceConnectors: [],
      mcpServers: [{
        name: "managed-docs",
        status: "offline",
        phase: "stopped",
        autoStart: true,
        editable: true,
        enabled: false,
        disabledReason: "Disabled by MiniCode MCP requirements (requirements.toml)",
      }],
    });

    render(<ConnectorsTab />);

    fireEvent.click(screen.getByRole("button", { name: "编辑 managed-docs" }));
    expect(screen.getByText("策略禁用")).toBeTruthy();
    expect(screen.getByText(/Disabled by MiniCode MCP requirements/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    const toggle = screen.getByRole("switch", { name: "停用 managed-docs" }) as HTMLButtonElement;
    expect(toggle.disabled).toBe(true);
    fireEvent.click(toggle);
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("keeps MCP form values when the backend rejects the server", async () => {
    awaitCommandResult.mockResolvedValueOnce({ type: "command.result", command: "mcp.add", level: "error", message: "invalid", data: {} });
    useAppStore.setState({ mcpServers: [], marketplaceConnectors: [] });
    render(<ConnectorsTab />);

    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));
    fireEvent.change(screen.getByRole("textbox", { name: "服务名称" }), { target: { value: "broken" } });
    fireEvent.change(screen.getByRole("textbox", { name: "启动命令" }), { target: { value: "node" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(awaitCommandResult).toHaveBeenCalled());
    expect((screen.getByRole("textbox", { name: "服务名称" }) as HTMLInputElement).value).toBe("broken");
    expect((screen.getByRole("textbox", { name: "启动命令" }) as HTMLInputElement).value).toBe("node");
  });

  it("waits for restart and confirms removal before sending either command", async () => {
    useAppStore.setState({
      marketplaceConnectors: [],
      mcpServers: [{
        name: "docs",
        status: "connected",
        phase: "connected",
        editable: true,
      }],
    });
    render(<ConnectorsTab />);

    fireEvent.click(screen.getByRole("button", { name: "编辑 docs" }));
    fireEvent.click(screen.getByRole("button", { name: "重启 docs" }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.restart",
      name: "docs",
      conversation_id: "mcp-test-owner",
      workspace_root: "C:/mcp-test-workspace",
    }, "mcp.restart", { timeoutMs: 300_000 }));

    fireEvent.click(screen.getByRole("button", { name: "卸载" }));
    await waitFor(() => expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({
      title: "删除 MCP 服务",
      danger: true,
    })));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.remove",
      name: "docs",
    }, "mcp.remove", { timeoutMs: 300_000 }));
  });

  it("does not inspect MCP content until expansion and then renders the standard inventory", async () => {
    useAppStore.setState({
      marketplaceConnectors: [],
      mcpServers: [{
        name: "docs",
        status: "connected",
        phase: "connected",
        capabilities: { resources: true, prompts: true },
      }],
    });
    awaitCommandResult.mockResolvedValueOnce({
      type: "command.result",
      command: "mcp.inventory.list",
      level: "info",
      message: "",
      data: {
        inventory: {
          server_name: "docs",
          capabilities: { resources: true, resources_subscribe: false, resources_list_changed: true, prompts: true },
          resources: [{ uri: "file:///guide.md", name: "Guide", description: "Project guide", mime_type: "text/markdown" }],
          resource_templates: [{ uri_template: "repo://{path}", name: "Repository file", description: "Read a file" }],
          prompts: [{
            name: "review",
            description: "Review a change",
            arguments: [
              { name: "path", description: "File path", required: true },
              { name: "tone", required: false },
            ],
          }],
          empty: false,
        },
      },
    });

    render(<ConnectorsTab />);

    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "编辑 docs" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 docs" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.inventory.list",
      name: "docs",
      operation_id: expect.stringMatching(/^mcp-inventory-/),
      conversation_id: "mcp-test-owner",
      workspace_root: "C:/mcp-test-workspace",
    }, "mcp.inventory.list", { timeoutMs: 75_000 }));
    expect(await screen.findByText("Guide")).toBeTruthy();
    expect(screen.getByText("file:///guide.md")).toBeTruthy();
    expect(screen.getByText("Repository file")).toBeTruthy();
    expect(screen.getByText("repo://{path}")).toBeTruthy();
    expect(screen.getByText("review")).toBeTruthy();
    expect(screen.getByText("参数：path*、tone")).toBeTruthy();
  });

  it("shows an authoritative empty inventory result", async () => {
    useAppStore.setState({
      marketplaceConnectors: [],
      mcpServers: [{ name: "empty", status: "connected", phase: "connected" }],
    });
    awaitCommandResult.mockResolvedValueOnce({
      type: "command.result",
      command: "mcp.inventory.list",
      level: "info",
      message: "",
      data: {
        inventory: {
          server_name: "empty",
          capabilities: { resources: false, resources_subscribe: false, resources_list_changed: false, prompts: false },
          resources: [],
          resource_templates: [],
          prompts: [],
          empty: true,
        },
      },
    });

    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 empty" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 empty" }));

    expect(await screen.findByText("该服务未公开资源、资源模板或提示词。")).toBeTruthy();
  });

  it("does not issue an inventory request for disconnected or login-gated services", () => {
    useAppStore.setState({
      marketplaceConnectors: [],
      mcpServers: [
        { name: "offline", status: "offline", phase: "stopped" },
        { name: "oauth", status: "error", phase: "auth_required", authStatus: "not_logged_in" },
      ],
    });

    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 offline" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 offline" }));
    expect(screen.getByText("服务尚未连接，连接成功后才能读取 MCP 目录。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑 oauth" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 oauth" }));

    expect(screen.getByText("请先完成此 MCP 服务的登录。")).toBeTruthy();
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it.each([
    ["capabilities_unavailable", "ignored", "服务尚未完成 MCP 能力协商，请稍后重试。"],
    ["timeout", "ignored", "读取 MCP 目录超时，请重试。"],
    ["transport_error", "ignored", "MCP 连接已中断，请检查服务状态后重试。"],
    ["authentication_expired", "ignored", "请先完成此 MCP 服务的登录。"],
    ["protocol_error", "MCP protocol error: invalid request", "MCP protocol error: invalid request"],
  ])("projects %s inventory failures into actionable UI text", async (errorCode, message, expected) => {
    useAppStore.setState({
      marketplaceConnectors: [],
      mcpServers: [{ name: "remote", status: "connected", phase: "connected" }],
    });
    awaitCommandResult.mockResolvedValueOnce({
      type: "command.result",
      command: "mcp.inventory.list",
      level: "error",
      message,
      data: { error_code: errorCode, recoverable: true },
    });

    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 remote" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 remote" }));

    expect(await screen.findByText(expected)).toBeTruthy();
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });

  it("retries with a new operation ID after a negotiated-capability failure", async () => {
    useAppStore.setState({
      marketplaceConnectors: [],
      mcpServers: [{ name: "warming", status: "connected", phase: "connected" }],
    });
    awaitCommandResult
      .mockResolvedValueOnce({
        type: "command.result",
        command: "mcp.inventory.list",
        level: "error",
        message: "not initialized",
        data: { error_code: "capabilities_unavailable", recoverable: true },
      })
      .mockResolvedValueOnce({
        type: "command.result",
        command: "mcp.inventory.list",
        level: "info",
        message: "",
        data: {
          inventory: {
            server_name: "warming",
            capabilities: { resources: false, resources_subscribe: false, resources_list_changed: false, prompts: false },
            resources: [],
            resource_templates: [],
            prompts: [],
            empty: true,
          },
        },
      });

    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 warming" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 warming" }));
    expect(await screen.findByText("服务尚未完成 MCP 能力协商，请稍后重试。")).toBeTruthy();
    const firstOperationId = (awaitCommandResult.mock.calls[0][0] as { operation_id: string }).operation_id;

    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    expect(await screen.findByText("该服务未公开资源、资源模板或提示词。")).toBeTruthy();
    const secondOperationId = (awaitCommandResult.mock.calls[1][0] as { operation_id: string }).operation_id;
    expect(secondOperationId).not.toBe(firstOperationId);
  });

  it("cancels a live inventory request when the user collapses the server", async () => {
    let resolveInventory: ((value: unknown) => void) | undefined;
    awaitCommandResult.mockImplementationOnce(() => new Promise((resolve) => {
      resolveInventory = resolve;
    }));
    useAppStore.setState({
      marketplaceConnectors: [],
      mcpServers: [{ name: "slow", status: "connected", phase: "connected" }],
    });

    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 slow" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 slow" }));
    expect(await screen.findByText("正在按需读取 MCP 目录…")).toBeTruthy();
    const operationId = (awaitCommandResult.mock.calls[0][0] as { operation_id: string }).operation_id;

    fireEvent.click(screen.getByRole("button", { name: "收起 MCP 内容 slow" }));

    expect(sendClientCommand).toHaveBeenCalledWith({
      type: "mcp.inventory.cancel",
      name: "slow",
      operation_id: operationId,
    });
    expect(screen.queryByText("正在按需读取 MCP 目录…")).toBeNull();
    resolveInventory?.({
      type: "command.result",
      command: "mcp.inventory.list",
      level: "error",
      message: "cancelled",
      data: { error_code: "cancelled", recoverable: true },
    });
  });

  it("reuses a loaded inventory when collapsing and expanding without a retry", async () => {
    useAppStore.setState({
      marketplaceConnectors: [],
      mcpServers: [{ name: "cached", status: "connected", phase: "connected" }],
    });
    awaitCommandResult.mockResolvedValueOnce({
      type: "command.result",
      command: "mcp.inventory.list",
      level: "info",
      message: "",
      data: {
        inventory: {
          server_name: "cached",
          capabilities: { resources: false, resources_subscribe: false, resources_list_changed: false, prompts: false },
          resources: [],
          resource_templates: [],
          prompts: [],
          empty: true,
        },
      },
    });

    render(<ConnectorsTab />);
    fireEvent.click(screen.getByRole("button", { name: "编辑 cached" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 cached" }));
    expect(await screen.findByText("该服务未公开资源、资源模板或提示词。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "收起 MCP 内容 cached" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 cached" }));

    expect(screen.getByText("该服务未公开资源、资源模板或提示词。")).toBeTruthy();
    expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(1);
  });
});
