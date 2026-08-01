import { useState } from "react";
import { LogIn, RefreshCw, Trash2 } from "lucide-react";
import { parseArgsStringToArgv } from "string-argv";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { BrandIcon } from "../components/BrandIcon";
import {
  Section,
  inputStyle,
  selectInputStyle,
  primaryActionStyle,
  secondaryActionStyle,
  subTabBarStyle,
  subTabStyle,
  subTabCountStyle,
  emptyInlineStyle,
  mcpServerRowStyle,
  mcpNameStyle,
  mcpErrorStyle,
  mcpDotStyle,
  statusChipStyle,
  miniMetaStyle,
  mcpActionBtnStyle,
  marketplaceListStyle,
  marketplaceRowStyle,
  marketplaceTitleStyle,
  marketplaceDescStyle,
  installedPillStyle,
  compactInstallStyle,
} from "./settingsShared";

const connectorAuthLabel = (auth?: string): string | null => {
  if (!auth || auth === "none") return null;
  if (auth === "local_app") return "本地应用";
  return auth.replace(/_/g, " ");
};

const parseMcpArgs = (value: string): string[] => parseArgsStringToArgv(value).map((arg) => {
  const quotedEquals = arg.match(/^([^=]+=)(["'])(.*)\2$/s);
  return quotedEquals ? `${quotedEquals[1]}${quotedEquals[3]}` : arg;
});

const connectorStatusLabel = (status?: string): string => {
  if (status === "connected") return "已连接";
  if (status === "connecting") return "连接中";
  if (status === "auth_required") return "需要登录";
  if (status === "reconnecting") return "正在重连";
  if (status === "failed" || status === "error") return "失败";
  if (status === "disabled") return "已停用";
  return String(status || "未知").replace(/_/g, " ");
};

export const ConnectorsTab = () => {
  const mcpServers = useAppStore((s) => s.mcpServers);
  const marketplaceConnectors = useAppStore((s) => s.marketplaceConnectors);
  const [mode, setMode] = useState<"servers" | "marketplace">("servers");
  const [newServerName, setNewServerName] = useState("");
  const [newServerCommand, setNewServerCommand] = useState("");
  const [newServerArgs, setNewServerArgs] = useState("");
  const [newServerTransport, setNewServerTransport] = useState<"stdio" | "http">("stdio");
  const [newServerUrl, setNewServerUrl] = useState("");

  return (
    <>
      <div style={subTabBarStyle}>
        <button type="button" onClick={() => setMode("servers")} style={subTabStyle(mode === "servers")}>
          服务
          <span style={subTabCountStyle}>{mcpServers.length}</span>
        </button>
        <button type="button" onClick={() => setMode("marketplace")} style={subTabStyle(mode === "marketplace")}>
          市场
          <span style={subTabCountStyle}>{marketplaceConnectors.length}</span>
        </button>
      </div>

      {mode === "servers" && (
        <>
          <Section title="MCP 服务" description="这里显示 MCP 运行时实际加载的服务、连接状态和可用工具数量。">
            {mcpServers.length === 0 && <div style={emptyInlineStyle}>尚未配置 MCP 服务。</div>}
            {mcpServers.map((server) => (
              <div key={server.name} style={mcpServerRowStyle}>
                <BrandIcon value={`${server.name} ${server.url || ""}`} websiteUrl={server.docsUrl || server.url} fallback="web" size={18} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <span style={mcpDotStyle(server.phase ?? server.status)} />
                    <span style={mcpNameStyle}>{server.name}</span>
                    {(server.phase ?? server.status) !== "connected" && (
                      <span style={statusChipStyle(server.phase ?? server.status)}>{connectorStatusLabel(server.phase ?? server.status)}</span>
                    )}
                    {server.requiresUserAction && (
                      <span style={{ ...miniMetaStyle, color: "var(--state-warning)" }}>需要处理</span>
                    )}
                    <span style={miniMetaStyle}>{server.tools ?? 0} 个工具</span>
                  </div>
                  {server.lastError && <div style={mcpErrorStyle}>{server.lastError}</div>}
                  {server.requiresUserAction && server.setupHint && (
                    <div style={{ ...miniMetaStyle, marginTop: 3, whiteSpace: "normal", lineHeight: 1.35 }}>
                      {server.setupHint}
                    </div>
                  )}
                  {server.progress?.status === "running" && (
                    <div style={miniMetaStyle}>{server.progress.message === "Connecting..." ? "正在连接…" : server.progress.message || `${server.progress.operation}…`}</div>
                  )}
                </div>
                <div className="flex gap-1">
                  {(server.phase === "auth_required" || server.phase === "expired") ? (
                    <button
                      onClick={() => sendClientCommand({ type: "mcp.oauth.login", name: server.name })}
                      style={mcpActionBtnStyle}
                      title="登录"
                      aria-label={`登录 ${server.name}`}
                    ><LogIn size={14} /></button>
                  ) : (
                    <button
                      onClick={() => sendClientCommand({ type: "mcp.restart", name: server.name })}
                      disabled={server.phase === "connecting" || server.phase === "reconnecting"}
                      style={mcpActionBtnStyle}
                      title="重启"
                      aria-label={`重启 ${server.name}`}
                    ><RefreshCw size={14} /></button>
                  )}
                  <button onClick={() => sendClientCommand({ type: "mcp.remove", name: server.name })} style={mcpActionBtnStyle} title="删除" aria-label={`删除 ${server.name}`}><Trash2 size={14} /></button>
                </div>
              </div>
            ))}
          </Section>

          <Section title="添加服务" description="stdio 启动本地进程；HTTP 连接现有的 Streamable HTTP 服务。">
            <div className="grid gap-2">
              <div className="flex gap-2">
                <input type="text" value={newServerName} onChange={(e) => setNewServerName(e.target.value)} placeholder="服务名称" style={{ ...inputStyle, flex: 1 }} />
                <select value={newServerTransport} onChange={(e) => setNewServerTransport(e.target.value as "stdio" | "http")} style={{ ...selectInputStyle, width: 90 }}>
                  <option value="stdio">stdio</option>
                  <option value="http">http</option>
                </select>
              </div>
              {newServerTransport === "stdio" ? (
                <>
                  <input type="text" value={newServerCommand} onChange={(e) => setNewServerCommand(e.target.value)} placeholder="命令（python、npx、uvx…）" style={inputStyle} />
                  <input type="text" value={newServerArgs} onChange={(e) => setNewServerArgs(e.target.value)} placeholder="参数" style={inputStyle} />
                </>
              ) : (
                <input type="text" value={newServerUrl} onChange={(e) => setNewServerUrl(e.target.value)} placeholder="http://localhost:8080/mcp" style={inputStyle} />
              )}
              <div className="flex justify-between gap-2">
                <button onClick={() => sendClientCommand({ type: "mcp.list" })} style={secondaryActionStyle}>刷新</button>
                <button
                  onClick={() => {
                    if (!newServerName.trim()) return;
                    sendClientCommand({
                      type: "mcp.add",
                      name: newServerName.trim(),
                      transport: newServerTransport,
                      command: newServerTransport === "stdio" ? newServerCommand.trim() : undefined,
                      args: newServerTransport === "stdio" ? parseMcpArgs(newServerArgs) : undefined,
                      url: newServerTransport === "http" ? newServerUrl.trim() : undefined,
                    });
                    setNewServerName("");
                    setNewServerCommand("");
                    setNewServerArgs("");
                    setNewServerUrl("");
                  }}
                  disabled={!newServerName.trim()}
                  style={primaryActionStyle}
                >
                  添加服务
                </button>
              </div>
            </div>
          </Section>
        </>
      )}

      {mode === "marketplace" && (
        <Section title="连接市场" description="市场项目来自后端连接目录；安装后仍由同一个 MCP 运行时管理。">
          {marketplaceConnectors.length === 0 && <div style={emptyInlineStyle}>暂未加载市场项目。</div>}
          {marketplaceConnectors.length > 0 && (
            <div style={marketplaceListStyle}>
              {marketplaceConnectors.map((c) => (
                <div key={c.name} style={marketplaceRowStyle}>
                  <BrandIcon
                    value={`${c.title} ${c.name} ${(c.tags ?? []).join(" ")}`}
                    size={20}
                    iconUrl={c.iconUrl}
                    websiteUrl={c.websiteUrl || c.docsUrl || c.url}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 min-w-0">
                      <div style={marketplaceTitleStyle}>{c.title}</div>
                      <span style={miniMetaStyle}>{c.transport}</span>
                      {connectorAuthLabel(c.auth) && (
                        <span style={miniMetaStyle}>{connectorAuthLabel(c.auth)}</span>
                      )}
                      {c.requiresUserAction && (
                        <span style={{ ...miniMetaStyle, color: "var(--state-warning)" }}>需要配置</span>
                      )}
                    </div>
                    <div style={marketplaceDescStyle}>{c.description}</div>
                    {c.setupHint && (
                      <div style={{ ...marketplaceDescStyle, whiteSpace: "normal" }}>{c.setupHint}</div>
                    )}
                    {c.docsUrl && (
                      <a
                        href={c.docsUrl}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-block mt-0.5"
                        style={{ ...miniMetaStyle, color: "var(--accent-primary)" }}
                      >
                        文档
                      </a>
                    )}
                  </div>
                  {c.installed ? (
                    <span style={installedPillStyle}>已安装</span>
                  ) : (
                    <button
                      onClick={() => {
                        sendClientCommand({ type: "connectors.marketplace.install", name: c.name });
                      }}
                      style={compactInstallStyle}
                    >
                      安装
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </Section>
      )}
    </>
  );
};
