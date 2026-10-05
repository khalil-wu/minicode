import { useCallback, useEffect, useRef, useState } from "react";
import { desktop, type DesktopSandboxSetupResult } from "../desktop/runtime";
import { LoaderCircle, RefreshCw, ShieldCheck, ShieldQuestion } from "../lib/icons";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { sendClientCommand } from "../protocol/ws-outbox";

interface SandboxStatus {
  available: boolean;
  backend: string;
  reason: string;
  setup_required: boolean;
  setup_supported: boolean;
  native_available: boolean;
  permission_mode: "confirm";
  state_root: string;
  sandbox_home: string;
  description: string;
}

export function SandboxSettings({ active = true }: { active?: boolean }) {
  const native = desktop();
  if (!native?.platformInfo.isDesktop || native.platformInfo.platform !== "win32") return null;
  return <WindowsSandboxSettings active={active} setup={native.sandbox.setup} />;
}

function WindowsSandboxSettings({ active, setup }: {
  active: boolean;
  setup: () => Promise<DesktopSandboxSetupResult>;
}) {
  const [status, setStatus] = useState<SandboxStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [settingUp, setSettingUp] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [setupError, setSetupError] = useState("");
  const [message, setMessage] = useState("");
  const request = useRef<AbortController | null>(null);
  const setupPending = useRef(false);
  const mounted = useRef(false);

  const refresh = useCallback(async () => {
    request.current?.abort();
    const controller = request.current = new AbortController();
    setLoading(true);
    setLoadError("");
    try {
      const response = await fetchWithTimeout(new URL("/api/sandbox/status", apiBase()), {
        headers: authHeaders(), cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      const next = await response.json() as SandboxStatus;
      if (controller.signal.aborted) return null;
      setStatus(next);
      return next;
    } catch (error) {
      if (!controller.signal.aborted) setLoadError(error instanceof Error ? error.message : String(error));
      return null;
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; request.current?.abort(); };
  }, []);
  useEffect(() => {
    if (active && !setupPending.current) void refresh();
  }, [active, refresh]);

  const initialize = async () => {
    if (setupPending.current) return;
    setupPending.current = true;
    setSettingUp(true);
    setSetupError("");
    setMessage("请在 Windows 系统授权窗口中确认初始化。");
    try {
      const result = await setup();
      if (!mounted.current) return;
      if (result.cancelled) setMessage("已取消初始化，可以稍后重试。");
      else if (!result.ok) {
        setMessage("");
        setSetupError(result.error || "Windows 沙箱初始化失败，请重试。");
      } else setMessage("初始化步骤已完成，正在确认工具沙箱状态…");
      const confirmed = await refresh();
      if (result.ok && mounted.current) {
        if (confirmed?.native_available && confirmed.available) {
          setMessage("Windows 系统初始化完成，默认权限的工具已可使用。");
          sendClientCommand({ type: "runtime.capabilities.inspect", source: "sandbox.setup" });
        } else setMessage(confirmed?.native_available
          ? "Windows 初始化已完成，但默认权限的工具尚不可用。请查看检测详情。"
          : "初始化步骤已结束，Windows 系统隔离尚未确认可用。请重新检测或查看检测详情。");
      }
    } catch (error) {
      if (mounted.current) {
        setMessage("");
        setSetupError(error instanceof Error ? error.message : String(error));
        await refresh();
      }
    } finally {
      setupPending.current = false;
      if (mounted.current) setSettingUp(false);
    }
  };

  const ready = Boolean(status?.available && !loadError);
  const stateLabel = settingUp ? "正在初始化" : loading ? "正在检测" : loadError ? "状态待确认"
    : ready ? "已就绪" : status?.setup_required ? "需要初始化" : status ? "暂不可用" : "尚未检测";

  return <section className="settings-group" aria-label="Windows 工具沙箱">
    <h3 className="settings-group-title">Windows 工具沙箱</h3>
    <div className="settings-card">
      <div className="settings-row">
        <div className="settings-row-copy">
          <div className="settings-row-title">系统隔离</div>
          <p className="settings-row-description">{status?.description || "默认权限下，Shell 与 Git 工具需要可用的隔离环境。首次使用时需完成一次系统初始化。"}</p>
        </div>
        <div className="settings-row-control">
          <span className="settings-state-pill" role="status">
            {loading || settingUp ? <LoaderCircle className="settings-spin" aria-hidden="true" />
              : ready ? <ShieldCheck aria-hidden="true" /> : <ShieldQuestion aria-hidden="true" />}{stateLabel}
          </span>
          {status?.setup_required && <button type="button" className="settings-action-button"
            disabled={settingUp || loading || !status.setup_supported}
            title={!status.setup_supported ? "当前安装的初始化组件不可用，请查看检测详情。" : "需要在 Windows 系统授权窗口中确认"}
            onClick={() => void initialize()}>{settingUp ? "正在初始化…" : "初始化"}</button>}
          <button type="button" className="settings-icon-button" aria-label="重新检测 Windows 工具沙箱"
            title="重新检测" disabled={settingUp || loading} onClick={() => { setMessage(""); setSetupError(""); void refresh(); }}>
            <RefreshCw aria-hidden="true" />
          </button>
        </div>
      </div>
    </div>
    {message && <p className="settings-page-note" role="status">{message}</p>}
    {setupError && <p className="settings-page-note" role="alert">{setupError}</p>}
    {loadError && <p className="settings-page-note" role="alert">检测失败：{loadError}</p>}
    {status && <details className="settings-detail-block">
      <summary>检测详情</summary>
      <p className="settings-page-note">{status.reason || "默认权限的工具隔离检测通过。"}</p>
      <div className="settings-row-description">执行方式：{status.backend || "尚未可用"} · 默认权限：确认</div>
      <div className="settings-row-description">状态目录：<code>{status.state_root}</code></div>
      <div className="settings-row-description">沙箱目录：<code>{status.sandbox_home}</code></div>
    </details>}
  </section>;
}
