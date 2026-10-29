import { useI18n, type Translate } from "../i18n";
import { useEffect, useLayoutEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { ArrowLeft, Ban, Check, CheckCheck, ChevronDown, ChevronRight, ExternalLink, LogIn, LogOut, Plus, RefreshCw, Search, Settings, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import {
  commandResultSucceeded,
  LONG_COMMAND_RESULT_TIMEOUT_MS,
  sendClientCommand,
  sendClientCommandAwaitResult,
} from "../protocol/ws-outbox";
import type {
  McpAddCommand,
  McpInventoryPayload,
  McpServerMutationPayload,
  McpTransport,
  McpUpdateCommand,
} from "../protocol/events";
import type { CommandResultEvent } from "../protocol/events";
import { pushToast } from "./ToastContainer";
import { showConfirm } from "./DialogService";
import { desktop, openExternal } from "../desktop/runtime";
import { reportCommandFailure } from "./commandFeedback";
import { normalizeWorkspaceRoot } from "../lib/workspace-path";
import { McpToolsEditor } from "./McpToolsEditor";
import { SettingsSourceIcon } from "./SettingsSourceIcon";
import {
  secondaryActionStyle,
  mcpNameStyle,
  mcpErrorStyle,
  statusChipStyle,
  miniMetaStyle,
  mcpActionBtnStyle,
} from "./settingsShared";
import "./ConnectorsTab.css";

function McpAppDataSettings() {
  const { t: tr } = useI18n();
  const [clearing, setClearing] = useState(false);
  const sandbox = desktop()?.mcpAppSandbox;
  if (!sandbox) return null;
  const clear = async () => {
    setClearing(true);
    try {
      await sandbox.clearData();
      pushToast(tr("MCP 应用数据已清除"), "success");
    } catch (error) {
      pushToast(tr("无法清除 MCP 应用数据：{v0}", { v0: error instanceof Error ? error.message : String(error) }), "error");
    } finally { setClearing(false); }
  };
  return <section className="settings-group settings-mcp-apps" aria-label={tr("MCP 应用")}>
    <h3 className="settings-group-title">{tr("MCP 应用")}</h3>
    <div className="settings-card"><div className="settings-row">
      <div className="settings-row-copy"><div className="settings-row-title">{tr("应用数据")}</div>
        <div className="settings-row-description">{tr("清除所有 MCP 应用的站点数据和缓存")}</div>
      </div>
      <button type="button" className="settings-action-button" disabled={clearing} aria-busy={clearing} onClick={() => void clear()}>{clearing && <span className="settings-refresh-spinner"><SettingsSourceIcon icon="spinner" size={12} /></span>}{tr("清除所有应用数据")}</button>
    </div></div>
  </section>;
}

type ValueRow = { id: string; value: string };
type EnvRow = { id: string; name: string; value: string };
type PassThroughRow = { id: string; name: string; source: "local" | "remote" };
type InventoryViewState = {
  expanded: boolean;
  status: "idle" | "loading" | "loaded" | "error";
  operationId?: string;
  data?: McpInventoryPayload;
  error?: string;
};
const rowId = () => `mcp-row-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
const emptyValueRow = (): ValueRow => ({ id: rowId(), value: "" });
const emptyPairRow = (): EnvRow => ({ id: rowId(), name: "", value: "" });
const emptyPassThroughRow = (): PassThroughRow => ({ id: rowId(), name: "", source: "local" });
const mcpPairs = (rows: EnvRow[]): Record<string, string> => Object.fromEntries(rows
  .filter(row => row.name.trim() && row.value.trim())
  .map(row => [row.name.trim(), row.value.trim()]));
const inventoryOperationId = () => `mcp-inventory-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
const MCP_INVENTORY_COMMAND_TIMEOUT_MS = 75_000;

const McpPairFields = ({ title, nameLabel, valueLabel, rows, onChange, addLabel, removeLabel, tr }: {
  title: string; nameLabel: string; valueLabel: string; rows: EnvRow[];
  onChange: Dispatch<SetStateAction<EnvRow[]>>; addLabel: string; removeLabel: string; tr: Translate;
}) => <div className="mcp-detail-field">
  <span className="mcp-detail-label">{title}</span>
  <div className="mcp-detail-entries">
    {rows.map(row => <div className="mcp-detail-pair" key={row.id}>
      <input aria-label={nameLabel} value={row.name} placeholder={tr("键")} onChange={event => onChange(items => items.map(item => item.id === row.id ? { ...item, name: event.target.value } : item))} />
      <input aria-label={row.name ? tr("{v0}的值", { v0: row.name }) : valueLabel} value={row.value} placeholder={tr("值")} onChange={event => onChange(items => items.map(item => item.id === row.id ? { ...item, value: event.target.value } : item))} />
      <button type="button" className="mcp-detail-remove" aria-label={removeLabel} disabled={rows.length === 1 && !row.name && !row.value}
        onClick={() => onChange(items => items.length === 1 ? [emptyPairRow()] : items.filter(item => item.id !== row.id))}><Trash2 size={14} /></button>
    </div>)}
    <button type="button" className="mcp-detail-add-entry" onClick={() => onChange(items => [...items, emptyPairRow()])}><Plus size={14} />{addLabel}</button>
  </div>
</div>;

const connectorStatusLabel = (tr: Translate, status?: string): string => {
  if (status === "connected") return tr("已连接");
  if (status === "connecting") return tr("连接中");
  if (status === "auth_required") return tr("需要登录");
  if (status === "expired") return tr("登录已过期");
  if (status === "reconnecting") return tr("正在重连");
  if (status === "failed" || status === "error") return tr("失败");
  if (status === "disabled") return tr("已停用");
  return String(status || tr("未知")).replace(/_/g, " ");
};

const connectorAuthStatusLabel = (tr: Translate, status?: string): string | null => {
  if (status === "oauth") return tr("OAuth 已登录");
  if (status === "not_logged_in") return tr("未登录");
  return null;
};

const commandFeedback = (tr: Translate, result: CommandResultEvent, message: string): string =>
  result.data?.tool_availability === "next_turn" ? tr("{v0}，下条消息可用", { v0: message }) : message;

const operationError = (tr: Translate, error: unknown): string =>
  error instanceof Error ? error.message : String(error || tr("未知错误"));

const inventoryFromResult = (result: CommandResultEvent): McpInventoryPayload | null => {
  const inventory = result.data?.inventory;
  if (!inventory || typeof inventory !== "object") return null;
  const value = inventory as Partial<McpInventoryPayload>;
  if (!value.server_name || !Array.isArray(value.resources) || !Array.isArray(value.resource_templates) || !Array.isArray(value.prompts)) {
    return null;
  }
  return value as McpInventoryPayload;
};

const inventoryFailureMessage = (tr: Translate, result: CommandResultEvent): string => {
  const code = String(result.data?.error_code || "");
  if (code === "server_not_found") return tr("该 MCP 服务已不存在，请刷新服务列表。");
  if (code === "not_connected") return tr("服务尚未连接，连接成功后才能读取 MCP 目录。");
  if (code === "capabilities_unavailable") return tr("服务尚未完成 MCP 能力协商，请稍后重试。");
  if (code === "authentication_required" || code === "authentication_expired") {
    return tr("请先完成此 MCP 服务的登录。");
  }
  if (code === "timeout") return tr("读取 MCP 目录超时，请重试。");
  if (code === "cancelled") return tr("MCP 目录读取已取消。");
  if (code === "transport_error") return tr("MCP 连接已中断，请检查服务状态后重试。");
  return String(result.message || (code === "protocol_error" ? tr("MCP 协议错误。") : tr("MCP 目录读取失败。")));
};

export interface ConnectorsTabProps {
  editorRequest?: number;
  search?: string;
  onEditorChange?: (open: boolean) => void;
}

export const ConnectorsTab = ({ editorRequest = 0, search: externalSearch, onEditorChange }: ConnectorsTabProps = {}) => {
  const { t: tr } = useI18n();
  const mcpServers = useAppStore((s) => s.mcpServers);
  const [localSearch, setSearch] = useState("");
  const search = externalSearch ?? localSearch;
  const visibleServers = mcpServers.filter(server =>
    (server.pluginServerName || server.name).toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const standaloneServers = visibleServers.filter(server => !server.pluginId);
  const pluginServers = visibleServers.filter(server => server.pluginId);
  const conversationId = useAppStore((s) => s.conversationId);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const inventoryScope = JSON.stringify([conversationId, normalizeWorkspaceRoot(workingDirectory)]);
  const inventoryKey = (name: string) => `${inventoryScope}:${name}`;
  const [newServerName, setNewServerName] = useState("");
  const [newServerCommand, setNewServerCommand] = useState("");
  const [newServerArgs, setNewServerArgs] = useState<ValueRow[]>(() => [emptyValueRow()]);
  const [newServerTransport, setNewServerTransport] = useState<McpTransport>("stdio");
  const [newServerUrl, setNewServerUrl] = useState("");
  const [newServerCwd, setNewServerCwd] = useState("");
  const [newServerEnv, setNewServerEnv] = useState<EnvRow[]>(() => [emptyPairRow()]);
  const [newServerHeaders, setNewServerHeaders] = useState<EnvRow[]>(() => [emptyPairRow()]);
  const [newServerBearerToken, setNewServerBearerToken] = useState("");
  const [newServerEnvHeaders, setNewServerEnvHeaders] = useState<EnvRow[]>(() => [emptyPairRow()]);
  const [newServerHeadersHelper, setNewServerHeadersHelper] = useState("");
  const [newServerOAuthClientId, setNewServerOAuthClientId] = useState("");
  const [newServerOAuthCallbackPort, setNewServerOAuthCallbackPort] = useState("");
  const [newServerEnvVars, setNewServerEnvVars] = useState<PassThroughRow[]>(() => [emptyPassThroughRow()]);
  const [newServerAutoStart, setNewServerAutoStart] = useState(true);
  const [initialEditorFields, setInitialEditorFields] = useState("");
  const [editingServerName, setEditingServerName] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [addingServer, setAddingServer] = useState(false);
  const [togglingServer, setTogglingServer] = useState("");
  const [pendingServerActions, setPendingServerActions] = useState<Record<string, string>>({});
  const [inventoryViews, setInventoryViews] = useState<Record<string, InventoryViewState>>({});
  const currentDraft = { newServerName, newServerCommand, newServerArgs, newServerTransport, newServerUrl, newServerCwd,
    newServerEnv, newServerHeaders, newServerBearerToken, newServerEnvHeaders, newServerHeadersHelper, newServerOAuthClientId, newServerOAuthCallbackPort,
    newServerEnvVars, newServerAutoStart, initialEditorFields, editingServerName, editorOpen };
  const editorFields = (draft: typeof currentDraft) => JSON.stringify({
    name: draft.newServerName.trim(), transport: draft.newServerTransport,
    command: draft.newServerCommand.trim(), args: draft.newServerArgs.map(row => row.value.trim()).filter(Boolean),
    cwd: draft.newServerCwd.trim(), env: mcpPairs(draft.newServerEnv),
    envVars: draft.newServerEnvVars.filter(row => row.name.trim()).map(row => [row.name.trim(), row.source]),
    url: draft.newServerUrl.trim(), headers: mcpPairs(draft.newServerHeaders),
    bearerTokenEnvVar: draft.newServerBearerToken.trim(), envHeaders: mcpPairs(draft.newServerEnvHeaders),
  });
  const editorChanged = editorFields(currentDraft) !== initialEditorFields;
  const currentDraftRef = useRef(currentDraft);
  currentDraftRef.current = currentDraft;
  const draftScope = useRef(inventoryScope);
  const scopedDrafts = useRef(new Map<string, { selected: string; drafts: Map<string, typeof currentDraft> }>());
  if (draftScope.current === inventoryScope) {
    const scope = scopedDrafts.current.get(inventoryScope) || { selected: "new", drafts: new Map<string, typeof currentDraft>() };
    scope.selected = editingServerName || "new";
    scope.drafts.set(scope.selected, currentDraft);
    scopedDrafts.current.set(inventoryScope, scope);
  }
  const restoreDraft = (draft: typeof currentDraft) => {
    setEditorOpen(draft.editorOpen);
    setNewServerName(draft.newServerName); setNewServerCommand(draft.newServerCommand); setNewServerArgs(draft.newServerArgs);
    setNewServerTransport(draft.newServerTransport); setNewServerUrl(draft.newServerUrl); setNewServerCwd(draft.newServerCwd);
    setNewServerEnv(draft.newServerEnv); setNewServerHeaders(draft.newServerHeaders); setNewServerHeadersHelper(draft.newServerHeadersHelper);
    setNewServerBearerToken(draft.newServerBearerToken); setNewServerEnvHeaders(draft.newServerEnvHeaders);
    setInitialEditorFields(draft.initialEditorFields);
    setNewServerOAuthClientId(draft.newServerOAuthClientId); setNewServerOAuthCallbackPort(draft.newServerOAuthCallbackPort);
    setNewServerEnvVars(draft.newServerEnvVars); setNewServerAutoStart(draft.newServerAutoStart); setEditingServerName(draft.editingServerName);
  };
  useLayoutEffect(() => {
    draftScope.current = inventoryScope;
    const scope = scopedDrafts.current.get(inventoryScope);
    const draft = scope?.drafts.get(scope.selected);
    if (draft) restoreDraft(draft);
    else clearEditor();
  }, [inventoryScope]);
  useEffect(() => setInventoryViews({}), [inventoryScope]);
  const editorChangeCallback = useRef(onEditorChange);
  editorChangeCallback.current = onEditorChange;
  useEffect(() => { editorChangeCallback.current?.(editorOpen); }, [editorOpen]);
  const handledEditorRequest = useRef(0);
  useEffect(() => {
    if (editorRequest === handledEditorRequest.current) return;
    handledEditorRequest.current = editorRequest;
    openNewEditor();
  }, [editorRequest]);
  const canAddServer = Boolean(
    newServerName.trim()
    && (newServerTransport === "stdio" ? newServerCommand.trim() : newServerUrl.trim()),
  );

  const loadServerInventory = async (server: typeof mcpServers[number]) => {
    const key = inventoryKey(server.name);
    const connected = (server.phase ?? server.status) === "connected";
    if (!connected) {
      setInventoryViews((current) => ({
        ...current,
        [key]: {
          expanded: true,
          status: "error",
          error: server.phase === "auth_required" || server.phase === "expired"
            ? tr("请先完成此 MCP 服务的登录。")
            : tr("服务尚未连接，连接成功后才能读取 MCP 目录。"),
        },
      }));
      return;
    }

    const operationId = inventoryOperationId();
    setInventoryViews((current) => ({
      ...current,
      [key]: { expanded: true, status: "loading", operationId },
    }));
    try {
      const result = await sendClientCommandAwaitResult(
        { type: "mcp.inventory.list", name: server.name, operation_id: operationId,
          conversation_id: conversationId || undefined, workspace_root: workingDirectory || undefined },
        "mcp.inventory.list",
        { timeoutMs: MCP_INVENTORY_COMMAND_TIMEOUT_MS },
      );
      const inventory = inventoryFromResult(result);
      const failure = !commandResultSucceeded(result)
        ? inventoryFailureMessage(tr, result)
        : inventory
          ? ""
          : tr("MCP 服务返回了无效的目录响应。");
      setInventoryViews((current) => {
        if (current[key]?.operationId !== operationId) return current;
        return {
          ...current,
          [key]: failure
            ? { expanded: true, status: "error", error: failure }
            : { expanded: true, status: "loaded", data: inventory ?? undefined },
        };
      });
    } catch (error) {
      setInventoryViews((current) => {
        if (current[key]?.operationId !== operationId) return current;
        return {
          ...current,
          [key]: { expanded: true, status: "error", error: operationError(tr, error) },
        };
      });
    }
  };

  const toggleServerInventory = (server: typeof mcpServers[number]) => {
    const key = inventoryKey(server.name);
    const current = inventoryViews[key];
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
        [key]: { ...views[key], expanded: false, operationId: undefined },
      }));
      return;
    }
    if (current?.data) {
      setInventoryViews((views) => ({
        ...views,
        [key]: { ...views[key], expanded: true },
      }));
      return;
    }
    void loadServerInventory(server);
  };

  const clearEditor = () => {
    setEditorOpen(false);
    setEditingServerName(null);
    setNewServerName("");
    setNewServerCommand("");
    setNewServerArgs([emptyValueRow()]);
    setNewServerTransport("stdio");
    setNewServerUrl("");
    setNewServerCwd("");
    setNewServerEnv([emptyPairRow()]);
    setNewServerHeaders([emptyPairRow()]);
    setNewServerBearerToken("");
    setNewServerEnvHeaders([emptyPairRow()]);
    setNewServerHeadersHelper("");
    setNewServerOAuthClientId("");
    setNewServerOAuthCallbackPort("");
    setNewServerEnvVars([emptyPassThroughRow()]);
    setNewServerAutoStart(true);
    setInitialEditorFields("");
  };
  const resetEditor = () => {
    scopedDrafts.current.get(inventoryScope)?.drafts.delete(editingServerName || "new");
    const newDraft = editingServerName ? scopedDrafts.current.get(inventoryScope)?.drafts.get("new") : undefined;
    if (newDraft) restoreDraft(newDraft);
    else clearEditor();
  };

  const openNewEditor = () => {
    const draft = scopedDrafts.current.get(inventoryScope)?.drafts.get("new");
    if (draft) restoreDraft({ ...draft, editorOpen: true });
    else { clearEditor(); setEditorOpen(true); }
  };

  const editServer = (server: typeof mcpServers[number]) => {
    const draft = scopedDrafts.current.get(inventoryScope)?.drafts.get(server.name);
    if (draft) { restoreDraft({ ...draft, editorOpen: true }); return; }
    const fields: typeof currentDraft = {
      editorOpen: true, editingServerName: server.name, newServerName: server.name,
      newServerTransport: server.transport ?? "stdio", newServerCommand: server.command ?? "",
      newServerArgs: server.args?.length ? server.args.map(value => ({ id: rowId(), value })) : [emptyValueRow()],
      newServerUrl: server.url ?? "", newServerCwd: server.cwd ?? "",
      newServerEnv: Object.keys(server.env ?? {}).length ? Object.entries(server.env!).map(([name, value]) => ({ id: rowId(), name, value })) : [emptyPairRow()],
      newServerHeaders: Object.keys(server.headers ?? {}).length ? Object.entries(server.headers!).map(([name, value]) => ({ id: rowId(), name, value })) : [emptyPairRow()],
      newServerBearerToken: server.bearerTokenEnvVar ?? "",
      newServerEnvHeaders: Object.keys(server.envHttpHeaders ?? {}).length ? Object.entries(server.envHttpHeaders!).map(([name, value]) => ({ id: rowId(), name, value })) : [emptyPairRow()],
      newServerHeadersHelper: server.headersHelper ?? "", newServerOAuthClientId: server.oauth?.clientId ?? "",
      newServerOAuthCallbackPort: server.oauth?.callbackPort ? String(server.oauth.callbackPort) : "",
      newServerEnvVars: server.envVars?.length ? server.envVars.map<PassThroughRow>(item => ({
        id: rowId(), name: typeof item === "string" ? item : String(item.name ?? ""),
        source: typeof item === "string" ? "local" : item.source === "remote" ? "remote" : "local",
      })) : [emptyPassThroughRow()],
      newServerAutoStart: server.autoStart !== false, initialEditorFields: "",
    };
    restoreDraft({ ...fields, initialEditorFields: editorFields(fields) });
  };

  const saveServer = async () => {
    const submittedDraft = JSON.stringify(currentDraft);
    const name = newServerName.trim();
    const command = newServerCommand.trim();
    const url = newServerUrl.trim();
    if (!canAddServer || !editorChanged || addingServer) return;
    setAddingServer(true);
    try {
      const commandType = editingServerName ? "mcp.update" : "mcp.add";
      const env = mcpPairs(newServerEnv);
      const envVars = newServerEnvVars
        .filter((row) => row.name.trim())
        .map((row) => {
          return row.source === "local" ? row.name.trim() : { name: row.name.trim(), source: "remote" as const };
        });
      const headers = mcpPairs(newServerHeaders);
      const envHeaders = mcpPairs(newServerEnvHeaders);
      const editingServer = mcpServers.find((server) => server.name === editingServerName);
      const shared = {
        name,
        auto_start: newServerAutoStart,
        ...(editingServer?.environmentId !== undefined ? { environment_id: editingServer.environmentId } : {}),
        ...(editingServer?.enabled !== undefined ? { enabled: editingServer.enabled } : {}),
      };
      let mutation: McpServerMutationPayload;
      if (newServerTransport === "stdio") {
        mutation = {
          ...shared,
          transport: "stdio",
          command,
          args: newServerArgs.map(row => row.value.trim()).filter(Boolean),
          ...(newServerCwd.trim() || editingServer?.cwd ? { cwd: newServerCwd.trim() } : {}),
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
          ...(newServerBearerToken.trim() || editingServer?.bearerTokenEnvVar !== undefined ? { bearer_token_env_var: newServerBearerToken.trim() || null } : {}),
          ...(Object.keys(envHeaders).length || editingServer?.envHttpHeaders !== undefined ? { env_http_headers: envHeaders } : {}),
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
      if (reportCommandFailure(result, editingServerName ? tr("保存 MCP 服务") : tr("添加 MCP 服务"), tr("服务未返回具体原因"))) return;
      pushToast(commandFeedback(tr,
        result,
        editingServerName ? tr("已保存 MCP 服务：{v0}", { v0: name }) : tr("已添加 MCP 服务：{v0}", { v0: name }),
      ), "success");
      if (draftScope.current === inventoryScope && JSON.stringify(currentDraftRef.current) === submittedDraft) {
        resetEditor();
        setEditorOpen(false);
      }
    } catch (error) {
      pushToast(tr("{v0} MCP 服务失败：{v1}", { v0: editingServerName ? tr("保存") : tr("添加"), v1: operationError(tr, error) }), "error");
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
      if (!reportCommandFailure(result, enabled ? tr("启用 MCP 服务") : tr("停用 MCP 服务"), tr("服务未返回具体原因"))) {
        pushToast(tr("{v0} MCP 服务：{v1}", { v0: enabled ? tr("已启用") : tr("已停用"), v1: name }), "success");
      }
    } catch (error) {
      pushToast(tr("{v0} MCP 服务失败：{v1}", { v0: enabled ? tr("启用") : tr("停用"), v1: operationError(tr, error) }), "error");
    } finally {
      setTogglingServer("");
    }
  };

  const runServerAction = async (
    name: string,
    action: "mcp.oauth.login" | "mcp.oauth.logout" | "mcp.restart" | "mcp.remove",
    successMessage: string,
  ) => {
    if (pendingServerActions[name]) return;
    setPendingServerActions((current) => ({ ...current, [name]: action }));
    try {
      const result = await sendClientCommandAwaitResult(
        { type: action, name,
          ...(action === "mcp.remove" ? {} : {
            conversation_id: conversationId || undefined, workspace_root: workingDirectory || undefined,
          }),
        },
        action,
        { timeoutMs: LONG_COMMAND_RESULT_TIMEOUT_MS },
      );
      if (!reportCommandFailure(result, successMessage.replace(/^已/, ""), tr("服务未返回具体原因"))) {
        pushToast(successMessage, "success");
        if (action === "mcp.remove") {
          scopedDrafts.current.get(inventoryScope)?.drafts.delete(name);
          if (draftScope.current === inventoryScope && currentDraftRef.current.editingServerName === name) {
            resetEditor();
            setEditorOpen(false);
          }
        }
      }
    } catch (error) {
      pushToast(tr("{v0}失败：{v1}", { v0: successMessage.replace(/^已/, ""), v1: operationError(tr, error) }), "error");
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
      title: tr("删除 MCP 服务"),
      message: tr("确定删除 {v0}？该服务的本地配置会被移除。", { v0: name }),
      confirmLabel: tr("删除"),
      danger: true,
    });
    if (!confirmed) return;
    await runServerAction(
      name,
      "mcp.remove",
      tr("已删除 MCP 服务：{v0}", { v0: name }),
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
      pushToast(tr("项目 MCP 的会话或工作区归属已失效，请刷新后重试"), "error");
      return;
    }
    setPendingServerActions((current) => ({ ...current, [name]: action }));
    try {
      const result = await sendClientCommandAwaitResult(
        { type: action, name, conversation_id: conversationId, workspace_root: workspaceRoot },
        action,
        { timeoutMs: LONG_COMMAND_RESULT_TIMEOUT_MS },
      );
      if (!reportCommandFailure(result, successMessage, tr("服务未返回具体原因"))) {
        pushToast(commandFeedback(tr, result, successMessage), "success");
      }
    } catch (error) {
      pushToast(tr("{v0}失败：{v1}", { v0: successMessage, v1: operationError(tr, error) }), "error");
    } finally {
      setPendingServerActions((current) => {
        const next = { ...current };
        delete next[name];
        return next;
      });
    }
  };

  const renderServerRuntime = (server: typeof mcpServers[number]) => {
    const inventoryView = inventoryViews[inventoryKey(server.name)];
    const inventory = inventoryView?.data;
    const displayName = server.pluginServerName || server.name;
    return (
              <div className="settings-mcp-runtime">
                <div className="flex-1 min-w-0">
                  <div className="settings-mcp-server-meta">
                    <span style={mcpNameStyle}>{displayName}</span>
                    {server.approvalStatus === "pending" && (
                      <span style={{ ...miniMetaStyle, color: "var(--state-warning)" }}>{tr("等待批准")}</span>
                    )}
                    {server.approvalStatus === "rejected" && (
                      <span style={{ ...miniMetaStyle, color: "var(--text-tertiary)" }}>{tr("已拒绝")}</span>
                    )}
                    {server.enabled === false && (
                      <span style={{ ...miniMetaStyle, color: "var(--state-warning)" }}>{tr("策略禁用")}</span>
                    )}
                    {(server.phase ?? server.status) !== "connected" && (
                      <span style={statusChipStyle(server.phase ?? server.status)}>{connectorStatusLabel(tr, server.phase ?? server.status)}</span>
                    )}
                    {server.requiresUserAction && (
                      <span style={{ ...miniMetaStyle, color: "var(--state-warning)" }}>{tr("需要处理")}</span>
                    )}
                    {server.cleanup?.pending && (
                      <span style={{ ...miniMetaStyle, color: "var(--state-warning)" }}>{tr("清理未完成")}</span>
                    )}
                    {connectorAuthStatusLabel(tr, server.authStatus) && (
                      <span style={{
                        ...miniMetaStyle,
                        color: server.authStatus === "not_logged_in" ? "var(--state-warning)" : "var(--state-success)",
                      }}>{connectorAuthStatusLabel(tr, server.authStatus)}</span>
                    )}
                    {inventoryView?.expanded && <span style={miniMetaStyle}>{server.tools ?? 0} {tr("个工具")}</span>}
                    {inventoryView?.expanded && server.capabilities?.resources && <span style={miniMetaStyle}>{tr("资源")}</span>}
                    {inventoryView?.expanded && server.capabilities?.prompts && <span style={miniMetaStyle}>{tr("提示词")}</span>}
                  </div>
                  {server.lastError && <div style={mcpErrorStyle}>{server.lastError}</div>}
                  {server.enabled === false && server.disabledReason && (
                    <div style={mcpErrorStyle}>{server.disabledReason}</div>
                  )}
                  {inventoryView?.expanded && server.source === "project" && server.configPath && (
                    <div style={{ ...miniMetaStyle, marginTop: 3 }} title={server.configPath}>{server.configPath}</div>
                  )}
                  {server.requiresUserAction && server.setupHint && (
                    <div style={{ ...miniMetaStyle, marginTop: 3, whiteSpace: "normal", lineHeight: 1.35 }}>
                      {server.setupHint}
                    </div>
                  )}
                  {server.progress?.status === "running" && (
                    <div style={miniMetaStyle}>{server.progress.message === "Connecting..." ? tr("正在连接…") : server.progress.message || `${server.progress.operation}…`}</div>
                  )}
                  {inventoryView?.expanded && (
                    <div
                      aria-label={tr("MCP 目录 {v0}", { v0: displayName })}
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
                          {tr("正在按需读取 MCP 目录…")}</div>
                      )}
                      {inventoryView.status === "error" && (
                        <div className="flex items-center justify-between gap-2">
                          <span style={mcpErrorStyle}>{inventoryView.error || tr("MCP 目录读取失败。")}</span>
                          <button type="button" onClick={() => void loadServerInventory(server)} style={secondaryActionStyle}>{tr("重试")}</button>
                        </div>
                      )}
                      {inventoryView.status === "loaded" && inventory?.empty && (
                        <div style={miniMetaStyle}>{tr("该服务未公开资源、资源模板或提示词。")}</div>
                      )}
                      {inventoryView.status === "loaded" && inventory && !inventory.empty && (
                        <div className="flex flex-col gap-2">
                          {Boolean(inventory.tools?.length) && <McpToolsEditor key={inventoryScope + server.name} server={server} tools={inventory.tools!} />}
                          {inventory.resources.length > 0 && (
                            <div>
                              <div style={{ ...miniMetaStyle, fontWeight: 600, marginBottom: 3 }}>{tr("资源 ·") + " "}{inventory.resources.length}</div>
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
                              <div style={{ ...miniMetaStyle, fontWeight: 600, marginBottom: 3 }}>{tr("资源模板 ·") + " "}{inventory.resource_templates.length}</div>
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
                              <div style={{ ...miniMetaStyle, fontWeight: 600, marginBottom: 3 }}>{tr("提示词 ·") + " "}{inventory.prompts.length}</div>
                              {inventory.prompts.map((item) => (
                                <div key={item.name} style={{ ...miniMetaStyle, marginTop: 3, whiteSpace: "normal" }}>
                                  <strong>{item.name}</strong>
                                  {item.description && <div>{item.description}</div>}
                                  {Boolean(item.arguments?.length) && (
                                    <div>{tr("参数：")}{item.arguments?.map((argument) => `${argument.name}${argument.required ? "*" : ""}`).join("、")}</div>
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
                    title={inventoryView?.expanded ? tr("收起 MCP 目录") : tr("按需查看 MCP 资源、资源模板和提示词")}
                    aria-label={tr("{v0} MCP 内容 {v1}", { v0: inventoryView?.expanded ? tr("收起") : tr("查看"), v1: displayName })}
                  >
                    {inventoryView?.expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  </button>
                  {server.source === "project" && server.approvalStatus === "pending" && (
                    <>
                      <button
                        type="button"
                        onClick={() => void decideProjectServer(server.name, server.projectWorkspace, "mcp.project.approve_all", tr("已允许当前及后续项目 MCP：{v0}", { v0: server.name }))}
                        disabled={Boolean(pendingServerActions[server.name])}
                        style={mcpActionBtnStyle}
                        title={tr("使用此服务，并允许此项目今后发现的所有 MCP 服务")}
                        aria-label={tr("允许当前及后续项目 MCP {v0}", { v0: server.name })}
                      >{pendingServerActions[server.name] === "mcp.project.approve_all" ? <RefreshCw size={14} className="animate-spin" /> : <CheckCheck size={14} />}</button>
                      <button
                        type="button"
                        onClick={() => void decideProjectServer(server.name, server.projectWorkspace, "mcp.project.approve", tr("已允许项目 MCP：{v0}", { v0: server.name }))}
                        disabled={Boolean(pendingServerActions[server.name])}
                        style={mcpActionBtnStyle}
                        title={tr("仅使用此 MCP 服务")}
                        aria-label={tr("允许项目 MCP {v0}", { v0: server.name })}
                      >{pendingServerActions[server.name] === "mcp.project.approve" ? <RefreshCw size={14} className="animate-spin" /> : <Check size={14} />}</button>
                      <button
                        type="button"
                        onClick={() => void decideProjectServer(server.name, server.projectWorkspace, "mcp.project.reject", tr("已跳过项目 MCP：{v0}", { v0: server.name }))}
                        disabled={Boolean(pendingServerActions[server.name])}
                        style={mcpActionBtnStyle}
                        title={tr("继续但不使用此 MCP 服务")}
                        aria-label={tr("拒绝项目 MCP {v0}", { v0: server.name })}
                      >{pendingServerActions[server.name] === "mcp.project.reject" ? <RefreshCw size={14} className="animate-spin" /> : <Ban size={14} />}</button>
                    </>
                  )}
                  {server.approvalStatus !== "pending" && server.approvalStatus !== "rejected" && ((server.authStatus === "not_logged_in" || server.phase === "auth_required" || server.phase === "expired") ? (
                    <button
                      type="button"
                      onClick={() => void runServerAction(
                        server.name,
                        "mcp.oauth.login",
                        tr("已登录 MCP 服务：{v0}", { v0: displayName }),
                      )}
                      disabled={Boolean(pendingServerActions[server.name])}
                      style={mcpActionBtnStyle}
                      title={tr("登录")}
                      aria-label={tr("登录 {v0}", { v0: displayName })}
                    >{pendingServerActions[server.name] === "mcp.oauth.login" ? <RefreshCw size={14} className="animate-spin" /> : <LogIn size={14} />}</button>
                  ) : server.authStatus === "oauth" ? (
                    <>
                      <button
                        type="button"
                        onClick={() => void runServerAction(
                          server.name,
                          "mcp.restart",
                          tr("已重启 MCP 服务：{v0}", { v0: displayName }),
                        )}
                        disabled={Boolean(pendingServerActions[server.name]) || server.phase === "connecting" || server.phase === "reconnecting"}
                        style={mcpActionBtnStyle}
                        title={tr("重启")}
                        aria-label={tr("重启 {v0}", { v0: displayName })}
                      ><RefreshCw size={14} className={pendingServerActions[server.name] === "mcp.restart" ? "animate-spin" : undefined} /></button>
                      <button
                        type="button"
                        onClick={() => void runServerAction(
                          server.name,
                          "mcp.oauth.logout",
                          tr("已清除 MCP 登录：{v0}", { v0: displayName }),
                        )}
                        disabled={Boolean(pendingServerActions[server.name])}
                        style={mcpActionBtnStyle}
                        title={tr("清除登录")}
                        aria-label={tr("清除 {v0} 登录", { v0: displayName })}
                      >{pendingServerActions[server.name] === "mcp.oauth.logout" ? <RefreshCw size={14} className="animate-spin" /> : <LogOut size={14} />}</button>
                    </>
                  ) : (
                    <button
                      onClick={() => void runServerAction(
                        server.name,
                        "mcp.restart",
                        tr("已重启 MCP 服务：{v0}", { v0: displayName }),
                      )}
                      disabled={Boolean(pendingServerActions[server.name]) || server.phase === "connecting" || server.phase === "reconnecting"}
                      style={mcpActionBtnStyle}
                      title={tr("重启")}
                      aria-label={tr("重启 {v0}", { v0: displayName })}
                    ><RefreshCw size={14} className={pendingServerActions[server.name] === "mcp.restart" ? "animate-spin" : undefined} /></button>
                  ))}

                </div>
              </div>
    );
  };
  const editingServer = mcpServers.find(server => server.name === editingServerName);
  const renderServerRow = (server: typeof mcpServers[number]) => <div key={server.name} className="settings-mcp-server-row">
    <span className="settings-mcp-list-name">{server.pluginServerName || server.name}</span>
    <div className="settings-mcp-server-actions">
      <button type="button" className="settings-icon-button" onClick={() => editServer(server)} title={tr("设置")} aria-label={tr("编辑 {v0}", { v0: server.pluginServerName || server.name })}><Settings size={16} /></button>
      {server.editable && <button type="button" className="settings-toggle settings-mcp-toggle" role="switch"
        aria-checked={server.autoStart !== false} aria-label={`${server.autoStart === false ? tr("启用") : tr("停用")} ${server.name}`}
        data-active={server.autoStart !== false ? "true" : "false"}
        disabled={server.enabled === false || togglingServer === server.name || Boolean(pendingServerActions[server.name])}
        onClick={() => void toggleServer(server.name, server.autoStart === false)}
        title={server.enabled === false ? server.disabledReason || tr("已被管理策略禁用") : server.autoStart === false ? tr("启用") : tr("停用")}><span /></button>}
    </div>
  </div>;

  if (editorOpen) return (
    <section className="mcp-detail" id="mcp-server-editor" aria-label={tr("MCP 服务编辑器")}>
      <button type="button" className="mcp-detail-back" onClick={() => setEditorOpen(false)}><ArrowLeft size={15} />{tr("返回")}</button>
      <header className="mcp-detail-heading">
        <h3>{editingServerName ? editingServer?.editable ? tr("更新 {v0} MCP", { v0: editingServerName }) : editingServer?.pluginServerName || editingServerName : tr("连接到自定义 MCP")}</h3>
        {!editingServerName && <a href="https://developers.openai.com/codex/mcp" target="_blank" rel="noreferrer" aria-label={tr("打开 MCP 文档")}
          onClick={event => { event.preventDefault(); void openExternal("https://developers.openai.com/codex/mcp"); }}>{tr("文档")}<ExternalLink size={12} /></a>
        }
        {editingServer?.editable && <button type="button" className="mcp-detail-uninstall" disabled={Boolean(pendingServerActions[editingServer.name])}
          onClick={() => void removeServer(editingServer.name)}><Trash2 size={14} />{tr("卸载")}</button>}
      </header>
      {editingServerName ? editingServer?.editable && <p className="mcp-detail-notice">{tr("如需切换 MCP 服务器类型，请先卸载。")}</p> : <div className="mcp-detail-card">
        <label className="mcp-detail-field"><span className="mcp-detail-label">{tr("名称")}</span>
          <input aria-label={tr("服务名称")} value={newServerName} placeholder={tr("MCP 服务器名称")} onChange={event => setNewServerName(event.target.value)} />
        </label>
        <div className="mcp-detail-type">
          <span>{tr("类型")}</span>
          <div className="mcp-detail-transport" role="group" aria-label={tr("传输方式")}>
            <button type="button" aria-pressed={newServerTransport === "stdio"} onClick={() => setNewServerTransport("stdio")}>STDIO</button>
            <button type="button" aria-pressed={newServerTransport !== "stdio"} onClick={() => setNewServerTransport("http")}>Streamable HTTP</button>
          </div>
        </div>
      </div>}
      {(!editingServerName || editingServer?.editable) && <fieldset className="mcp-detail-card">
        {newServerTransport === "stdio" ? <>
          <label className="mcp-detail-field"><span className="mcp-detail-label">{tr("启动命令")}</span>
            <input aria-label={tr("启动命令")} value={newServerCommand} placeholder="openai-dev-mcp serve-sqlite" onChange={event => setNewServerCommand(event.target.value)} />
          </label>
          <div className="mcp-detail-field">
            <span className="mcp-detail-label">{tr("参数")}</span>
            <div className="mcp-detail-entries">
              {newServerArgs.map((row, index) => <div className="mcp-detail-single" key={row.id}>
                <input aria-label={tr("命令参数 {v0}", { v0: index + 1 })} value={row.value} onChange={event => setNewServerArgs(rows => rows.map(item => item.id === row.id ? { ...item, value: event.target.value } : item))} />
                <button type="button" className="mcp-detail-remove" aria-label={tr("删除参数 {v0}", { v0: index + 1 })} disabled={newServerArgs.length === 1 && !row.value}
                  onClick={() => setNewServerArgs(rows => rows.length === 1 ? [emptyValueRow()] : rows.filter(item => item.id !== row.id))}><Trash2 size={14} /></button>
              </div>)}
              <button type="button" className="mcp-detail-add-entry" onClick={() => setNewServerArgs(rows => [...rows, emptyValueRow()])}><Plus size={14} />{tr("添加参数")}</button>
            </div>
          </div>
          <McpPairFields title={tr("环境变量")} nameLabel={tr("环境变量名称")} valueLabel={tr("环境变量的值")} rows={newServerEnv} onChange={setNewServerEnv}
            addLabel={tr("添加环境变量")} removeLabel={tr("删除环境变量")} tr={tr} />
          <div className="mcp-detail-field">
            <span className="mcp-detail-label">{tr("透传的环境变量")}</span>
            <div className="mcp-detail-entries">
              {newServerEnvVars.map((row, index) => <div className="mcp-detail-single" key={row.id}>
                <input aria-label={tr("透传变量 {v0}", { v0: index + 1 })} value={row.name} onChange={event => setNewServerEnvVars(rows => rows.map(item => item.id === row.id ? { ...item, name: event.target.value } : item))} />
                <button type="button" className="mcp-detail-remove" aria-label={tr("删除透传变量 {v0}", { v0: index + 1 })} disabled={newServerEnvVars.length === 1 && !row.name}
                  onClick={() => setNewServerEnvVars(rows => rows.length === 1 ? [emptyPassThroughRow()] : rows.filter(item => item.id !== row.id))}><Trash2 size={14} /></button>
              </div>)}
              <button type="button" className="mcp-detail-add-entry" onClick={() => setNewServerEnvVars(rows => [...rows, emptyPassThroughRow()])}><Plus size={14} />{tr("添加变量")}</button>
            </div>
          </div>
          <label className="mcp-detail-field"><span className="mcp-detail-label">{tr("工作目录")}</span>
            <input aria-label={tr("工作目录")} value={newServerCwd} placeholder="~/code" onChange={event => setNewServerCwd(event.target.value)} />
          </label>
        </> : <>
          <label className="mcp-detail-field"><span className="mcp-detail-label">URL</span>
            <input aria-label={tr("服务地址")} value={newServerUrl} placeholder="https://mcp.example.com/mcp" onChange={event => setNewServerUrl(event.target.value)} />
          </label>
          {newServerTransport !== "ws" && <label className="mcp-detail-field"><span className="mcp-detail-label">{tr("Bearer 令牌环境变量")}</span>
            <input aria-label={tr("Bearer 令牌环境变量")} value={newServerBearerToken} placeholder="MCP_BEARER_TOKEN" onChange={event => setNewServerBearerToken(event.target.value)} />
          </label>}
          <McpPairFields title={tr("请求头")} nameLabel={tr("请求头名称")} valueLabel={tr("请求头的值")} rows={newServerHeaders} onChange={setNewServerHeaders}
            addLabel={tr("添加请求头")} removeLabel={tr("删除请求头")} tr={tr} />
          {newServerTransport !== "ws" && <McpPairFields title={tr("来自环境变量的请求头")} nameLabel={tr("环境变量请求头名称")} valueLabel={tr("请求头环境变量名称")} rows={newServerEnvHeaders} onChange={setNewServerEnvHeaders}
            addLabel={tr("添加变量")} removeLabel={tr("删除请求头变量")} tr={tr} />}
        </>}
      </fieldset>}
      {editingServer && renderServerRuntime(editingServer)}
      {(!editingServer || editingServer.editable) && <div className="mcp-detail-footer"><button type="button" className="mcp-detail-save" disabled={!canAddServer || !editorChanged || addingServer} onClick={() => void saveServer()}>
        {addingServer ? tr("正在保存…") : tr("保存")}
      </button></div>}
    </section>
  );

  return (
    <>
      <section className="settings-group settings-mcp-services" aria-label={tr("MCP 服务")}>
        <div className="settings-mcp-services-heading">
          <h3>{tr("服务器")}</h3>
          {externalSearch === undefined && <div className="settings-mcp-services-actions">
            <label className="settings-mcp-search"><Search size={15} /><input aria-label={tr("搜索 MCP 服务器")} placeholder={tr("搜索 MCP 服务器")} value={search} onChange={event => setSearch(event.target.value)} /></label>
            {!editorOpen && <button type="button" className="settings-action-button settings-mcp-add" aria-expanded={false} aria-controls="mcp-server-editor"
              disabled={addingServer} onClick={openNewEditor}><Plus size={14} aria-hidden="true" />{tr("添加服务")}</button>}
          </div>}
        </div>
            <div className="settings-card settings-mcp-list">
            {mcpServers.every(server => server.pluginId) && <div className="settings-mcp-empty">{tr("尚未配置 MCP 服务。")}</div>}
            {mcpServers.some(server => !server.pluginId) && standaloneServers.length === 0 && <div className="settings-mcp-empty">{tr("没有匹配的 MCP 服务器。")}</div>}
            {standaloneServers.map(renderServerRow)}
            </div>
          </section>
          {pluginServers.length > 0 && <section className="settings-group" aria-label={tr("来自插件")}>
            <h3 className="settings-group-title">{tr("来自插件")}</h3>
            <div className="settings-card settings-mcp-list">
              {pluginServers.map(renderServerRow)}
            </div>
          </section>}
          <McpAppDataSettings />
    </>
  );
};
