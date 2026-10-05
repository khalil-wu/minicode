import { useEffect, useRef, useState } from "react";
import { Check, Copy, ExternalLink, Github, LoaderCircle, RefreshCw } from "lucide-react";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { isDesktop, openExternal } from "../desktop/runtime";
import { copyText } from "../lib/clipboard";
import "./GithubConnection.css";

interface GithubStatus {
  available: boolean;
  authenticated: boolean;
  login: string | null;
  host: string;
  message: string;
}
interface LoginEvent {
  phase: "starting" | "authorizing" | "connected" | "error";
  user_code?: string;
  verification_uri?: string;
  message?: string;
  connection?: GithubStatus;
}

export function GithubConnection({ active = true }: { active?: boolean }) {
  const [connection, setConnection] = useState<GithubStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [authorization, setAuthorization] = useState<LoginEvent | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [revision, setRevision] = useState(0);
  const operation = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!active || operation.current) return;
    const controller = new AbortController();
    setLoading(true); setError("");
    void fetchWithTimeout(new URL("/api/github/status", apiBase()), {
      headers: authHeaders(), signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      return response.json() as Promise<GithubStatus>;
    }).then((status) => { if (!controller.signal.aborted) setConnection(status); })
      .catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [active, revision]);
  useEffect(() => () => operation.current?.abort(), []);

  const connect = async () => {
    const controller = operation.current = new AbortController();
    setConnecting(true); setAuthorization(null); setCopied(false); setError(""); setMessage("正在准备 GitHub 授权…");
    try {
      const response = await fetch(new URL("/api/github/login", apiBase()), {
        method: "POST", headers: authHeaders(), signal: controller.signal,
      });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let pending = "";
      let completed = false;
      const receive = (line: string) => {
        const event = JSON.parse(line) as LoginEvent;
        setMessage(event.message ?? "");
        if (event.phase === "authorizing") setAuthorization((previous) => ({ ...previous, ...event }));
        if (event.phase === "connected") {
          completed = true;
          setConnection(event.connection!);
          setAuthorization(null);
        }
        if (event.phase === "error") {
          completed = true;
          setError(event.message || "GitHub 连接失败。");
          setAuthorization(null);
        }
      };
      try {
        while (true) {
          const { done, value } = await reader.read();
          pending += decoder.decode(value, { stream: !done });
          const lines = pending.split("\n"); pending = lines.pop()!;
          for (const line of lines.filter(Boolean)) receive(line);
          if (done) { if (pending.trim()) receive(pending); break; }
        }
      } finally { reader.releaseLock(); }
      if (!completed) throw new Error("授权连接中断，尚未收到 GitHub 的完成状态，请重新连接。");
    } catch (reason) {
      if (!controller.signal.aborted) { controller.abort(); setError(reason instanceof Error ? reason.message : String(reason)); setAuthorization(null); }
    } finally {
      if (operation.current === controller) { operation.current = null; setConnecting(false); }
    }
  };
  const cancel = () => {
    operation.current?.abort(); operation.current = null;
    setConnecting(false); setAuthorization(null); setError(""); setMessage("已取消连接。");
  };
  const openAuthorization = async () => {
    try {
      const target = authorization!.verification_uri!;
      if (isDesktop()) {
        if (!await openExternal(target)) throw new Error("未能打开授权页面，请重试。");
      } else {
        window.open(target, "_blank", "noopener,noreferrer");
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const knownAccount = connection?.available && connection.authenticated ? connection.login : null;
  const connected = Boolean(connection?.available && connection.authenticated && !error);
  const stateLabel = connecting ? "等待授权" : loading ? "读取连接状态…" : connected ? "已连接"
    : error ? "状态待确认" : connection?.available ? "尚未连接" : connection ? "连接组件未就绪" : "尚未读取";

  return <section className="settings-group github-connection" aria-label="GitHub 连接">
    <h3 className="settings-group-title">GitHub</h3>
    <div className="settings-card">
      <div className="settings-row">
        <div className="settings-row-copy"><div className="settings-row-title github-connection-title"><Github size={18} aria-hidden="true" />{knownAccount || "连接 GitHub"}</div>
          <div className="settings-row-description">{knownAccount ? connection!.host + (error ? " · 上次连接账号，当前状态待确认。" : " · 读取 PR 与检查状态，提交和推送由你发起。") : "连接账号后，在工作区查看 PR、检查状态并创建草稿 PR。"}</div></div>
        <div className="settings-row-control github-connection-controls">
          <span className="github-connection-status" data-connected={connected} role="status">{connecting || loading ? <LoaderCircle size={13} className="settings-spin" /> : connected ? <Check size={13} /> : null}{stateLabel}</span>
          {!connected && !connecting && <button type="button" className="settings-action-button" disabled={loading || !connection?.available} onClick={() => void connect()}>连接 GitHub</button>}
          {!connecting && <button type="button" className="settings-icon-button" aria-label="刷新 GitHub 连接状态" disabled={loading} onClick={() => { setMessage(""); setRevision((value) => value + 1); }}><RefreshCw size={14} /></button>}
        </div>
      </div>
      {connecting && <div className="github-authorization">
        <p>{message || "正在等待 GitHub 授权…"}</p>
        {authorization?.user_code && <>
          <div className="github-authorization-code"><code aria-label="GitHub 授权码">{authorization.user_code}</code><button type="button" className="settings-icon-button" aria-label={copied ? "授权码已复制" : "复制授权码"} onClick={() => {
            void copyText(authorization.user_code!, "授权码").then((success) => setCopied(success));
          }}>{copied ? <Check size={15} /> : <Copy size={15} />}</button></div>
          <p className="github-authorization-help">在 GitHub 授权页面输入验证码并确认。完成后，这里会显示连接账号。</p>
        </>}
        <div className="github-authorization-actions">{authorization?.verification_uri && <button type="button" className="settings-action-button" onClick={() => void openAuthorization()}><ExternalLink size={14} />打开授权页面</button>}
          <button type="button" className="settings-action-button" onClick={cancel}>取消连接</button></div>
      </div>}
      {!connecting && !connected && connection && !error && <p className="github-connection-note">{connection.message}</p>}
      {!connecting && message && !error && <p className="github-connection-note" role="status">{message}</p>}
      {error && <p className="github-connection-error" role="alert">{error}</p>}
    </div>
  </section>;
}
