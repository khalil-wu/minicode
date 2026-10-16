import { useI18n, type TranslationKey } from "../i18n";
import { appendWorkspaceOwner } from "../protocol/workspace-owner";
import { useEffect, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { showConfirm } from "./DialogService";

interface Environment { backend_python: string; python_version: string; project_python: string | null; node: string | null; commands: Record<string, string[]>; display_commands: Record<string, string> }
const labels: Record<string, TranslationKey> = { python_venv: "创建 .venv", python_dependencies: "安装 Python 依赖", node_dependencies: "安装 Node.js 依赖" };
export function ProjectEnvironmentSettings({ workspaceRoot }: { workspaceRoot: string }) {
  const { t: tr } = useI18n();
  const [environment, setEnvironment] = useState<Environment | null>(null);
  const [revision, setRevision] = useState(0);
  const [output, setOutput] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const operation = useRef<AbortController | null>(null);
  useEffect(() => () => operation.current?.abort(), []);
  const endpoint = (path = "") => { const url = new URL("/api/workspace/environment" + path, apiBase()); url.searchParams.set("workspace_root", workspaceRoot); appendWorkspaceOwner(url); return url; };
  useEffect(() => {
    if (!workspaceRoot) return;
    const controller = new AbortController();
    void fetchWithTimeout(endpoint(), { headers: authHeaders(), signal: controller.signal }).then(async (response) => {
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      return response.json() as Promise<Environment>;
    }).then(setEnvironment).catch((reason) => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [workspaceRoot, revision]);
  const run = async (action: string) => {
    if (!await showConfirm({ title: tr(labels[action]), message: tr("在") + " " + workspaceRoot + " " + tr("运行：") + "\n" + environment!.display_commands[action] + "\n" + tr("依赖安装会执行项目配置的安装脚本。"), confirmLabel: tr("运行") })) return;
    if (useAppStore.getState().workingDirectory !== workspaceRoot) return;
    const controller = operation.current = new AbortController();
    setBusy(action); setError(""); setOutput("");
    try {
      const response = await fetch(endpoint("/setup"), { method: "POST", headers: authHeaders({ "content-type": "application/json" }), signal: controller.signal, body: JSON.stringify({ action }) });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let pending = "";
      let completed = false;
      while (true) {
        const { done, value } = await reader.read();
        pending += decoder.decode(value, { stream: !done });
        const lines = pending.split("\n"); pending = lines.pop()!;
        for (const line of lines.filter(Boolean)) {
          const event = JSON.parse(line) as { output?: string; exit_code?: number };
          if (event.output) setOutput((previous) => previous + event.output);
          if (event.exit_code != null) { completed = true; if (event.exit_code !== 0) throw new Error("命令退出码 " + event.exit_code); }
        }
        if (done) break;
      }
      if (!completed) throw new Error(tr("连接中断，命令没有返回完成状态。"));
      setOutput((previous) => previous + "\n已完成。"); setRevision((value) => value + 1);
    } catch (reason) { setError(controller.signal.aborted ? tr("已取消初始化。") : reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(""); }
  };
  return <section className="settings-group"><h3 className="settings-group-title">{tr("项目环境初始化")}</h3>
    {!workspaceRoot ? <p className="settings-section-description">{tr("打开项目后配置开发环境。")}</p> : <div className="settings-card">
      <div className="settings-row"><span>{tr("后端解释器")}</span><code title={environment?.backend_python}>{environment?.backend_python}</code></div>
      <div className="settings-row"><span>{tr("项目解释器")}</span><code>{environment?.project_python || tr("尚未创建 .venv")}</code></div>
      <div className="settings-row"><span>{tr("Node.js 路径")}</span><code title={environment?.node ?? undefined}>{environment?.node || tr("未检测到")}</code></div>
      {Object.keys(environment?.commands ?? {}).filter((action) => action !== "python_venv" || !environment?.project_python).map((action) => <div className="settings-row" key={action}><div className="settings-row-copy"><div className="settings-row-title">{tr(labels[action])}</div><code className="settings-row-description">{environment!.display_commands[action]}</code></div><button className="settings-action-button" type="button" disabled={Boolean(busy)} onClick={() => void run(action)}>{tr("运行")}</button></div>)}
      {busy && <button className="settings-action-button" type="button" onClick={() => operation.current?.abort()}>{tr("取消") + " "}{tr(labels[busy])}</button>}
      {output && <pre className="settings-environment-output">{output}</pre>}{error && <p role="alert">{error}</p>}
    </div>}
  </section>;
}
