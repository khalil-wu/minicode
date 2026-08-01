import { useEffect, useState, type ReactNode } from "react";
import { Monitor, Moon, Sun } from "lucide-react";
import type { EffortLevel, PermissionMode, RemoteImagePolicy } from "../stores/types";
import { useAppStore } from "../stores";
import { EFFORT_LEVELS } from "./settingsShared";
import { desktop, isDesktop } from "../desktop/runtime";
import { ModelBrandIcon } from "../components/ModelBrandIcon";

const PERMISSION_CHOICES: { id: PermissionMode; label: string; description: string }[] = [
  { id: "ask_permissions", label: "默认权限", description: "修改文件、运行命令或访问网络前向你确认。" },
  { id: "plan", label: "规划模式", description: "先读取和研究工作区，方案获批后再执行修改。" },
  { id: "auto", label: "自动审核", description: "可读取和编辑工作区文件，并自动批准低风险操作。" },
  { id: "bypass", label: "完全访问权限", description: "无需批准即可编辑文件、运行命令并访问网络，风险较高。" },
];

const REMOTE_IMAGE_CHOICES = [
  { id: "ask", label: "询问", title: "加载远程图片前询问" },
  { id: "allow", label: "允许", title: "自动加载 Markdown 中的远程图片" },
  { id: "block", label: "阻止", title: "不加载任何远程图片" },
] as const;

export const GeneralTab = ({
  permissionMode,
  effortLevel,
  currentModel,
  showReasoningEffort,
  effortOptions,
  switchPermissionMode,
  setEffortLevel,
  remoteImagePolicy,
  setRemoteImagePolicy,
}: {
  permissionMode: PermissionMode;
  effortLevel: string;
  currentModel: string;
  showReasoningEffort: boolean;
  effortOptions?: EffortLevel[];
  switchPermissionMode: (mode: PermissionMode) => void;
  setEffortLevel: (level: EffortLevel) => void;
  remoteImagePolicy: RemoteImagePolicy;
  setRemoteImagePolicy: (policy: RemoteImagePolicy) => void;
}) => {
  const themeMode = useAppStore((s) => s.themeMode);
  const setThemeMode = useAppStore((s) => s.setThemeMode);
  const selectedEffort = effortOptions?.includes(effortLevel as EffortLevel)
    ? effortLevel
    : effortLevel === "max" && effortOptions?.includes("xhigh")
      ? "xhigh"
      : effortOptions?.[0];

  return (
    <>
      <SettingsGroup title="外观">
        <SettingsRow title="主题" description="选择应用的明暗外观，也可以跟随系统设置。">
          <SegmentedControl>
            {([
              { id: "system", label: "跟随系统", icon: <Monitor aria-hidden="true" /> },
              { id: "dark", label: "深色", icon: <Moon aria-hidden="true" /> },
              { id: "light", label: "浅色", icon: <Sun aria-hidden="true" /> },
            ] as const).map((theme) => (
              <button
                key={theme.id}
                type="button"
                className="settings-segment settings-theme-segment"
                data-active={themeMode === theme.id ? "true" : "false"}
                aria-pressed={themeMode === theme.id}
                onClick={() => setThemeMode(theme.id)}
              >
                {theme.icon}
                <span>{theme.label}</span>
              </button>
            ))}
          </SegmentedControl>
        </SettingsRow>
      </SettingsGroup>

      <SettingsGroup title="权限">
        {PERMISSION_CHOICES.map((mode) => (
          <SettingsRow key={mode.id} title={mode.label} description={mode.description}>
            <ChoiceToggle
              active={permissionMode === mode.id}
              label={`使用${mode.label}`}
              warning={mode.id === "bypass"}
              onClick={() => switchPermissionMode(mode.id)}
            />
          </SettingsRow>
        ))}
      </SettingsGroup>

      <SettingsGroup title="常规">
        <SettingsRow title="当前模型" description="新任务和后续对话默认使用的模型。">
          <div className="settings-model-summary">
            <ModelBrandIcon model={currentModel} size={19} framed />
            <span>{currentModel || "尚未配置"}</span>
          </div>
        </SettingsRow>

        {showReasoningEffort && (
          <SettingsRow title="推理强度" description="更高的推理强度更适合复杂任务，但完成时间更长。">
            <SegmentedControl>
              {EFFORT_LEVELS.filter((level) => !effortOptions || effortOptions.includes(level.id)).map((level) => (
                <button
                  key={level.id}
                  type="button"
                  className="settings-segment"
                  data-active={selectedEffort === level.id ? "true" : "false"}
                  aria-pressed={selectedEffort === level.id}
                  onClick={() => setEffortLevel(level.id)}
                  title={level.desc}
                >
                  {level.label}
                </button>
              ))}
            </SegmentedControl>
          </SettingsRow>
        )}

        <SettingsRow title="远程 Markdown 图片" description="控制对话是否可以加载外部网站上的图片。">
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

const ChoiceToggle = ({ active, label, warning = false, onClick }: { active: boolean; label: string; warning?: boolean; onClick: () => void }) => (
  <button
    type="button"
    className="settings-toggle"
    role="switch"
    aria-checked={active}
    aria-label={label}
    data-active={active ? "true" : "false"}
    data-tone={warning ? "warning" : undefined}
    onClick={onClick}
  >
    <span />
  </button>
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
  useEffect(() => {
    const applyStatus = (payload: {
      status: string;
      version?: string;
      previousVersion?: string;
      failedVersion?: string;
      percent?: number;
      message?: string;
    }) => {
      setStatus(payload.status);
      setVersion(payload.version || "");
      setPreviousVersion(payload.previousVersion || "");
      setFailedVersion(payload.failedVersion || "");
      setMessage(payload.message || "");
      setPercent(typeof payload.percent === "number" ? payload.percent : null);
    };
    const updates = desktop()?.updates;
    void updates?.getStatus().then(applyStatus).catch(() => undefined);
    return updates?.onStatus(applyStatus);
  }, []);
  const label = status === "checking" ? "正在检查更新…"
    : status === "available" ? `版本 ${version || "更新"} 已可下载。`
      : status === "downloading" ? `正在下载${percent == null ? "…" : ` ${Math.round(percent)}%`}`
        : status === "ready" ? `版本 ${version || "更新"} 已可安装。`
          : status === "current" ? "MiniCode 已是最新版本。"
            : status === "rollback_launching" ? `正在恢复至 ${version || previousVersion || "上一版本"}…`
              : status === "rolled_back" ? `已自动恢复至 ${version || "上一版本"}${failedVersion ? `；版本 ${failedVersion} 未通过启动检查。` : "。"}`
                : status === "recovery_required" ? `当前版本未通过启动检查${previousVersion ? `，且无法自动启动 ${previousVersion}` : ""}。${message ? ` ${message}` : ""}`
            : status === "error" ? "检查更新失败，请稍后重试。"
              : "MiniCode 会通过已配置的发布源检查更新。";
  const updatesUnavailable = status === "recovery_required" || status === "rollback_launching";
  return (
    <SettingsGroup title="桌面更新">
      <SettingsRow title="更新通道" description={label}>
        <button type="button" className="settings-action-button" onClick={() => void desktop()?.updates.check()} disabled={updatesUnavailable || status === "checking" || status === "downloading"}>
          检查更新
        </button>
        {status === "available" && <button type="button" className="settings-action-button" data-primary="true" onClick={() => void desktop()?.updates.download()}>下载</button>}
        {status === "ready" && <button type="button" className="settings-action-button" data-primary="true" onClick={() => void desktop()?.updates.install()}>重启并安装</button>}
      </SettingsRow>
    </SettingsGroup>
  );
};
