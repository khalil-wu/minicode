import { useI18n, type Translate } from "../i18n";
import { useRef, useState } from "react";
import { Download, Upload } from "lucide-react";
import { useAppStore } from "../stores";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { getWebSocket } from "../hooks/useWebSocket";
import { sendClientCommand } from "../protocol/ws-outbox";
import { defaultWorkbenchPreferences, type WorkbenchPreferences } from "../lib/workbench-preferences";
import { saveBlobFile } from "../lib/save-blob";

function parsePreferences(tr: Translate, value: unknown): Partial<WorkbenchPreferences> {
  if (value == null || typeof value !== "object" || Array.isArray(value)) throw new Error(tr("偏好配置格式无效。"));
  const source = value as Record<string, unknown>;
  const result: Record<string, unknown> = {};
  for (const [key, expected] of Object.entries(defaultWorkbenchPreferences)) {
    if (!(key in source)) continue;
    const item = source[key];
    if (key === "snippets") {
      if (!Array.isArray(item) || item.some((snippet) => !snippet || ["id", "language", "prefix", "body", "description"].some((field) => typeof snippet[field] !== "string"))) throw new Error(tr("代码模板格式无效。"));
    } else if (typeof item !== typeof expected) throw new Error(tr("偏好字段类型无效：") + key);
    result[key] = item;
  }
  for (const [key, min, max] of [["proseSize", 12, 24], ["tabSize", 1, 8], ["aiMaxTokens", 32, 4096]] as const) {
    if (result[key] != null && (!Number.isFinite(result[key]) || Number(result[key]) < min || Number(result[key]) > max)) throw new Error(key + " 超出可用范围。");
  }
  return result as Partial<WorkbenchPreferences>;
}

export function MigrationSettings() {
  const { t: tr } = useI18n();
  const workspace = useAppStore((state) => state.workingDirectory);
  const fileInput = useRef<HTMLInputElement>(null);
  const [pending, setPending] = useState<{ file: File; preferences?: Partial<WorkbenchPreferences>; titles?: string[] } | null>(null);
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const [busy, setBusy] = useState(false);
  const download = async () => {
    setError("");
    const content = { schema: "minicode.preferences", version: 1, preferences: useAppStore.getState().workbenchPreferences };
    try {
      await saveBlobFile(new Blob([JSON.stringify(content, null, 2)], { type: "application/json" }), "minicode-preferences.json");
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const select = async (file: File) => {
    setError(""); setResult(""); setPending(null);
    try {
      if (file.size > 25 * 1024 * 1024) throw new Error(tr("导入文件不能超过 25 MiB。"));
      const data = JSON.parse(await file.text());
      if (data.schema === "minicode.preferences" && data.version === 1) setPending({ file, preferences: parsePreferences(tr, data.preferences) });
      else if (data.schema === "minicode.conversation.export" && data.version === 1 && Array.isArray(data.conversations)) setPending({ file, titles: data.conversations.map((record: { title: string }) => record.title) });
      else throw new Error(tr("请选择 MiniCode 导出的会话或偏好 JSON 文件。"));
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const apply = async () => {
    setBusy(true); setError("");
    try {
      if (pending!.preferences) {
        useAppStore.getState().setWorkbenchPreferences(pending!.preferences);
        setResult(tr("偏好配置已导入。"));
      } else {
        const session = getWebSocket()?.sessionId;
        if (!session) throw new Error(tr("连接后端后才能导入会话。"));
        const url = new URL("/api/conversations/import", apiBase()); url.searchParams.set("session_id", session); url.searchParams.set("workspace_root", workspace);
        const form = new FormData(); form.append("file", pending!.file);
        const response = await fetchWithTimeout(url, { method: "POST", headers: authHeaders(), body: form });
        if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
        const imported = await response.json() as { count: number; unavailable_artifacts: { conversation_id: string; artifact_id: string; reason: string }[] };
        sendClientCommand({ type: "conversation.list" }, { silent: true });
        setResult("已导入 " + imported.count + " 个新会话，可在会话列表继续。"
          + (imported.unavailable_artifacts.length ? tr("有 {v0} 个附件或文件引用无法读取原文件，请重新上传。", { v0: imported.unavailable_artifacts.length }) : ""));
      }
      setPending(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };
  return <section className="settings-group"><h3 className="settings-group-title">{tr("导入与迁移")}</h3><div className="settings-card">
    <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{tr("编辑器与输入偏好")}</div><div className="settings-row-description">{tr("字体、编辑习惯、模板、预测和语音配置。")}</div></div><button type="button" className="settings-action-button" onClick={() => void download()}><Download size={14} />{tr("导出偏好")}</button></div>
    <div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{tr("导入偏好或会话 JSON")}</div><div className="settings-row-description">{tr("会话导入为新的副本，保留历史和分支关系；导出 JSON 不包含外部附件原文件，缺失文件需重新上传。")}</div></div><button type="button" className="settings-action-button" onClick={() => fileInput.current?.click()}><Upload size={14} />{tr("选择文件")}</button><input ref={fileInput} hidden type="file" accept=".json,application/json" onChange={(event) => { const file = event.target.files?.[0]; if (file) void select(file); event.target.value = ""; }} /></div>
    {pending && <div className="settings-import-preview"><strong>{pending.file.name}</strong>{pending.preferences ? <pre>{JSON.stringify(pending.preferences, null, 2)}</pre> : <><p>{pending.titles!.length} {tr("个会话将归入：")}{workspace || tr("无项目")}</p><ul>{pending.titles!.map((title, index) => <li key={index}>{title}</li>)}</ul></>}
      <button type="button" className="settings-action-button" disabled={busy} onClick={() => void apply()}>{busy ? tr("导入中…") : tr("确认导入")}</button><button type="button" className="settings-action-button" disabled={busy} onClick={() => setPending(null)}>{tr("取消")}</button></div>}
    {error && <p role="alert">{error}</p>}{result && <p role="status">{result}</p>}
  </div></section>;
}
