import { useEffect, useRef, useState, type ReactNode } from "react";
import type { AppMode, FollowUpBehavior, RemoteImagePolicy, SendShortcut, ViewMode } from "../stores/types";
import { desktop, isDesktop, type UpdatePreflightResult, type UpdateStatus } from "../desktop/runtime";
import { useAppStore } from "../stores";
import { pushToast } from "./ToastContainer";

const REMOTE_IMAGE_CHOICES = [
  { id: "ask", label: "询问", title: "加载远程图片前询问" },
  { id: "allow", label: "允许", title: "自动加载 Markdown 中的远程图片" },
  { id: "block", label: "阻止", title: "不加载任何远程图片" },
] as const;

const APP_MODE_CHOICES: { id: AppMode; label: string; title: string }[] = [
  { id: "cowork", label: "协作", title: "使用会话、任务和工具侧栏" },
  { id: "code", label: "代码", title: "使用文件树与编辑器工作区" },
];

const VIEW_MODE_CHOICES: { id: ViewMode; label: string; title: string }[] = [
  { id: "summary", label: "精简", title: "减少过程信息" },
  { id: "normal", label: "标准", title: "显示主要步骤" },
  { id: "verbose", label: "详细", title: "显示完整活动信息" },
];

const SEND_SHORTCUT_CHOICES: { id: SendShortcut; label: string; title: string }[] = [
  { id: "enter", label: "Enter", title: "按 Enter 发送，Shift + Enter 换行" },
  { id: "mod-enter", label: "Ctrl/Cmd + Enter", title: "按 Ctrl/Cmd + Enter 发送，Enter 换行" },
];

const FOLLOW_UP_CHOICES: { id: FollowUpBehavior; label: string; title: string }[] = [
  { id: "queue", label: "排队", title: "当前任务结束后再处理新消息" },
  { id: "steer", label: "引导", title: "把新消息立即加入当前任务" },
];

export const GeneralTab = ({
  remoteImagePolicy,
  setRemoteImagePolicy,
}: {
  remoteImagePolicy: RemoteImagePolicy;
  setRemoteImagePolicy: (policy: RemoteImagePolicy) => void;
}) => {
  const appMode = useAppStore((s) => s.appMode);
  const setAppMode = useAppStore((s) => s.setAppMode);
  const viewMode = useAppStore((s) => s.viewMode);
  const setViewMode = useAppStore((s) => s.setViewMode);
  const sendShortcut = useAppStore((s) => s.sendShortcut);
  const setSendShortcut = useAppStore((s) => s.setSendShortcut);
  const followUpBehavior = useAppStore((s) => s.followUpBehavior);
  const setFollowUpBehavior = useAppStore((s) => s.setFollowUpBehavior);
  const allowedRemoteImageDomains = useAppStore((s) => s.allowedRemoteImageDomains);
  const clearAllowedRemoteImageDomains = useAppStore((s) => s.clearAllowedRemoteImageDomains);
  return (
    <>
      <SettingsGroup title="工作区">
        <SettingsRow title="工作模式" description="切换协作区或代码区。">
          <SegmentedControl>
            {APP_MODE_CHOICES.map((choice) => (
              <button
                key={choice.id}
                type="button"
                className="settings-segment"
                data-active={appMode === choice.id ? "true" : "false"}
                aria-pressed={appMode === choice.id}
                onClick={() => setAppMode(choice.id)}
                title={choice.title}
              >
                {choice.label}
              </button>
            ))}
          </SegmentedControl>
        </SettingsRow>
        <SettingsRow title="过程详情" description="控制活动与工具详情。">
          <SegmentedControl>
            {VIEW_MODE_CHOICES.map((choice) => (
              <button
                key={choice.id}
                type="button"
                className="settings-segment"
                data-active={viewMode === choice.id ? "true" : "false"}
                aria-pressed={viewMode === choice.id}
                onClick={() => setViewMode(choice.id)}
                title={choice.title}
              >
                {choice.label}
              </button>
            ))}
          </SegmentedControl>
        </SettingsRow>
        <SettingsRow title="发送快捷键" description="设置输入框的发送按键。">
          <SegmentedControl>
            {SEND_SHORTCUT_CHOICES.map((choice) => (
              <button
                key={choice.id}
                type="button"
                className="settings-segment"
                data-active={sendShortcut === choice.id ? "true" : "false"}
                aria-pressed={sendShortcut === choice.id}
                onClick={() => setSendShortcut(choice.id)}
                title={choice.title}
              >
                {choice.label}
              </button>
            ))}
          </SegmentedControl>
        </SettingsRow>
        <SettingsRow title="跟进行为" description="任务运行时发送新消息的方式。">
          <SegmentedControl>
            {FOLLOW_UP_CHOICES.map((choice) => (
              <button
                key={choice.id}
                type="button"
                className="settings-segment"
                data-active={followUpBehavior === choice.id ? "true" : "false"}
                aria-pressed={followUpBehavior === choice.id}
                onClick={() => setFollowUpBehavior(choice.id)}
                title={choice.title}
              >
                {choice.label}
              </button>
            ))}
          </SegmentedControl>
        </SettingsRow>
      </SettingsGroup>

      <SettingsGroup title="内容">
        <SettingsRow title="远程 Markdown 图片" description="控制外部图片加载。">
          <SegmentedControl>
            {REMOTE_IMAGE_CHOICES.map((choice) => (
              <button
                key={choice.id}
                type="button"
                className="settings-segment"
                data-active={remoteImagePolicy === choice.id ? "true" : "false"}
                aria-pressed={remoteImagePolicy === choice.id}
                onClick={() => setRemoteImagePolicy(choice.id)}
                title={choice.title}
              >
                {choice.label}
              </button>
            ))}
          </SegmentedControl>
        </SettingsRow>
        {allowedRemoteImageDomains.length > 0 && (
          <SettingsRow title="已允许的网站" description={`${allowedRemoteImageDomains.length} 个网站已允许。`}>
            <button type="button" className="settings-action-button" onClick={clearAllowedRemoteImageDomains}>
              清除列表
            </button>
          </SettingsRow>
        )}
      </SettingsGroup>

      {isDesktop() && <DesktopUpdates />}
    </>
  );
};

const SettingsGroup = ({ title, children }: { title: string; children: ReactNode }) => (
  <section className="settings-group">
    <h3 className="settings-group-title">{title}</h3>
    <div className="settings-card">{children}</div>
  </section>
);

const SettingsRow = ({ title, description, children }: { title: string; description: string; children: ReactNode }) => (
  <div className="settings-row">
    <div className="settings-row-copy">
      <div className="settings-row-title">{title}</div>
      <div className="settings-row-description">{description}</div>
    </div>
    <div className="settings-row-control">{children}</div>
  </div>
);

const SegmentedControl = ({ children }: { children: ReactNode }) => (
  <div className="settings-segmented">{children}</div>
);

const DesktopUpdates = () => {
  const [status, setStatus] = useState("idle");
  const [version, setVersion] = useState("");
  const [previousVersion, setPreviousVersion] = useState("");
  const [failedVersion, setFailedVersion] = useState("");
  const [message, setMessage] = useState("");
  const [percent, setPercent] = useState<number | null>(null);
  const [pendingAction, setPendingAction] = useState<"" | "check" | "download" | "install">("");
  const [installBlockMessage, setInstallBlockMessage] = useState("");
  const statusSequenceRef = useRef(-1);
  useEffect(() => {
    const applyStatus = (payload: UpdateStatus) => {
      const sequence = Number(payload.sequence ?? 0);
      if (sequence < statusSequenceRef.current) return;
      statusSequenceRef.current = sequence;
      setStatus(payload.status);
      setVersion(payload.version || "");
      setPreviousVersion(payload.previousVersion || "");
      setFailedVersion(payload.failedVersion || "");
      setMessage(payload.message || "");
      setPercent(typeof payload.percent === "number" ? payload.percent : null);
      if (payload.status !== "ready") setInstallBlockMessage("");
    };
    const updates = desktop()?.updates;
    const unsubscribe = updates?.onStatus(applyStatus);
    void updates?.getStatus().then(applyStatus).catch((error) => {
      const detail = error instanceof Error ? error.message : String(error || "未知错误");
      setStatus("error");
      setMessage(detail);
    });
    return unsubscribe;
  }, []);

  const runUpdateAction = async (action: "check" | "download" | "install") => {
    if (pendingAction) return;
    const updates = desktop()?.updates;
    if (!updates) {
      pushToast("桌面更新服务不可用", "error");
      return;
    }
    setPendingAction(action);
    try {
      if (action === "install") {
        const installPreflight = await updates.preflight();
        if (!installPreflight.allowed || !installPreflight.fingerprint) {
          const detail = describeUpdateBlockers(installPreflight) || "当前状态无法安全安装更新。";
          setInstallBlockMessage(detail);
          pushToast(detail, "warning");
          return;
        }
        setInstallBlockMessage("");
        const result = await updates.install({ fingerprint: installPreflight.fingerprint });
        if (!result.installed) {
          const detail = result.message
            || describeUpdateBlockers(result.preflight)
            || (result.reason === "preflight_changed" || result.reason === "preflight_stale"
              ? "应用状态在安装前发生变化，请确认当前任务后重新安装。"
              : "当前环境不支持此更新操作。");
          setInstallBlockMessage(detail);
          pushToast(detail, result.reason === "install_failed" ? "error" : "warning");
          return;
        }
      } else {
        const accepted = await updates[action]();
        if (!accepted) throw new Error("当前环境不支持此更新操作");
      }
      pushToast(
        action === "check" ? "已开始检查更新" : action === "download" ? "已开始下载更新" : "正在重启并安装更新",
        "success",
      );
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error || "未知错误");
      pushToast(`${action === "check" ? "检查" : action === "download" ? "下载" : "安装"}更新失败：${detail}`, "error");
    } finally {
      setPendingAction("");
    }
  };
  const label = status === "ready" && installBlockMessage ? `暂不能安装：${installBlockMessage}`
    : status === "checking" ? "正在检查更新…"
    : status === "available" ? `版本 ${version || "更新"} 已可下载。`
      : status === "downloading" ? `正在下载${percent == null ? "…" : ` ${Math.round(percent)}%`}`
        : status === "ready" ? `版本 ${version || "更新"} 已可安装。${message ? ` 上次操作未完成：${message}` : ""}`
          : status === "current" ? "MiniCode 已是最新版本。"
            : status === "rollback_launching" ? `正在恢复至 ${version || previousVersion || "上一版本"}…`
              : status === "rolled_back" ? `已自动恢复至 ${version || "上一版本"}${failedVersion ? `；版本 ${failedVersion} 未通过启动检查。` : "。"}`
                : status === "recovery_required" ? `当前版本未通过启动检查${previousVersion ? `，且无法自动启动 ${previousVersion}` : ""}。${message ? ` ${message}` : ""}`
            : status === "error" ? `更新服务失败${message ? `：${message}` : "，请稍后重试。"}`
              : "MiniCode 会通过已配置的发布源检查更新。";
  const updatesUnavailable = status === "recovery_required" || status === "rollback_launching";
  const actionPending = Boolean(pendingAction);
  return (
    <SettingsGroup title="桌面更新">
      <SettingsRow title="更新通道" description={label}>
        <button type="button" className="settings-action-button" onClick={() => void runUpdateAction("check")} disabled={updatesUnavailable || actionPending || status === "checking" || status === "downloading" || status === "ready"}>
          {pendingAction === "check" ? "正在请求…" : "检查更新"}
        </button>
        {status === "available" && <button type="button" className="settings-action-button" data-primary="true" disabled={actionPending} onClick={() => void runUpdateAction("download")}>{pendingAction === "download" ? "正在请求…" : "下载"}</button>}
        {status === "ready" && <button type="button" className="settings-action-button" data-primary="true" disabled={actionPending} onClick={() => void runUpdateAction("install")}>{pendingAction === "install" ? "正在请求…" : "重启并安装"}</button>}
      </SettingsRow>
    </SettingsGroup>
  );
};

const describeUpdateBlockers = (preflight?: UpdatePreflightResult): string => {
  if (!preflight) return "";
  const labelByCode: Record<string, string> = {
    "update.not_ready": "更新尚未下载完成",
    "install.locked": "另一项安装正在进行",
    "activity.unknown": "尚未取得应用活动状态",
    "activity.invalid": "应用活动状态无效",
    "activity.stale": "应用活动状态已过期",
    "runtime.not_ready": "会话仍在连接或恢复",
    "turn.running": "主任务仍在运行",
    "side_chat.running": "侧聊仍在生成",
    "prompt.pending": "仍有审批、提问或差异确认待处理",
    "attachment.uploading": "附件仍在上传",
    "editor.dirty": "编辑器中仍有未保存内容",
    "task.running": "后台任务仍在运行",
    "pty.running": "终端会话仍在运行",
    "pty.unknown": "无法确认终端状态",
    "pty.invalid": "终端状态无效",
  };
  return preflight.checks
    .filter((check) => check.severity === "blocking")
    .map((check) => labelByCode[check.code] || check.message)
    .join("；");
};
