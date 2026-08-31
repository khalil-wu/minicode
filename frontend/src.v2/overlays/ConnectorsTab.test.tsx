/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { ConnectorsTab } from "./ConnectorsTab";

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
    awaitCommandResult.mockResolvedValue({ type: "command.result", command: "mcp.add", level: "info", message: "", data: {} });
    useAppStore.setState({
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

  it("surfaces auth required / failed / reconnecting phases and the action hint", () => {
    render(<ConnectorsTab />);

    expect(screen.getByText("需要登录")).toBeTruthy();
    expect(screen.getByText("未登录")).toBeTruthy();
    expect(screen.getByText("失败")).toBeTruthy();
    expect(screen.getByText("正在重连")).toBeTruthy();
    // requires_user_action surfaces a compact hint, not a big card.
    expect(screen.getByText("需要处理")).toBeTruthy();
  });

  it("shows configured MCP services without a static marketplace", () => {
    render(<ConnectorsTab />);

    expect(screen.getByText("MCP 服务")).toBeTruthy();
    expect(screen.queryByRole("tablist", { name: "MCP 视图" })).toBeNull();
    expect(screen.queryByText(/^市场/)).toBeNull();
  });

  it("starts OAuth only from the explicit login action and waits for completion", async () => {
    render(<ConnectorsTab />);

    fireEvent.click(screen.getByRole("button", { name: "登录 needs-login" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.oauth.login",
      name: "needs-login",
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

    expect(screen.getByText("连接中")).toBeTruthy();
    expect(screen.getByText("正在连接…")).toBeTruthy();
  });

  it("shows verified OAuth state only when the runtime reports it", () => {
    useAppStore.setState({
      mcpServers: [{ name: "linear", status: "connected", phase: "connected", authStatus: "oauth", tools: 4 }],
    });

    render(<ConnectorsTab />);

    expect(screen.getByText("OAuth 已登录")).toBeTruthy();
    expect(screen.getByText("4 个工具")).toBeTruthy();
  });

  it("preserves quoted stdio arguments when adding an MCP server", async () => {
    useAppStore.setState({ mcpServers: [], marketplaceConnectors: [] });
    render(<ConnectorsTab />);

    fireEvent.change(screen.getByPlaceholderText("服务名称"), { target: { value: "quoted" } });
    fireEvent.change(screen.getByPlaceholderText("命令（python、npx、uvx…）"), { target: { value: "node" } });
    fireEvent.change(screen.getByPlaceholderText("参数"), { target: { value: '--flag "two words" --path="C:\\Program Files\\tool"' } });
    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.add",
      name: "quoted",
      transport: "stdio",
      command: "node",
      args: ["--flag", "two words", "--path=C:\\Program Files\\tool"],
      auto_start: true,
    }, "mcp.add", { timeoutMs: 300_000 }));
    await waitFor(() => expect((screen.getByPlaceholderText("服务名称") as HTMLInputElement).value).toBe(""));
  });

  it("edits an existing server with cwd, env, pass-through, and auto-start", async () => {
    useAppStore.setState({
      marketplaceConnectors: [],
      mcpServers: [{
        name: "docs",
        status: "connected",
        phase: "connected",
        transport: "stdio",
        command: "node",
        args: ["server.js"],
        cwd: "C:\\workspace",
        env: { TOKEN: "secret" },
        envVars: [{ name: "HOME_ALIAS", source: "USERPROFILE" }],
        autoStart: true,
        editable: true,
      }],
    });
    render(<ConnectorsTab />);

    fireEvent.click(screen.getByRole("button", { name: "编辑 docs" }));
    expect((screen.getByRole("textbox", { name: "工作目录" }) as HTMLInputElement).value).toBe("C:\\workspace");
    fireEvent.click(screen.getByRole("switch", { name: "自动启动 MCP 服务" }));
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.update",
      original_name: "docs",
      name: "docs",
      transport: "stdio",
      command: "node",
      args: ["server.js"],
      cwd: "C:\\workspace",
      env: { TOKEN: "secret" },
      env_vars: [{ name: "HOME_ALIAS", source: "USERPROFILE" }],
      auto_start: false,
    }, "mcp.update", { timeoutMs: 300_000 }));
  });

  it("emits an exact SSE payload with headers, helper, and OAuth only", async () => {
    useAppStore.setState({ mcpServers: [], marketplaceConnectors: [] });
    render(<ConnectorsTab />);

    fireEvent.change(screen.getByRole("textbox", { name: "服务名称" }), { target: { value: "remote-sse" } });
    fireEvent.change(screen.getByLabelText("传输方式"), { target: { value: "sse" } });
    fireEvent.change(screen.getByRole("textbox", { name: "服务地址" }), { target: { value: "https://mcp.example/sse" } });
    fireEvent.click(screen.getByRole("button", { name: "添加请求头" }));
    fireEvent.change(screen.getByRole("textbox", { name: "请求头名称" }), { target: { value: "X-Region" } });
    fireEvent.change(screen.getByRole("textbox", { name: "X-Region的值" }), { target: { value: "cn" } });
    fireEvent.change(screen.getByRole("textbox", { name: "动态请求头助手" }), { target: { value: "node headers.js" } });
    fireEvent.change(screen.getByRole("textbox", { name: "OAuth 客户端 ID" }), { target: { value: "client-1" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "OAuth 回调端口" }), { target: { value: "4545" } });
    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.add",
      name: "remote-sse",
      transport: "sse",
      url: "https://mcp.example/sse",
      headers: { "X-Region": "cn" },
      headers_helper: "node headers.js",
      oauth: { client_id: "client-1", callback_port: 4545 },
      auto_start: true,
    }, "mcp.add", { timeoutMs: 300_000 }));
  });

  it("emits exact Streamable HTTP and WebSocket payloads without stale fields", async () => {
    useAppStore.setState({ mcpServers: [], marketplaceConnectors: [] });
    const { unmount } = render(<ConnectorsTab />);

    fireEvent.change(screen.getByRole("textbox", { name: "服务名称" }), { target: { value: "modern-http" } });
    fireEvent.change(screen.getByLabelText("传输方式"), { target: { value: "http" } });
    fireEvent.change(screen.getByRole("textbox", { name: "服务地址" }), { target: { value: "https://mcp.example/mcp" } });
    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.add",
      name: "modern-http",
      transport: "http",
      url: "https://mcp.example/mcp",
      auto_start: true,
    }, "mcp.add", { timeoutMs: 300_000 }));

    unmount();
    vi.clearAllMocks();
    awaitCommandResult.mockResolvedValue({ type: "command.result", command: "mcp.add", level: "info", message: "", data: {} });
    render(<ConnectorsTab />);
    fireEvent.change(screen.getByRole("textbox", { name: "服务名称" }), { target: { value: "socket" } });
    fireEvent.change(screen.getByLabelText("传输方式"), { target: { value: "ws" } });
    fireEvent.change(screen.getByRole("textbox", { name: "服务地址" }), { target: { value: "wss://mcp.example/ws" } });
    fireEvent.click(screen.getByRole("button", { name: "添加请求头" }));
    fireEvent.change(screen.getByRole("textbox", { name: "请求头名称" }), { target: { value: "Authorization" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Authorization的值" }), { target: { value: "Bearer token" } });
    expect(screen.queryByRole("textbox", { name: "OAuth 客户端 ID" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.add",
      name: "socket",
      transport: "ws",
      url: "wss://mcp.example/ws",
      headers: { Authorization: "Bearer token" },
      auto_start: true,
    }, "mcp.add", { timeoutMs: 300_000 }));
  });

  it("clears incompatible editor state when switching transports", () => {
    useAppStore.setState({ mcpServers: [], marketplaceConnectors: [] });
    render(<ConnectorsTab />);

    fireEvent.change(screen.getByRole("textbox", { name: "启动命令" }), { target: { value: "node" } });
    fireEvent.change(screen.getByRole("textbox", { name: "命令参数" }), { target: { value: "server.js" } });
    fireEvent.change(screen.getByRole("textbox", { name: "工作目录" }), { target: { value: "C:\\tools" } });
    fireEvent.change(screen.getByLabelText("传输方式"), { target: { value: "http" } });
    expect((screen.getByRole("textbox", { name: "服务地址" }) as HTMLInputElement).value).toBe("");

    fireEvent.change(screen.getByRole("textbox", { name: "服务地址" }), { target: { value: "https://mcp.example/mcp" } });
    fireEvent.change(screen.getByRole("textbox", { name: "动态请求头助手" }), { target: { value: "node headers.js" } });
    fireEvent.change(screen.getByRole("textbox", { name: "OAuth 客户端 ID" }), { target: { value: "client" } });
    fireEvent.change(screen.getByLabelText("传输方式"), { target: { value: "stdio" } });
    expect((screen.getByRole("textbox", { name: "启动命令" }) as HTMLInputElement).value).toBe("");
    expect((screen.getByRole("textbox", { name: "命令参数" }) as HTMLInputElement).value).toBe("");
    expect((screen.getByRole("textbox", { name: "工作目录" }) as HTMLInputElement).value).toBe("");

    fireEvent.change(screen.getByLabelText("传输方式"), { target: { value: "http" } });
    expect((screen.getByRole("textbox", { name: "服务地址" }) as HTMLInputElement).value).toBe("");
    expect((screen.getByRole("textbox", { name: "动态请求头助手" }) as HTMLInputElement).value).toBe("");
    expect((screen.getByRole("textbox", { name: "OAuth 客户端 ID" }) as HTMLInputElement).value).toBe("");
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

    expect(screen.getByText("策略禁用")).toBeTruthy();
    expect(screen.getByText(/Disabled by MiniCode MCP requirements/)).toBeTruthy();
    const toggle = screen.getByRole("switch", { name: "停用 managed-docs" }) as HTMLButtonElement;
    expect(toggle.disabled).toBe(true);
    fireEvent.click(toggle);
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });

  it("keeps MCP form values when the backend rejects the server", async () => {
    awaitCommandResult.mockResolvedValueOnce({ type: "command.result", command: "mcp.add", level: "error", message: "invalid", data: {} });
    useAppStore.setState({ mcpServers: [], marketplaceConnectors: [] });
    render(<ConnectorsTab />);

    fireEvent.change(screen.getByPlaceholderText("服务名称"), { target: { value: "broken" } });
    fireEvent.change(screen.getByPlaceholderText("命令（python、npx、uvx…）"), { target: { value: "node" } });
    fireEvent.click(screen.getByRole("button", { name: "添加服务" }));

    await waitFor(() => expect(awaitCommandResult).toHaveBeenCalled());
    expect((screen.getByPlaceholderText("服务名称") as HTMLInputElement).value).toBe("broken");
    expect((screen.getByPlaceholderText("命令（python、npx、uvx…）") as HTMLInputElement).value).toBe("node");
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

    fireEvent.click(screen.getByRole("button", { name: "重启 docs" }));
    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.restart",
      name: "docs",
    }, "mcp.restart", { timeoutMs: 300_000 }));

    fireEvent.click(screen.getByRole("button", { name: "删除 docs" }));
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
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 docs" }));

    await waitFor(() => expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({
      type: "mcp.inventory.list",
      name: "docs",
      operation_id: expect.stringMatching(/^mcp-inventory-/),
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
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 offline" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 oauth" }));

    expect(screen.getByText("服务尚未连接，连接成功后才能读取 MCP 目录。")).toBeTruthy();
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
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 cached" }));
    expect(await screen.findByText("该服务未公开资源、资源模板或提示词。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "收起 MCP 内容 cached" }));
    fireEvent.click(screen.getByRole("button", { name: "查看 MCP 内容 cached" }));

    expect(screen.getByText("该服务未公开资源、资源模板或提示词。")).toBeTruthy();
    expect(sendClientCommandAwaitResult).toHaveBeenCalledTimes(1);
  });
});
