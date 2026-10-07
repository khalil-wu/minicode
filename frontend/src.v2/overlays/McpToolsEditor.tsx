import { useState } from "react";
import type { McpServerStatus } from "../stores/types";
import { useAppStore } from "../stores";
import { commandResultSucceeded, sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";

export function McpToolsEditor({ server, tools }: { server: McpServerStatus; tools: Array<{ name: string; description?: string }> }) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(() => new Set(tools.filter((tool) => (server.enabledTools == null || server.enabledTools.includes(tool.name)) && !server.disabledTools?.includes(tool.name)).map((tool) => tool.name)));
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const owner = useAppStore((state) => state.workingDirectory);
  const rows = tools.filter((tool) => (tool.name + " " + tool.description).toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const save = async (all: boolean) => {
    setBusy(true); setFeedback("");
    try {
      const result = await sendClientCommandAwaitResult({ type: "mcp.update", original_name: server.name, tools_only: true, enabled_tools: all ? null : [...selected] }, "mcp.update");
      if (!commandResultSucceeded(result)) throw new Error(result.message || "工具策略保存失败");
      if (all) setSelected(new Set(tools.map((tool) => tool.name)));
      if (useAppStore.getState().workingDirectory === owner) { setFeedback("已保存，下条消息生效。"); sendClientCommand({ type: "mcp.list" }, { silent: true }); }
    } catch (error) { setFeedback(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  };
  return <div className="settings-mcp-tools">
    <div className="settings-row"><strong>工具 · {selected.size} / {tools.length}</strong><input aria-label={"搜索 " + server.name + " 工具"} placeholder="搜索工具" value={query} onChange={(event) => setQuery(event.target.value)} /></div>
    <div className="settings-mcp-tool-list">{rows.map((tool) => <label key={tool.name}><input type="checkbox" checked={selected.has(tool.name)} disabled={busy || !server.editable} onChange={(event) => setSelected((previous) => { const next = new Set(previous); if (event.target.checked) next.add(tool.name); else next.delete(tool.name); return next; })} /><span><strong>{tool.name}</strong><small>{tool.description}</small></span></label>)}</div>
    {server.editable ? <div className="settings-row-control">
      <button type="button" className="settings-action-button" disabled={busy} onClick={() => setSelected(new Set(tools.map((tool) => tool.name)))}>全选</button>
      <button type="button" className="settings-action-button" disabled={busy} onClick={() => setSelected(new Set())}>清空</button>
      <button type="button" className="settings-action-button" disabled={busy} onClick={() => void save(false)}>{busy ? "保存中…" : "保存所选工具"}</button>
      <button type="button" className="settings-action-button" disabled={busy} onClick={() => void save(true)}>允许全部及新增工具</button>
    </div> : <small>策略来自 {server.configPath || server.source}。</small>}
    {feedback && <p role="status">{feedback}</p>}
  </div>;
}
