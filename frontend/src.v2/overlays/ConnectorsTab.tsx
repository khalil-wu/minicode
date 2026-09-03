import { useState } from "react";
import { Ban, Check, CheckCheck, ChevronDown, ChevronRight, LogIn, LogOut, Pencil, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { parseArgsStringToArgv } from "string-argv";
import { useAppStore } from "../stores";
import {
  commandResultSucceeded,
  LONG_COMMAND_RESULT_TIMEOUT_MS,
  sendClientCommand,
  sendClientCommandAwaitResult,
} from "../protocol/ws-outbox";
import type {
  ClientCommand,
  McpAddCommand,
  McpInventoryPayload,
  McpServerMutationPayload,
  McpTransport,
  McpUpdateCommand,
} from "../protocol/events";
import type { CommandResultEvent } from "../protocol/events";
import { BrandIcon } from "../components/BrandIcon";
import { pushToast } from "./ToastContainer";
import { showConfirm } from "./DialogService";
import { SelectMenu } from "../components/SelectMenu";
import { reportCommandFailure } from "./commandFeedback";
import {
  Section,
  inputStyle,
  primaryActionStyle,
  secondaryActionStyle,
  emptyInlineStyle,
  mcpServerRowStyle,
  mcpNameStyle,
  mcpErrorStyle,
  mcpDotStyle,
  statusChipStyle,
  miniMetaStyle,
  mcpActionBtnStyle,
} from "./settingsShared";

const parseMcpArgs = (value: string): string[] => parseArgsStringToArgv(value).map((arg) => {
  const quotedEquals = arg.match(/^([^=]+=)(["'])(.*)\2$/s);
  return quotedEquals ? `${quotedEquals[1]}${quotedEquals[3]}` : arg;
});

const formatMcpArgs = (args?: string[]): string => (args ?? [])
  .map((arg) => /\s|["']/.test(arg) ? JSON.stringify(arg) : arg)
  .join(" ");

type EnvRow = { id: string; name: string; value: string };
type PassThroughRow = { id: string; name: string; source: string };
type InventoryViewState = {
  expanded: boolean;
  status: "idle" | "loading" | "loaded" | "error";
  operationId?: string;
  data?: McpInventoryPayload;
  error?: string;
};
const rowId = () => `mcp-row-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
const inventoryOperationId = () => `mcp-inventory-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
const MCP_INVENTORY_COMMAND_TIMEOUT_MS = 75_000;

const connectorStatusLabel = (status?: string): string => {
  if (status === "connected") return "已连接";
  if (status === "connecting") return "连接中";
  if (status === "auth_required") return "需要登录";
  if (status === "reconnecting") return "正在重连";
  if (status === "failed" || status === "error") return "失败";
  if (status === "disabled") return "已停用";
  return String(status || "未知").replace(/_/g, " ");
};

const connectorAuthStatusLabel = (status?: string): string | null => {
  if (status === "oauth") return "OAuth 已登录";
  if (status === "not_logged_in") return "未登录";
  return null;
};

const commandFeedback = (result: CommandResultEvent, message: string): string =>
  result.data?.tool_availability === "next_turn" ? `${message}，下条消息可用` : message;

const operationError = (error: unknown): string =>
  error instanceof Error ? error.message : String(error || "未知错误");

const inventoryFromResult = (result: CommandResultEvent): McpInventoryPayload | null => {
  const inventory = result.data?.inventory;
  if (!inventory || typeof inventory !== "object") return null;
  const value = inventory as Partial<McpInventoryPayload>;
  if (!value.server_name || !Array.isArray(value.resources) || !Array.isArray(value.resource_templates) || !Array.isArray(value.prompts)) {
    return null;
  }
  return value as McpInventoryPayload;
};

const inventoryFailureMessage = (result: CommandResultEvent): string => {
  const code = String(result.data?.error_code || "");
  if (code === "server_not_found") return "该 MCP 服务已不存在，请刷新服务列表。";
  if (code === "not_connected") return "服务尚未连接，连接成功后才能读取 MCP 目录。";
  if (code === "capabilities_unavailable") return "服务尚未完成 MCP 能力协商，请稍后重试。";
  if (code === "authentication_required" || code === "authentication_expired") {
    return "请先完成此 MCP 服务的登录。";
  }
  if (code === "timeout") return "读取 MCP 目录超时，请重试。";
  if (code === "cancelled") return "MCP 目录读取已取消。";
  if (code === "transport_error") return "MCP 连接已中断，请检查服务状态后重试。";
  return String(result.message || (code === "protocol_error" ? "MCP 协议错误。" : "MCP 目录读取失败。"));
};

export const ConnectorsTab = () => {
  const mcpServers = useAppStore((s) => s.mcpServers);
  const conversationId = useAppStore((s) => s.conversationId);
  const [newServerName, setNewServerName] = useState("");
  const [newServerCommand, setNewServerCommand] = useState("");
  const [newServerArgs, setNewServerArgs] = useState("");
  const [newServerTransport, setNewServerTransport] = useState<McpTransport>("stdio");
  const [newServerUrl, setNewServerUrl] = useState("");
  const [newServerCwd, setNewServerCwd] = useState("");
  const [newServerEnv, setNewServerEnv] = useState<EnvRow[]>([]);
  const [newServerHeaders, setNewServerHeaders] = useState<EnvRow[]>([]);
  const [newServerHeadersHelper, setNewServerHeadersHelper] = useState("");
  const [newServerOAuthClientId, setNewServerOAuthClientId] = useState("");
  const [newServerOAuthCallbackPort, setNewServerOAuthCallbackPort] = useState("");
  const [newServerEnvVars, setNewServerEnvVars] = useState<PassThroughRow[]>([]);
  const [newServerAutoStart, setNewServerAutoStart] = useState(true);
  const [editingServerName, setEditingServerName] = useState<string | null>(null);
  const [addingServer, setAddingServer] = useState(false);
  const [togglingServer, setTogglingServer] = useState("");
  const [pendingServerActions, setPendingServerActions] = useState<Record<string, string>>({});
  const [refreshingServers, setRefreshingServers] = useState(false);
  const [inventoryViews, setInventoryViews] = useState<Record<string, InventoryViewState>>({});
  const canAddServer = Boolean(
    newServerName.trim()
    && (newServerTransport === "stdio" ? newServerCommand.trim() : newServerUrl.trim()),
  );

  const loadServerInventory = async (server: typeof mcpServers[number]) => {
    const connected = (server.phase ?? server.status) === "connected";
    if (!connected) {
      setInventoryViews((current) => ({
        ...current,
        [server.name]: {
          expanded: true,
          status: "error",
          error: server.phase === "auth_required" || server.phase === "expired"
            ? "请先完成此 MCP 服务的登录。"
            : "服务尚未连接，连接成功后才能读取 MCP 目录。",
        },
      }));
      return;
    }

    const operationId = inventoryOperationId();
    setInventoryViews((current) => ({
      ...current,
      [server.name]: { expanded: true, status: "loading", operationId },
    }));
    try {
      const result = await sendClientCommandAwaitResult(
        { type: "mcp.inventory.list", name: server.name, operation_id: operationId },
        "mcp.inventory.list",
        { timeoutMs: MCP_INVENTORY_COMMAND_TIMEOUT_MS },
      );
      const inventory = inventoryFromResult(result);
      const failure = !commandResultSucceeded(result)
        ? inventoryFailureMessage(result)
        : inventory
          ? ""
          : "MCP 服务返回了无效的目录响应。";
      setInventoryViews((current) => {
        if (current[server.name]?.operationId !== operationId) return current;
        return {
          ...current,
          [server.name]: failure
            ? { expanded: true, status: "error", error: failure }
            : { expanded: true, status: "loaded", data: inventory ?? undefined },
        };
      });
    } catch (error) {
      setInventoryViews((current) => {
        if (current[server.name]?.operationId !== operationId) return current;
        return {
          ...current,
          [server.name]: { expanded: true, status: "error", error: operationError(error) },
        };
      });
    }
  };

  const toggleServerInventory = (server: typeof mcpServers[number]) => {
    const current = inventoryViews[server.name];
    if (current?.expanded) {
      if (current.status === "loading" && current.operationId) {
        sendClientCommand({
          type: "mcp.inventory.cancel",
          name: server.name,
          operation_id: current.operationId,
        });
      }
      setInventoryViews((views) => ({
        ...views,
        [server.name]: { ...views[server.name], expanded: false, operationId: undefined },
      }));
      return;
    }
    if (current?.data) {
      setInventoryViews((views) => ({
        ...views,
        [server.name]: { ...views[server.name], expanded: true },
      }));
      return;
    }
    void loadServerInventory(server);
  };

  const resetEditor = () => {
    setEditingServerName(null);
    setNewServerName("");
    setNewServerCommand("");
    setNewServerArgs("");
    setNewServerTransport("stdio");
    setNewServerUrl("");
    setNewServerCwd("");
    setNewServerEnv([]);
    setNewServerHeaders([]);
    setNewServerHeadersHelper("");
    setNewServerOAuthClientId("");
    setNewServerOAuthCallbackPort("");
    setNewServerEnvVars([]);
    setNewServerAutoStart(true);
  };

  const changeServerTransport = (transport: McpTransport) => {
    setNewServerTransport(transport);
    if (transport === "stdio") {
      setNewServerUrl("");
      setNewServerHeaders([]);
      setNewServerHeadersHelper("");
      setNewServerOAuthClientId("");
      setNewServerOAuthCallbackPort("");
      return;
    }
    setNewServerCommand("");
    setNewServerArgs("");
    setNewServerCwd("");
    setNewServerEnv([]);
    setNewServerEnvVars([]);
    if (transport === "ws") {
      setNewServerOAuthClientId("");
      setNewServerOAuthCallbackPort("");
    }
  };

  const editServer = (server: typeof mcpServers[number]) => {
    setEditingServerName(server.name);
    setNewServerName(server.name);
    setNewServerTransport(server.transport ?? "stdio");
    setNewServerCommand(server.command ?? "");
    setNewServerArgs(formatMcpArgs(server.args));
    setNewServerUrl(server.url ?? "");
    setNewServerCwd(server.cwd ?? "");
    setNewServerEnv(Object.entries(server.env ?? {}).map(([name, value]) => ({ id: rowId(), name, value })));
    setNewServerHeaders(Object.entries(server.headers ?? {}).map(([name, value]) => ({ id: rowId(), name, value })));
    setNewServerHeadersHelper(server.headersHelper ?? "");
    setNewServerOAuthClientId(server.oauth?.clientId ?? "");
    setNewServerOAuthCallbackPort(server.oauth?.callbackPort ? String(server.oauth.callbackPort) : "");
    setNewServerEnvVars((server.envVars ?? []).map((item) => {
      const name = typeof item === "string" ? item : String(item.name ?? "");
      const source = typeof item === "string" ? item : String(item.source ?? name);
      return { id: rowId(), name, source };
    }));
    setNewServerAutoStart(server.autoStart !== false);
  };

  const saveServer = async () => {
    const name = newServerName.trim();
    const command = newServerCommand.trim();
    const argsText = newServerArgs;
    const url = newServerUrl.trim();
    if (!canAddServer || addingServer) return;
    setAddingServer(true);
    try {
      const commandType = editingServerName ? "mcp.update" : "mcp.add";
      const env = Object.fromEntries(
        newServerEnv
          .filter((row) => row.name.trim())
          .map((row) => [row.name.trim(), row.value]),
      );
      const envVars = newServerEnvVars
        .filter((row) => row.name.trim() && row.source.trim())
        .map((row) => row.name.trim() === row.source.trim()
          ? row.name.trim()
          : { name: row.name.trim(), source: row.source.trim() });
      const headers = Object.fromEntries(
        newServerHeaders
          .filter((row) => row.name.trim())
          .map((row) => [row.name.trim(), row.value]),
      );
      const shared = {
        name,
        auto_start: newServerAutoStart,
      };
      let mutation: McpServerMutationPayload;
      if (newServerTransport === "stdio") {
        mutation = {
          ...shared,
          transport: "stdio",
          command,
          args: parseMcpArgs(argsText),
          ...(newServerCwd.trim() ? { cwd: newServerCwd.trim() } : {}),
          ...(Object.keys(env).length ? { env } : {}),
          ...(envVars.length ? { env_vars: envVars } : {}),
        };
      } else if (newServerTransport === "sse" || newServerTransport === "http") {
        const oauth = {
              ...(newServerOAuthClientId.trim() ? { client_id: newServerOAuthClientId.trim() } : {}),
              ...(newServerOAuthCallbackPort.trim() ? { callback_port: Number(newServerOAuthCallbackPort) } : {}),
        };
        mutation = {
          ...shared,
          transport: newServerTransport,
          url,
          ...(Object.keys(headers).length ? { headers } : {}),
          ...(newServerHeadersHelper.trim() ? { headers_helper: newServerHeadersHelper.trim() } : {}),
          ...(Object.keys(oauth).length ? { oauth } : {}),
        };
      } else {
        mutation = {
          ...shared,
          transport: "ws",
          url,
          ...(Object.keys(headers).length ? { headers } : {}),
          ...(newServerHeadersHelper.trim() ? { headers_helper: newServerHeadersHelper.trim() } : {}),
        };
      }
      const clientCommand: McpAddCommand | McpUpdateCommand = editingServerName
        ? { type: "mcp.update", original_name: editingServerName, ...mutation }
        : { type: "mcp.add", ...mutation };
      const result = await sendClientCommandAwaitResult(
        clientCommand,
        commandType,
        { timeoutMs: LONG_COMMAND_RESULT_TIMEOUT_MS },
      );
      if (reportCommandFailure(result, editingServerName ? "保存 MCP 服务" : "添加 MCP 服务", "服务未返回具体原因")) return;
      pushToast(commandFeedback(
        result,
        editingServerName ? `已保存 MCP 服务：${name}` : `已添加 MCP 服务：${name}`,
      ), "success");
      resetEditor();
    } catch (error) {
      pushToast(`${editingServerName ? "保存" : "添加"} MCP 服务失败：${operationError(error)}`, "error");
    } finally {
      setAddingServer(false);
    }
  };

  const toggleServer = async (name: string, enabled: boolean) => {
    if (togglingServer) return;
    setTogglingServer(name);
    try {
      const result = await sendClientCommandAwaitResult(
        { type: "mcp.toggle", name, enabled },
        "mcp.toggle",
        { timeoutMs: LONG_COMMAND_RESULT_TIMEOUT_MS },
      );
      if (!reportCommandFailure(result, enabled ? "启用 MCP 服务" : "停用 MCP 服务", "服务未返回具体原因")) {
        pushToast(`${enabled ? "已启用" : "已停用"} MCP 服务：${name}`, "success");
      }
    } catch (error) {
      pushToast(`${enabled ? "启用" : "停用"} MCP 服务失败：${operationError(error)}`, "error");
    } finally {
      setTogglingServer("");
    }
  };

  const runServerAction = async (
    name: string,
    action: "mcp.oauth.login" | "mcp.oauth.logout" | "mcp.restart" | "mcp.remove",
    command: ClientCommand,
    successMessage: string,
  ) => {
    if (pendingServerActions[name]) return;
    setPendingServerActions((current) => ({ ...current, [name]: action }));
    try {
      const result = await sendClientCommandAwaitResult(
        command,
        action,
        { timeoutMs: LONG_COMMAND_RESULT_TIMEOUT_MS },
      );
      if (!reportCommandFailure(result, successMessage.replace(/^已/, ""), "服务未返回具体原因")) {
        pushToast(successMessage, "success");
      }
    } catch (error) {
      pushToast(`${successMessage.replace(/^已/, "")}失败：${operationError(error)}`, "error");
    } finally {
      setPendingServerActions((current) => {
        const next = { ...current };
        delete next[name];
        return next;
      });
    }
  };

  const removeServer = async (name: string) => {
    const confirmed = await showConfirm({
      title: "删除 MCP 服务",
      message: `确定删除 ${name}？该服务的本地配置会被移除。`,
      confirmLabel: "删除",
      danger: true,
    });
    if (!confirmed) return;
    await runServerAction(
      name,
      "mcp.remove",
      { type: "mcp.remove", name },
      `已删除 MCP 服务：${name}`,
    );
  };

  const decideProjectServer = async (
    name: string,
    workspaceRoot: string | undefined,
    action: "mcp.project.approve" | "mcp.project.approve_all" | "mcp.project.reject",
    successMessage: string,
  ) => {
    if (pendingServerActions[name]) return;
    if (!conversationId || !workspaceRoot) {
      pushToast("项目 MCP 的会话或工作区归属已失效，请刷新后重试", "error");
      return;
    }
    setPendingServerActions((current) => ({ ...current, [name]: action }));
    try {
      const result = await sendClientCommandAwaitResult(
        { type: action, name, conversation_id: conversationId, workspace_root: workspaceRoot },
        action,
        { timeoutMs: LONG_COMMAND_RESULT_TIMEOUT_MS },
      );
      if (!reportCommandFailure(result, successMessage, "服务未返回具体原因")) {
        pushToast(commandFeedback(result, successMessage), "success");
      }
    } catch (error) {
      pushToast(`${successMessage}失败：${operationError(error)}`, "error");
    } finally {
      setPendingServerActions((current) => {
        const next = { ...current };
        delete next[name];
        return next;
      });
    }
  };

  const refreshServers = async () => {
    if (refreshingServers) return;
    setRefreshingServers(true);
    try {
      const result = await sendClientCommandAwaitResult({ type: "mcp.list" }, "mcp.list");
      if (!reportCommandFailure(result, "刷新 MCP 状态", "服务未返回具体原因")) {
        pushToast("MCP 状态已刷新", "success");
      }
    } catch (error) {
      pushToast(`刷新 MCP 状态失败：${operationError(error)}`, "error");
    } finally {
      setRefreshingServers(false);
    }
  };

  return (
    <>
      <Section title="MCP 服务" description="运行时服务、协商能力与工具数。">
            <div className="settings-mcp-list">
            {mcpServers.length === 0 && <div style={emptyInlineStyle}>尚未配置 MCP 服务。</div>}
            {mcpServers.map((server) => {
              const inventoryView = inventoryViews[server.name];
              const inventory = inventoryView?.data;
              return (
              <div key={server.name} className="settings-mcp-server-row" style={mcpServerRowStyle}>
                <BrandIcon value={`${server.name} ${server.url || ""}`} websiteUrl={server.docsUrl || server.url} fallback="web" size={18} />
                <div className="flex-1 min-w-0">
                  <div className="settings-mcp-server-meta">
                    <span style={mcpDotStyle(server.phase ?? server.status)} />
                    <span style={mcpNameStyle}>{server.name}</span>
                    {server.source === "project" && <span style={miniMetaStyle}>项目 .mcp.json</span>}
                    {server.approvalStatus === "pending" && (
                      <span style={{ ...miniMetaStyle, color: "var(--state-warning)" }}>等待批准</span>
                    )}
                    {server.approvalStatus === "rejected" && (
                      <span style={{ ...miniMetaStyle, color: "var(--text-tertiary)" }}>已拒绝</span>
                    )}
                    {server.enabled === false && (
                      <span style={{ ...miniMetaStyle, color: "var(--state-warning)" }}>策略禁用</span>
                    )}
                    {(server.phase ?? server.status) !== "connected" && (
                      <span style={statusChipStyle(server.phase ?? server.status)}>{connectorStatusLabel(server.phase ?? server.status)}</span>
                    )}
                    {server.requiresUserAction && (
                      <span style={{ ...miniMetaStyle, color: "var(--state-warning)" }}>需要处理</span>
                    )}
                    {server.cleanup?.pending && (
                      <span style={{ ...miniMetaStyle, color: "var(--state-warning)" }}>清理未完成</span>
                    )}
                    {connectorAuthStatusLabel(server.authStatus) && (
                      <span style={{
                        ...miniMetaStyle,
                        color: server.authStatus === "not_logged_in" ? "var(--state-warning)" : "var(--state-success)",
                      }}>{connectorAuthStatusLabel(server.authStatus)}</span>
                    )}
                    <span style={miniMetaStyle}>{server.tools ?? 0} 个工具</span>
                    {server.capabilities?.resources && <span style={miniMetaStyle}>资源</span>}
                    {server.capabilities?.prompts && <span style={miniMetaStyle}>提示词</span>}
                  </div>
                  {server.lastError && <div style={mcpErrorStyle}>{server.lastError}</div>}
                  {server.enabled === false && server.disabledReason && (
                    <div style={mcpErrorStyle}>{server.disabledReason}</div>
                  )}
                  {server.source === "project" && server.configPath && (
                    <div style={{ ...miniMetaStyle, marginTop: 3 }} title={server.configPath}>{server.configPath}</div>
                  )}
                  {server.requiresUserAction && server.setupHint && (
                    <div style={{ ...miniMetaStyle, marginTop: 3, whiteSpace: "normal", lineHeight: 1.35 }}>
                      {server.setupHint}
                    </div>
                  )}
                  {server.progress?.status === "running" && (
                    <div style={miniMetaStyle}>{server.progress.message === "Connecting..." ? "正在连接…" : server.progress.message || `${server.progress.operation}…`}</div>
                  )}
                  {inventoryView?.expanded && (
                    <div
                      aria-label={`MCP 目录 ${server.name}`}
                      style={{
                        marginTop: 8,
                        padding: 9,
                        border: "1px solid var(--border-subtle)",
                        borderRadius: "var(--radius-sm, 6px)",
                        background: "var(--surface-base)",
                      }}
                    >
                      {inventoryView.status === "loading" && (
                        <div className="flex items-center gap-2" style={miniMetaStyle}>
                          <RefreshCw size={13} className="animate-spin" aria-hidden="true" />
                          正在按需读取 MCP 目录…
                        </div>
                      )}
                      {inventoryView.status === "error" && (
                        <div className="flex items-center justify-between gap-2">
                          <span style={mcpErrorStyle}>{inventoryView.error || "MCP 目录读取失败。"}</span>
                          <button type="button" onClick={() => void loadServerInventory(server)} style={secondaryActionStyle}>重试</button>
                        </div>
                      )}
                      {inventoryView.status === "loaded" && inventory?.empty && (
                        <div style={miniMetaStyle}>该服务未公开资源、资源模板或提示词。</div>
                      )}
                      {inventoryView.status === "loaded" && inventory && !inventory.empty && (
                        <div className="flex flex-col gap-2">
                          {inventory.resources.length > 0 && (
                            <div>
                              <div style={{ ...miniMetaStyle, fontWeight: 600, marginBottom: 3 }}>资源 · {inventory.resources.length}</div>
                              {inventory.resources.map((item) => (
                                <div key={item.uri} style={{ ...miniMetaStyle, marginTop: 3, whiteSpace: "normal" }}>
                                  <strong>{item.name || item.uri}</strong>
                                  <div title={item.uri} style={{ fontFamily: "var(--font-mono)", overflowWrap: "anywhere" }}>{item.uri}</div>
                                  {item.description && <div>{item.description}</div>}
                                </div>
                              ))}
                            </div>
                          )}
                          {inventory.resource_templates.length > 0 && (
                            <div>
                              <div style={{ ...miniMetaStyle, fontWeight: 600, marginBottom: 3 }}>资源模板 · {inventory.resource_templates.length}</div>
                              {inventory.resource_templates.map((item) => (
                                <div key={item.uri_template} style={{ ...miniMetaStyle, marginTop: 3, whiteSpace: "normal" }}>
                                  <strong>{item.name || item.uri_template}</strong>
                                  <div title={item.uri_template} style={{ fontFamily: "var(--font-mono)", overflowWrap: "anywhere" }}>{item.uri_template}</div>
                                  {item.description && <div>{item.description}</div>}
                                </div>
                              ))}
                            </div>
                          )}
                          {inventory.prompts.length > 0 && (
                            <div>
                              <div style={{ ...miniMetaStyle, fontWeight: 600, marginBottom: 3 }}>提示词 · {inventory.prompts.length}</div>
                              {inventory.prompts.map((item) => (
                                <div key={item.name} style={{ ...miniMetaStyle, marginTop: 3, whiteSpace: "normal" }}>
                                  <strong>{item.name}</strong>
                                  {item.description && <div>{item.description}</div>}
                                  {Boolean(item.arguments?.length) && (
                                    <div>参数：{item.arguments?.map((argument) => `${argument.name}${argument.required ? "*" : ""}`).join("、")}</div>
                                  )}
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </div>
                <div className="settings-mcp-server-actions">
                  <button
                    type="button"
                    onClick={() => toggleServerInventory(server)}
                    style={mcpActionBtnStyle}
                    title={inventoryView?.expanded ? "收起 MCP 目录" : "按需查看 MCP 资源、资源模板和提示词"}
                    aria-label={`${inventoryView?.expanded ? "收起" : "查看"} MCP 内容 ${server.name}`}
                  >
                    {inventoryView?.expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  </button>
                  {server.source === "project" && server.approvalStatus === "pending" && (
                    <>
                      <button
                        type="button"
                        onClick={() => void decideProjectServer(server.name, server.projectWorkspace, "mcp.project.approve_all", `已允许当前及后续项目 MCP：${server.name}`)}
                        disabled={Boolean(pendingServerActions[server.name])}
                        style={mcpActionBtnStyle}
                        title="使用此服务，并允许此项目今后发现的所有 MCP 服务"
                        aria-label={`允许当前及后续项目 MCP ${server.name}`}
                      >{pendingServerActions[server.name] === "mcp.project.approve_all" ? <RefreshCw size={14} className="animate-spin" /> : <CheckCheck size={14} />}</button>
                      <button
                        type="button"
                        onClick={() => void decideProjectServer(server.name, server.projectWorkspace, "mcp.project.approve", `已允许项目 MCP：${server.name}`)}
                        disabled={Boolean(pendingServerActions[server.name])}
                        style={mcpActionBtnStyle}
                        title="仅使用此 MCP 服务"
                        aria-label={`允许项目 MCP ${server.name}`}
                      >{pendingServerActions[server.name] === "mcp.project.approve" ? <RefreshCw size={14} className="animate-spin" /> : <Check size={14} />}</button>
                      <button
                        type="button"
                        onClick={() => void decideProjectServer(server.name, server.projectWorkspace, "mcp.project.reject", `已跳过项目 MCP：${server.name}`)}
                        disabled={Boolean(pendingServerActions[server.name])}
                        style={mcpActionBtnStyle}
                        title="继续但不使用此 MCP 服务"
                        aria-label={`拒绝项目 MCP ${server.name}`}
                      >{pendingServerActions[server.name] === "mcp.project.reject" ? <RefreshCw size={14} className="animate-spin" /> : <Ban size={14} />}</button>
                    </>
                  )}
                  {server.editable && (
                    <button
                      type="button"
                      className="settings-toggle settings-mcp-toggle"
                      role="switch"
                      aria-checked={server.autoStart !== false}
                      aria-label={`${server.autoStart === false ? "启用" : "停用"} ${server.name}`}
                      data-active={server.autoStart !== false ? "true" : "false"}
                      disabled={server.enabled === false || togglingServer === server.name || Boolean(pendingServerActions[server.name])}
                      onClick={() => void toggleServer(server.name, server.autoStart === false)}
                      title={server.enabled === false ? server.disabledReason || "已被管理策略禁用" : server.autoStart === false ? "启用" : "停用"}
                    ><span /></button>
                  )}
                  {server.editable && (
                    <button
                      type="button"
                      onClick={() => editServer(server)}
                      style={mcpActionBtnStyle}
                      title="编辑"
                      aria-label={`编辑 ${server.name}`}
                    ><Pencil size={14} /></button>
                  )}
                  {server.approvalStatus !== "pending" && server.approvalStatus !== "rejected" && ((server.authStatus === "not_logged_in" || server.phase === "auth_required" || server.phase === "expired") ? (
                    <button
                      type="button"
                      onClick={() => void runServerAction(
                        server.name,
                        "mcp.oauth.login",
                        { type: "mcp.oauth.login", name: server.name },
                        `已登录 MCP 服务：${server.name}`,
                      )}
                      disabled={Boolean(pendingServerActions[server.name])}
                      style={mcpActionBtnStyle}
                      title="登录"
                      aria-label={`登录 ${server.name}`}
                    >{pendingServerActions[server.name] === "mcp.oauth.login" ? <RefreshCw size={14} className="animate-spin" /> : <LogIn size={14} />}</button>
                  ) : server.authStatus === "oauth" ? (
                    <>
                      <button
                        type="button"
                        onClick={() => void runServerAction(
                          server.name,
                          "mcp.restart",
                          { type: "mcp.restart", name: server.name },
                          `已重启 MCP 服务：${server.name}`,
                        )}
                        disabled={Boolean(pendingServerActions[server.name]) || server.phase === "connecting" || server.phase === "reconnecting"}
                        style={mcpActionBtnStyle}
                        title="重启"
                        aria-label={`重启 ${server.name}`}
                      ><RefreshCw size={14} className={pendingServerActions[server.name] === "mcp.restart" ? "animate-spin" : undefined} /></button>
                      <button
                        type="button"
                        onClick={() => void runServerAction(
                          server.name,
                          "mcp.oauth.logout",
                          { type: "mcp.oauth.logout", name: server.name },
                          `已清除 MCP 登录：${server.name}`,
                        )}
                        disabled={Boolean(pendingServerActions[server.name])}
                        style={mcpActionBtnStyle}
                        title="清除登录"
                        aria-label={`清除 ${server.name} 登录`}
                      >{pendingServerActions[server.name] === "mcp.oauth.logout" ? <RefreshCw size={14} className="animate-spin" /> : <LogOut size={14} />}</button>
                    </>
                  ) : (
                    <button
                      onClick={() => void runServerAction(
                        server.name,
                        "mcp.restart",
                        { type: "mcp.restart", name: server.name },
                        `已重启 MCP 服务：${server.name}`,
                      )}
                      disabled={Boolean(pendingServerActions[server.name]) || server.phase === "connecting" || server.phase === "reconnecting"}
                      style={mcpActionBtnStyle}
                      title="重启"
                      aria-label={`重启 ${server.name}`}
                    ><RefreshCw size={14} className={pendingServerActions[server.name] === "mcp.restart" ? "animate-spin" : undefined} /></button>
                  ))}
                  {server.editable && <button type="button" onClick={() => void removeServer(server.name)} disabled={Boolean(pendingServerActions[server.name])} style={mcpActionBtnStyle} title="删除" aria-label={`删除 ${server.name}`}>{pendingServerActions[server.name] === "mcp.remove" ? <RefreshCw size={14} className="animate-spin" /> : <Trash2 size={14} />}</button>}
                </div>
              </div>
              );
            })}
            </div>
          </Section>

          <Section
            title={editingServerName ? `编辑 ${editingServerName}` : "添加服务"}
            description="stdio 启动本地进程；HTTP 使用 streamable HTTP；SSE 为旧版远程传输。"
          >
            <div className="settings-mcp-editor">
              <div className="flex gap-2">
                <input aria-label="服务名称" type="text" value={newServerName} onChange={(e) => setNewServerName(e.target.value)} placeholder="服务名称" style={{ ...inputStyle, flex: 1 }} />
                <SelectMenu ariaLabel="传输方式" value={newServerTransport} onValueChange={(value) => changeServerTransport(value as McpTransport)} style={{ width: 150 }}>
                  <option value="stdio">stdio</option>
                  <option value="http">HTTP</option>
                  <option value="sse">SSE（旧版）</option>
                  <option value="ws">WebSocket</option>
                </SelectMenu>
              </div>
              {newServerTransport === "stdio" ? (
                <>
                  <input aria-label="启动命令" type="text" value={newServerCommand} onChange={(e) => setNewServerCommand(e.target.value)} placeholder="命令（python、npx、uvx…）" style={inputStyle} />
                  <input aria-label="命令参数" type="text" value={newServerArgs} onChange={(e) => setNewServerArgs(e.target.value)} placeholder="参数" style={inputStyle} />
                  <input aria-label="工作目录" type="text" value={newServerCwd} onChange={(e) => setNewServerCwd(e.target.value)} placeholder="工作目录（可选）" style={inputStyle} />
                </>
              ) : (
                <input aria-label="服务地址" type="text" value={newServerUrl} onChange={(e) => setNewServerUrl(e.target.value)} placeholder="http://localhost:8080/mcp" style={inputStyle} />
              )}
              {newServerTransport !== "stdio" && (
                <div className="settings-mcp-fieldset">
                  <div className="settings-mcp-fieldset-heading">
                    <span>请求头</span>
                    <button type="button" onClick={() => setNewServerHeaders((rows) => [...rows, { id: rowId(), name: "", value: "" }])} aria-label="添加请求头"><Plus /></button>
                  </div>
                  {newServerHeaders.map((row) => (
                    <div className="settings-mcp-pair-row" key={row.id}>
                      <input aria-label="请求头名称" value={row.name} onChange={(event) => setNewServerHeaders((rows) => rows.map((item) => item.id === row.id ? { ...item, name: event.target.value } : item))} placeholder="Authorization" style={inputStyle} />
                      <input aria-label={`${row.name || "请求头"}的值`} value={row.value} onChange={(event) => setNewServerHeaders((rows) => rows.map((item) => item.id === row.id ? { ...item, value: event.target.value } : item))} placeholder="Bearer ${TOKEN}" style={inputStyle} />
                      <button type="button" onClick={() => setNewServerHeaders((rows) => rows.filter((item) => item.id !== row.id))} aria-label="删除请求头"><X /></button>
                    </div>
                  ))}
                  {newServerHeaders.length === 0 && <span className="settings-mcp-empty-field">未设置请求头</span>}
                  <input
                    aria-label="动态请求头助手"
                    value={newServerHeadersHelper}
                    onChange={(event) => setNewServerHeadersHelper(event.target.value)}
                    placeholder="动态请求头命令（输出 JSON；10 秒超时）"
                    style={inputStyle}
                  />
                  {(newServerTransport === "sse" || newServerTransport === "http") && (
                    <div className="settings-mcp-pair-row">
                      <input
                        aria-label="OAuth 客户端 ID"
                        value={newServerOAuthClientId}
                        onChange={(event) => setNewServerOAuthClientId(event.target.value)}
                        placeholder="OAuth clientId（可选）"
                        style={inputStyle}
                      />
                      <input
                        aria-label="OAuth 回调端口"
                        type="number"
                        min={1}
                        max={65535}
                        value={newServerOAuthCallbackPort}
                        onChange={(event) => setNewServerOAuthCallbackPort(event.target.value)}
                        placeholder="callbackPort（可选）"
                        style={inputStyle}
                      />
                    </div>
                  )}
                </div>
              )}
              <div className="settings-mcp-fieldset">
                <div className="settings-mcp-fieldset-heading">
                  <span>环境变量</span>
                  <button type="button" onClick={() => setNewServerEnv((rows) => [...rows, { id: rowId(), name: "", value: "" }])} aria-label="添加环境变量"><Plus /></button>
                </div>
                {newServerEnv.map((row) => (
                  <div className="settings-mcp-pair-row" key={row.id}>
                    <input aria-label="环境变量名称" value={row.name} onChange={(event) => setNewServerEnv((rows) => rows.map((item) => item.id === row.id ? { ...item, name: event.target.value } : item))} placeholder="名称" style={inputStyle} />
                    <input aria-label={`${row.name || "环境变量"}的值`} value={row.value} onChange={(event) => setNewServerEnv((rows) => rows.map((item) => item.id === row.id ? { ...item, value: event.target.value } : item))} placeholder="值" style={inputStyle} />
                    <button type="button" onClick={() => setNewServerEnv((rows) => rows.filter((item) => item.id !== row.id))} aria-label="删除环境变量"><X /></button>
                  </div>
                ))}
                {newServerEnv.length === 0 && <span className="settings-mcp-empty-field">未设置固定环境变量</span>}
              </div>
              <div className="settings-mcp-fieldset">
                <div className="settings-mcp-fieldset-heading">
                  <span>环境变量透传</span>
                  <button type="button" onClick={() => setNewServerEnvVars((rows) => [...rows, { id: rowId(), name: "", source: "" }])} aria-label="添加透传变量"><Plus /></button>
                </div>
                {newServerEnvVars.map((row) => (
                  <div className="settings-mcp-pair-row" key={row.id}>
                    <input aria-label="目标变量名称" value={row.name} onChange={(event) => setNewServerEnvVars((rows) => rows.map((item) => item.id === row.id ? { ...item, name: event.target.value } : item))} placeholder="目标名称" style={inputStyle} />
                    <input aria-label={`${row.name || "变量"}的来源`} value={row.source} onChange={(event) => setNewServerEnvVars((rows) => rows.map((item) => item.id === row.id ? { ...item, source: event.target.value } : item))} placeholder="系统变量名称" style={inputStyle} />
                    <button type="button" onClick={() => setNewServerEnvVars((rows) => rows.filter((item) => item.id !== row.id))} aria-label="删除透传变量"><X /></button>
                  </div>
                ))}
                {newServerEnvVars.length === 0 && <span className="settings-mcp-empty-field">未透传系统环境变量</span>}
              </div>
              <div className="settings-mcp-autostart">
                <div><strong>自动启动</strong><span>MiniCode 启动时连接此服务。</span></div>
                <button
                  type="button"
                  className="settings-toggle"
                  role="switch"
                  aria-checked={newServerAutoStart}
                  aria-label="自动启动 MCP 服务"
                  data-active={newServerAutoStart ? "true" : "false"}
                  onClick={() => setNewServerAutoStart((enabled) => !enabled)}
                ><span /></button>
              </div>
              <div className="flex justify-between gap-2">
                {editingServerName ? (
                  <button type="button" onClick={resetEditor} style={secondaryActionStyle}>取消</button>
                ) : (
                  <button type="button" onClick={() => void refreshServers()} disabled={refreshingServers} style={secondaryActionStyle}>
                    {refreshingServers ? "刷新中…" : "刷新"}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => void saveServer()}
                  disabled={!canAddServer || addingServer}
                  style={primaryActionStyle}
                >
                  {addingServer ? "正在保存…" : editingServerName ? "保存修改" : "添加服务"}
                </button>
              </div>
            </div>
      </Section>
    </>
  );
};
