import { useState } from "react";
import { RefreshCw, Trash2 } from "lucide-react";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import {
  Section,
  inputStyle,
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
  if (auth === "local_app") return "local app";
  return auth.replace(/_/g, " ");
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
          Servers
          <span style={subTabCountStyle}>{mcpServers.length}</span>
        </button>
        <button type="button" onClick={() => setMode("marketplace")} style={subTabStyle(mode === "marketplace")}>
          Marketplace
          <span style={subTabCountStyle}>{marketplaceConnectors.length}</span>
        </button>
      </div>

      {mode === "servers" && (
        <>
          <Section title="MCP Servers" description="Connectors stay here; prompts and tools should not flood the slash menu.">
            {mcpServers.length === 0 && <div style={emptyInlineStyle}>No MCP servers configured.</div>}
            {mcpServers.map((server) => (
              <div key={server.name} style={mcpServerRowStyle}>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <span style={mcpDotStyle(server.phase ?? server.status)} />
                    <span style={mcpNameStyle}>{server.name}</span>
                    <span style={statusChipStyle(server.phase ?? server.status)}>{(server.phase ?? server.status).replace(/_/g, " ")}</span>
                    {server.requiresUserAction && (
                      <span style={{ ...miniMetaStyle, color: "var(--state-warning)" }}>action required</span>
                    )}
                    <span style={miniMetaStyle}>{server.transport || "stdio"}</span>
                    <span style={miniMetaStyle}>{server.tools ?? 0} tools</span>
                  </div>
                  {server.lastError && <div style={mcpErrorStyle}>{server.lastError}</div>}
                  {server.requiresUserAction && server.setupHint && (
                    <div style={{ ...miniMetaStyle, marginTop: 3, whiteSpace: "normal", lineHeight: 1.35 }}>
                      {server.setupHint}
                    </div>
                  )}
                  {server.progress?.status === "running" && (
                    <div style={miniMetaStyle}>{server.progress.message || `${server.progress.operation}...`}</div>
                  )}
                </div>
                <div className="flex gap-1">
                  <button onClick={() => sendClientCommand({ type: "mcp.restart", name: server.name })} style={mcpActionBtnStyle} title="Restart" aria-label={`Restart ${server.name}`}><RefreshCw size={14} /></button>
                  <button onClick={() => sendClientCommand({ type: "mcp.remove", name: server.name })} style={mcpActionBtnStyle} title="Remove" aria-label={`Remove ${server.name}`}><Trash2 size={14} /></button>
                </div>
              </div>
            ))}
          </Section>

          <Section title="Add Server">
            <div className="grid gap-2">
              <div className="flex gap-2">
                <input type="text" value={newServerName} onChange={(e) => setNewServerName(e.target.value)} placeholder="Server name" style={{ ...inputStyle, flex: 1 }} />
                <select value={newServerTransport} onChange={(e) => setNewServerTransport(e.target.value as "stdio" | "http")} style={{ ...inputStyle, width: 90 }}>
                  <option value="stdio">stdio</option>
                  <option value="http">http</option>
                </select>
              </div>
              {newServerTransport === "stdio" ? (
                <>
                  <input type="text" value={newServerCommand} onChange={(e) => setNewServerCommand(e.target.value)} placeholder="Command (python, npx, uvx...)" style={inputStyle} />
                  <input type="text" value={newServerArgs} onChange={(e) => setNewServerArgs(e.target.value)} placeholder="Args" style={inputStyle} />
                </>
              ) : (
                <input type="text" value={newServerUrl} onChange={(e) => setNewServerUrl(e.target.value)} placeholder="http://localhost:8080/mcp" style={inputStyle} />
              )}
              <div className="flex justify-between gap-2">
                <button onClick={() => sendClientCommand({ type: "mcp.list" })} style={secondaryActionStyle}>Refresh</button>
                <button
                  onClick={() => {
                    if (!newServerName.trim()) return;
                    sendClientCommand({
                      type: "mcp.add",
                      name: newServerName.trim(),
                      transport: newServerTransport,
                      command: newServerTransport === "stdio" ? newServerCommand.trim() : undefined,
                      args: newServerTransport === "stdio" ? newServerArgs.split(/\s+/).filter(Boolean) : undefined,
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
                  Add Server
                </button>
              </div>
            </div>
          </Section>
        </>
      )}

      {mode === "marketplace" && (
        <Section title="Marketplace" description="Curated MCP connectors install into the server list.">
          {marketplaceConnectors.length === 0 && <div style={emptyInlineStyle}>No marketplace entries loaded yet.</div>}
          {marketplaceConnectors.length > 0 && (
            <div style={marketplaceListStyle}>
              {marketplaceConnectors.map((c) => (
                <div key={c.name} style={marketplaceRowStyle}>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 min-w-0">
                      <div style={marketplaceTitleStyle}>{c.title}</div>
                      <span style={miniMetaStyle}>{c.transport}</span>
                      {connectorAuthLabel(c.auth) && (
                        <span style={miniMetaStyle}>{connectorAuthLabel(c.auth)}</span>
                      )}
                      {c.requiresUserAction && (
                        <span style={{ ...miniMetaStyle, color: "var(--state-warning)" }}>setup required</span>
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
                        Docs
                      </a>
                    )}
                  </div>
                  {c.installed ? (
                    <span style={installedPillStyle}>Installed</span>
                  ) : (
                    <button
                      onClick={() => {
                        sendClientCommand({ type: "connectors.marketplace.install", name: c.name });
                        setTimeout(() => {
                          sendClientCommand({ type: "mcp.list" });
                          sendClientCommand({ type: "connectors.marketplace.list" });
                        }, 1000);
                      }}
                      style={compactInstallStyle}
                    >
                      Install
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
