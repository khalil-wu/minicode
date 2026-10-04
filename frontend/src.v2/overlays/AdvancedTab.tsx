import { useEffect, useRef, useState } from "react";
import { CheckCircle2, CircleSlash2, FolderGit2, RefreshCw, FileOutput, FolderOpen } from "lucide-react";
import { useAppStore } from "../stores";
import { pushToast } from "./ToastContainer";
import { envDetect, exportDiagnostics, isDesktop, type DesktopEnvInfo } from "../desktop/runtime";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { inputStyle, preStyle } from "./settingsShared";
import { openRightPanelFromSettings } from "../lib/settings-navigation";
import { showConfirm } from "./DialogService";

const TOOLS: { key: keyof Pick<DesktopEnvInfo, "git" | "python" | "node" | "docker" | "ollama">; label: string; detail: string }[] = [
  { key: "git", label: "Git", detail: "版本控制和 Worktree" },
  { key: "python", label: "Python", detail: "后端与 Python 工具" },
  { key: "node", label: "Node.js", detail: "前端、插件与本地服务" },
  { key: "docker", label: "Docker", detail: "容器化运行环境" },
  { key: "ollama", label: "Ollama", detail: "本地模型服务" },
];

export const AdvancedTab = ({ active = true }: { active?: boolean }) => {
  const envVars = useAppStore((s) => s.envVars);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const workspaceGit = useAppStore((s) => s.workspaceGit);
  const isConnected = useAppStore((s) => s.isConnected);
  const [environment, setEnvironment] = useState<DesktopEnvInfo | null>(null);
  const [environmentLoading, setEnvironmentLoading] = useState(false);
  const [environmentError, setEnvironmentError] = useState("");
  const [newEnvName, setNewEnvName] = useState("");
  const [newEnvValue, setNewEnvValue] = useState("");
  const [newEnvDescription, setNewEnvDescription] = useState("");
  const [savingEnvName, setSavingEnvName] = useState("");
  const [deletingEnvNames, setDeletingEnvNames] = useState<Record<string, boolean>>({});
  const [diagResult, setDiagResult] = useState<Record<string, unknown> | null>(null);
  const [diagLoading, setDiagLoading] = useState(false);
  const environmentRequestRef = useRef(0);
  const envDraftRef = useRef({ name: newEnvName, value: newEnvValue, description: newEnvDescription });
  envDraftRef.current = { name: newEnvName, value: newEnvValue, description: newEnvDescription };
  const [envListState, setEnvListState] = useState<"loading" | "ready" | "error">("loading");
  const [envListError, setEnvListError] = useState("");
  const [envRefresh, setEnvRefresh] = useState(0);
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    if (!isConnected) {
      setEnvListState("error");
      setEnvListError("后端连接尚未就绪，恢复连接后将重新读取。");
      return;
    }
    setEnvListState("loading");
    setEnvListError("");
    void sendClientCommandAwaitResult({ type: "env.list" }, "env.list").then((result) => {
      if (cancelled) return;
      if (!commandResultSucceeded(result)) throw new Error(result.message || "无法读取环境变量。");
      setEnvListState("ready");
    }).catch((error) => {
      if (cancelled) return;
      setEnvListState("error");
      setEnvListError(error instanceof Error ? error.message : "无法读取环境变量。");
    });
    return () => { cancelled = true; };
  }, [active, isConnected, envRefresh]);

  const refreshEnvironment = async (showFeedback = false) => {
    if (!isDesktop()) return;
    const request = ++environmentRequestRef.current;
    setEnvironmentLoading(true);
    setEnvironmentError("");
    try {
      const detected = await envDetect();
      if (request !== environmentRequestRef.current) return;
      setEnvironment(detected);
      if (showFeedback) pushToast("运行环境检测完成", "success");
    } catch (error) {
      if (request !== environmentRequestRef.current) return;
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      setEnvironment(null);
      setEnvironmentError(message);
      if (showFeedback) pushToast(`运行环境检测失败：${message}`, "error");
    } finally {
      if (request === environmentRequestRef.current) setEnvironmentLoading(false);
    }
  };

  const addEnvironmentVariable = async () => {
    const name = newEnvName.trim();
    const value = newEnvValue;
    const description = newEnvDescription.trim();
    if (!name || !value || savingEnvName || deletingEnvNames[name]) return;
    const submittedDraft = envDraftRef.current;
    setSavingEnvName(name);
    try {
      const result = await sendClientCommandAwaitResult(
        { type: "env.set", name, value, description },
        "env.set",
      );
      if (!commandResultSucceeded(result)) {
        pushToast(`添加环境变量失败：${result.message || "后端未返回具体原因"}`, "error");
        return;
      }
      if (envDraftRef.current.name === submittedDraft.name && envDraftRef.current.value === submittedDraft.value
        && envDraftRef.current.description === submittedDraft.description) {
        setNewEnvName("");
        setNewEnvValue("");
        setNewEnvDescription("");
      }
      pushToast(`已添加环境变量：${name}`, "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      pushToast(`添加环境变量失败：${message}`, "error");
    } finally {
      setSavingEnvName("");
    }
  };

  const deleteEnvironmentVariable = async (name: string) => {
    if (deletingEnvNames[name] || savingEnvName === name) return;
    setDeletingEnvNames((current) => ({ ...current, [name]: true }));
    try {
      const confirmed = await showConfirm({
        title: "删除环境变量",
        message: `确定删除“${name}”？依赖该变量的工具可能无法继续工作。`,
        confirmLabel: "删除",
        danger: true,
      });
      if (!confirmed) return;
      const result = await sendClientCommandAwaitResult(
        { type: "env.delete", name },
        "env.delete",
      );
      if (!commandResultSucceeded(result)) {
        pushToast(`删除环境变量失败：${result.message || "后端未返回具体原因"}`, "error");
        return;
      }
      pushToast(`已删除环境变量：${name}`, "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      pushToast(`删除环境变量失败：${message}`, "error");
    } finally {
      setDeletingEnvNames((current) => {
        const next = { ...current };
        delete next[name];
        return next;
      });
    }
  };

  useEffect(() => {
    void refreshEnvironment();
    return () => { environmentRequestRef.current += 1; };
  }, []);

  return (
    <>
      <section className="settings-group">
        <div className="settings-section-heading settings-section-heading-row">
          <div>
            <h3 className="settings-group-title">运行环境</h3>
            <p className="settings-section-description">MiniCode 实际检测到的本机开发工具；这些状态来自桌面运行时。</p>
          </div>
          {isDesktop() && (
            <button type="button" className="settings-icon-button" onClick={() => void refreshEnvironment(true)} disabled={environmentLoading} aria-label="重新检测运行环境" title="重新检测">
              <RefreshCw className={environmentLoading ? "spin" : undefined} />
            </button>
          )}
        </div>
        <div className="settings-runtime-grid">
          {TOOLS.map((tool) => {
            const available = environment?.[tool.key] === true;
            return (
              <div className="settings-runtime-item" key={tool.key} data-available={available ? "true" : "false"}>
                <span className="settings-runtime-icon" aria-hidden="true">{available ? <CheckCircle2 /> : <CircleSlash2 />}</span>
                <span><strong>{tool.label}</strong><small>{tool.detail}</small></span>
                <em>{environment == null ? "未检测" : available ? "可用" : "未找到"}</em>
              </div>
            );
          })}
        </div>
        {environmentError && <p className="settings-page-note" role="alert">检测失败：{environmentError}</p>}
      </section>

      <section className="settings-group">
        <h3 className="settings-group-title">工作区</h3>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-copy">
              <div className="settings-row-title">当前目录</div>
              <div className="settings-row-description settings-path-description" title={workingDirectory || undefined}>{workingDirectory || "尚未打开项目"}</div>
            </div>
            <div className="settings-row-control">
              <span className="settings-state-pill"><FolderOpen aria-hidden="true" />{workingDirectory ? "已打开" : "未打开"}</span>
            </div>
          </div>
          <div className="settings-row">
            <div className="settings-row-copy">
              <div className="settings-row-title">Git 工作树</div>
              <div className="settings-row-description">{workspaceGit?.isWorktree ? "当前任务运行在独立 Worktree 中。" : "当前任务直接使用项目工作区。"}</div>
            </div>
            <div className="settings-row-control">
              <span className="settings-state-pill" data-tone={workspaceGit?.error ? "danger" : "neutral"}><FolderGit2 aria-hidden="true" />{workspaceGit?.branch || "未检测分支"}</span>
            </div>
          </div>
        </div>
      </section>

      <section className="settings-group">
        <div className="settings-section-heading">
          <h3 className="settings-group-title">环境变量</h3>
          <p className="settings-section-description">仅在工具执行时注入；敏感值不会在此处回显。</p>
        </div>
        <div className="settings-card settings-env-card">
          {envListState === "loading" && <p role="status">正在读取环境变量…</p>}
          {envListError && <div role="alert" className="settings-page-note">
            读取环境变量失败：{envListError}
            <button type="button" className="settings-action-button" onClick={() => setEnvRefresh((value) => value + 1)}>重试</button>
          </div>}
          {envVars.map((variable) => (
            <div key={variable.name} className="settings-env-row">
              <code title={variable.name}>{variable.name}</code>
              <span>{variable.description || "没有说明"}</span>
              <em>{variable.scope}</em>
              <button
                type="button"
                disabled={Boolean(deletingEnvNames[variable.name]) || savingEnvName === variable.name}
                onClick={() => void deleteEnvironmentVariable(variable.name)}
                aria-label={`删除环境变量 ${variable.name}`}
                title={`删除环境变量 ${variable.name}`}
              >
                {deletingEnvNames[variable.name] ? "正在删除…" : "删除"}
              </button>
            </div>
          ))}
          {envVars.length === 0 && envListState === "ready" && <div className="settings-empty-inline">尚未配置环境变量。</div>}
          <div className="settings-env-editor">
            <input placeholder="变量名" value={newEnvName} onChange={(event) => setNewEnvName(event.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, ""))} style={inputStyle} />
            <input placeholder="变量值" type="password" value={newEnvValue} onChange={(event) => setNewEnvValue(event.target.value)} style={inputStyle} />
            <input placeholder="说明（可选）" value={newEnvDescription} onChange={(event) => setNewEnvDescription(event.target.value)} style={inputStyle} />
            <button type="button" className="settings-action-button" disabled={!newEnvName.trim() || !newEnvValue || Boolean(savingEnvName) || Boolean(deletingEnvNames[newEnvName])} onClick={() => void addEnvironmentVariable()}>{savingEnvName ? "正在添加…" : "添加"}</button>
          </div>
        </div>
      </section>

      <section className="settings-group">
        <h3 className="settings-group-title">运行状态</h3>
        <div className="settings-card">
          <div className="settings-row">
            <div className="settings-row-copy">
              <div className="settings-row-title">后端、模型与 MCP</div>
              <div className="settings-row-description">查看连接状态、当前模型和服务错误。</div>
            </div>
            <div className="settings-row-control">
              <button type="button" className="settings-action-button" onClick={() => openRightPanelFromSettings("diagnostics")}>打开运行状态</button>
            </div>
          </div>
        </div>
      </section>

      {isDesktop() && (
        <section className="settings-group">
          <div className="settings-section-heading">
            <h3 className="settings-group-title">诊断</h3>
            <p className="settings-section-description">导出桌面日志和运行环境摘要，用于排查启动、后端或工具问题。</p>
          </div>
          <div className="settings-card">
            <div className="settings-row">
              <div className="settings-row-copy">
                <div className="settings-row-title">导出诊断信息</div>
                <div className="settings-row-description">生成可以本地检查的诊断包，不会自动发送给第三方。</div>
              </div>
              <div className="settings-row-control">
                <button type="button" className="settings-action-button" onClick={async () => {
                  setDiagLoading(true);
                  try {
                    const result = await exportDiagnostics();
                    if (!result) throw new Error("Desktop diagnostics are unavailable");
                    setDiagResult(result as Record<string, unknown>);
                    pushToast("诊断信息已导出", "success");
                  } catch (error) {
                    const message = error instanceof Error ? error.message : String(error || "未知错误");
                    pushToast(`导出失败：${message}`, "error");
                  } finally {
                    setDiagLoading(false);
                  }
                }} disabled={diagLoading}><FileOutput aria-hidden="true" />{diagLoading ? "正在导出…" : "导出"}</button>
              </div>
            </div>
          </div>
          {diagResult && <pre style={preStyle}>{JSON.stringify(diagResult, null, 2)}</pre>}
        </section>
      )}
    </>
  );
};
