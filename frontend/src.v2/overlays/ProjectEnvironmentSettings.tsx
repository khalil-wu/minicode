import { useI18n, type TranslationKey } from "../i18n";
import { appendWorkspaceOwner, workspaceFileOwner, workspaceRequestOwner, type WorkspaceRequestOwner } from "../protocol/workspace-owner";
import { useEffect, useRef, useState } from "react";
import { useAppStore } from "../stores";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { showConfirm } from "./DialogService";

interface Environment extends Partial<WorkspaceRequestOwner> { registration_id?: string; backend_python: string | null; python_version: string; project_python: string | null; node: string | null; commands: Record<string, string[]>; display_commands: Record<string, string> }
type LoadedEnvironment = { value: Environment; owner: WorkspaceRequestOwner | undefined; conversationId: string | null; root: string };
const labels: Record<string, TranslationKey> = { python_venv: "创建 .venv", python_dependencies: "安装 Python 依赖", node_dependencies: "安装 Node.js 依赖" };
export function ProjectEnvironmentSettings({ workspaceRoot }: { workspaceRoot: string }) {
  const { t: tr } = useI18n();
  const [environment, setEnvironment] = useState<Environment | null>(null);
  const [revision, setRevision] = useState(0);
  const [output, setOutput] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const conversationId = useAppStore((state) => state.conversationId);
  const operation = useRef<AbortController | null>(null);
  const loaded = useRef<LoadedEnvironment | null>(null);
  const owns = (snapshot: LoadedEnvironment) => Boolean(loaded.current
    && loaded.current.value.registration_id === snapshot.value.registration_id
    && loaded.current.value.environment_id === snapshot.value.environment_id
    && useAppStore.getState().conversationId === snapshot.conversationId
    && useAppStore.getState().workingDirectory === snapshot.root);
  useEffect(() => {
    operation.current?.abort(); loaded.current = null;
    setEnvironment(null); setBusy(""); setOutput(""); setError("");
    return () => operation.current?.abort();
  }, [workspaceRoot, conversationId]);
  const endpoint = (path = "", owner?: WorkspaceRequestOwner) => { const url = new URL("/api/workspace/environment" + path, apiBase()); url.searchParams.set("workspace_root", workspaceRoot); appendWorkspaceOwner(url, owner); return url; };
  useEffect(() => {
    if (!workspaceRoot) return;
    const controller = new AbortController();
    const owner = workspaceRequestOwner();
    void fetchWithTimeout(endpoint("", owner), { headers: authHeaders(), signal: controller.signal }).then(async (response) => {
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      return response.json() as Promise<Environment>;
    }).then((value) => {
      if (!controller.signal.aborted && useAppStore.getState().conversationId === conversationId && useAppStore.getState().workingDirectory === workspaceRoot) {
        loaded.current = { value, owner, conversationId, root: workspaceRoot }; setEnvironment(value);
      }
    }).catch((reason) => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [workspaceRoot, conversationId, revision]);
  const run = async (action: string) => {
    const snapshot = loaded.current;
    if (!snapshot || !await showConfirm({ title: tr(labels[action]), message: tr("在") + " " + workspaceRoot + " " + tr("运行：") + "\n" + snapshot.value.display_commands[action] + "\n" + tr("依赖安装会执行项目配置的安装脚本。"), confirmLabel: tr("运行") })) return;
    if (!owns(snapshot)) return;
    const controller = operation.current = new AbortController();
    setBusy(action); setError(""); setOutput("");
    try {
      const owner = snapshot.value.registration_id ? workspaceFileOwner(snapshot.value) : snapshot.owner;
      const response = await fetch(endpoint("/setup", owner), { method: "POST", headers: authHeaders({ "content-type": "application/json" }), signal: controller.signal, body: JSON.stringify({ action, expected_registration_id: snapshot.value.registration_id ?? "" }) });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let pending = "";
      let completed = false;
      while (true) {
        const { done, value } = await reader.read();
        if (!owns(snapshot) || controller.signal.aborted) return;
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
    } catch (reason) { if (owns(snapshot)) setError(controller.signal.aborted ? tr("已取消初始化。") : reason instanceof Error ? reason.message : String(reason)); }
    finally { if (owns(snapshot) && operation.current === controller) setBusy(""); }
  };
  return <section className="settings-group"><h3 className="settings-group-title">{tr("项目环境初始化")}</h3>
    {!workspaceRoot ? <p className="settings-section-description">{tr("打开项目后配置开发环境。")}</p> : <div className="settings-card">
      <div className="settings-row"><span>{tr("后端解释器")}</span><code title={environment?.backend_python ?? undefined}>{environment?.backend_python || tr("未检测到")}</code></div>
      <div className="settings-row"><span>{tr("项目解释器")}</span><code>{environment?.project_python || tr("尚未创建 .venv")}</code></div>
      <div className="settings-row"><span>{tr("Node.js 路径")}</span><code title={environment?.node ?? undefined}>{environment?.node || tr("未检测到")}</code></div>
      {Object.keys(environment?.commands ?? {}).filter((action) => action !== "python_venv" || !environment?.project_python).map((action) => <div className="settings-row" key={action}><div className="settings-row-copy"><div className="settings-row-title">{tr(labels[action])}</div><code className="settings-row-description">{environment!.display_commands[action]}</code></div><button className="settings-action-button" type="button" disabled={Boolean(busy)} onClick={() => void run(action)}>{tr("运行")}</button></div>)}
      {busy && <button className="settings-action-button" type="button" onClick={() => operation.current?.abort()}>{tr("取消") + " "}{tr(labels[busy])}</button>}
      {output && <pre className="settings-environment-output">{output}</pre>}{error && <p role="alert">{error}</p>}
    </div>}
  </section>;
}
