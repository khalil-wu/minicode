import { useI18n, type TranslationKey } from "../i18n";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { FollowUpBehavior, SendShortcut } from "../stores/types";
import { desktop, isDesktop, type UpdatePreflightResult, type UpdateStatus } from "../desktop/runtime";
import { useAppStore } from "../stores";
import { pushToast } from "./ToastContainer";
import { SelectMenu } from "../components/SelectMenu";
import { FileOpenTargetIcon } from "../components/FileOpenTargetIcon";
import { useDesktopPreferences } from "../hooks/useDesktopPreferences";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { ChevronDown, ShieldAlert } from "lucide-react";
import { notificationSoundWav } from "./notificationSoundImport";
import "./GeneralTab.css";
import { setPluginsFeatureEnabled, usePluginsFeature } from "../hooks/usePluginsFeature";

const SEND_SHORTCUT_CHOICES = [
  { id: "enter", label: "按 Enter 键", title: "按 Enter 发送，Shift + Enter 换行" },
  { id: "cmdIfMultiline", label: "{modifierSymbol} + Enter 用于多行提示", title: "单行按 Enter 发送，多行按 Ctrl/Cmd + Enter 发送" },
  { id: "mod-enter", label: "总是使用 {modifierSymbol} + Enter", title: "按 Ctrl/Cmd + Enter 发送，Enter 换行" },
 ] as const satisfies readonly { id: SendShortcut; label: TranslationKey; title: TranslationKey }[];

const FOLLOW_UP_CHOICES = [
  { id: "queue", label: "排队", title: "当前任务结束后再处理新消息" },
  { id: "steer", label: "引导", title: "把新消息立即加入当前任务" },
 ] as const satisfies readonly { id: FollowUpBehavior; label: TranslationKey; title: TranslationKey }[];

export const GeneralTab = ({
  onOpenLicenses,
}: {
  onOpenLicenses?: () => void;
}) => {
  const { t } = useI18n();
  const sendShortcut = useAppStore((s) => s.sendShortcut);
  const setSendShortcut = useAppStore((s) => s.setSendShortcut);
  const followUpBehavior = useAppStore((s) => s.followUpBehavior);
  const setFollowUpBehavior = useAppStore((s) => s.setFollowUpBehavior);
  const modifierSymbol = navigator.platform.startsWith("Mac") ? "⌘" : "^";
  return (
    <>
      {isDesktop() && <DesktopGeneralPreferences onOpenLicenses={onOpenLicenses} />}
      <SettingsGroup title={t("输入框")}>
        <PlainTextComposerPreference />
        {isDesktop() && <ComposerContextUsagePreference />}
        <SettingsRow title={t("发送快捷键")} description={t("选择按 Enter 时是发送提示还是插入新行")}>
          <SelectMenu ariaLabel={t("发送快捷键")} className="settings-select settings-select-regular" align="end" value={sendShortcut}
            onValueChange={(value) => setSendShortcut(value as SendShortcut)}>
            {SEND_SHORTCUT_CHOICES.map(choice => <option key={choice.id} value={choice.id} title={t(choice.title)}>{t(choice.label, { modifierSymbol })}</option>)}
          </SelectMenu>
        </SettingsRow>
        <SettingsRow title={t("跟进行为")} description={t("任务运行时发送新消息的方式。")}>
          <div className="settings-segmented settings-follow-up-segmented" role="radiogroup" aria-label={t("跟进行为")} onKeyDown={event => {
            if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const next = event.key === "Home" ? "queue" : event.key === "End" ? "steer" : followUpBehavior === "queue" ? "steer" : "queue";
            setFollowUpBehavior(next);
            event.currentTarget.querySelector<HTMLButtonElement>(`[data-value="${next}"]`)!.focus();
          }}>
            {FOLLOW_UP_CHOICES.map(choice => <button key={choice.id} type="button" role="radio" className="settings-segment"
              title={t(choice.title)} data-value={choice.id} data-active={followUpBehavior === choice.id}
              aria-checked={followUpBehavior === choice.id} tabIndex={followUpBehavior === choice.id ? 0 : -1}
              onClick={() => setFollowUpBehavior(choice.id)}>{t(choice.label)}</button>)}
          </div>
        </SettingsRow>
      </SettingsGroup>

      {isDesktop() && <DesktopNotificationPreferences />}

      <details className="settings-appearance-advanced"><summary className="settings-appearance-advanced-trigger">{t("高级")}<ChevronDown size={15} aria-hidden="true" /></summary><div className="settings-appearance-advanced-body">
      {isDesktop() && <DefaultPermissionSettings />}
      {isDesktop() && <DesktopUpdates />}
      </div></details>
    </>
  );
};

const SettingsGroup = ({ title, children }: { title: string; children: ReactNode }) => (
  <section className="settings-group">
    <h3 className="settings-group-title">{title}</h3>
    <div className="settings-card">{children}</div>
  </section>
);

const ComposerContextUsagePreference = () => {
  const { t } = useI18n();
  const { preferences, update } = useDesktopPreferences();
  if (!preferences) return null;
  return <SettingsRow title={t("在输入框中显示上下文用量")} description="">
    <button type="button" className="settings-toggle" role="switch" aria-label={t("在输入框中显示上下文用量")}
      aria-checked={preferences.showContextWindowUsage} data-active={preferences.showContextWindowUsage}
      onClick={() => void update({ showContextWindowUsage: !preferences.showContextWindowUsage })}><span /></button>
  </SettingsRow>;
};

const PlainTextComposerPreference = () => {
  const { t } = useI18n();
  const plain = useAppStore(state => state.composerPlainTextMode);
  const setPlain = useAppStore(state => state.setComposerPlainTextMode);
  const [saving, setSaving] = useState(false);
  return <SettingsRow title={t("纯文本输入")} description={t("编写消息时，将代码、Markdown 和链接保留为字面文本。")}>
    <button type="button" className="settings-toggle" role="switch" aria-label={t("纯文本输入")}
      aria-checked={plain} data-active={plain} disabled={saving} onClick={async () => {
        setSaving(true);
        try { await setPlain(!plain); }
        catch (error) { pushToast(String(error), "error"); }
        finally { setSaving(false); }
      }}><span /></button>
  </SettingsRow>;
};

const DefaultTerminalLocationPreference = () => {
  const { t } = useI18n();
  const location = useAppStore(state => state.defaultTerminalLocation);
  const setLocation = useAppStore(state => state.setDefaultTerminalLocation);
  const [saving, setSaving] = useState(false);
  const select = async (next: "bottom" | "right") => {
    setSaving(true);
    try { await setLocation(next); }
    catch (error) { pushToast(String(error), "error"); }
    finally { setSaving(false); }
  };
  return <SettingsRow title={t("默认终端位置")} description={t("选择终端快捷键和环境动作打开终端标签页的位置。")}>
    <div className="settings-segmented" role="radiogroup" aria-label={t("默认终端位置")}
      onKeyDown={event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || saving) return;
        event.preventDefault();
        const next = event.key === "Home" ? "bottom" : event.key === "End" ? "right" : location === "bottom" ? "right" : "bottom";
        void select(next);
        event.currentTarget.querySelector<HTMLButtonElement>('[data-value="' + next + '"]')!.focus();
      }}>
      {(["bottom", "right"] as const).map(value => <button key={value} type="button" role="radio"
        className="settings-segment" data-value={value} data-active={location === value} aria-checked={location === value}
        tabIndex={location === value ? 0 : -1} disabled={saving} onClick={() => void select(value)}>{t(value === "bottom" ? "底部" : "右侧")}</button>)}
    </div>
  </SettingsRow>;
};

const SettingsRow = ({ title, description, children }: { title: string; description: ReactNode; children: ReactNode }) => (
  <div className="settings-row">
    <div className="settings-row-copy">
      <div className="settings-row-title">{title}</div>
      <div className="settings-row-description">{description}</div>
    </div>
    <div className="settings-row-control">{children}</div>
  </div>
);

const DesktopGeneralPreferences = ({ onOpenLicenses }: { onOpenLicenses?: () => void }) => {
  const { t } = useI18n();
  const { preferences, error, update } = useDesktopPreferences();
  const [fullAccessConfirmation, setFullAccessConfirmation] = useState(false);
  const [changingTaskDirectory, setChangingTaskDirectory] = useState(false);
  if (error) return <p role="alert">{error}</p>;
  if (!preferences) return <p role="status">{t("正在读取桌面设置…")}</p>;
  return <>
    <SettingsGroup title={t("权限")}>
      <SettingsRow title={t("默认权限")} description={t("默认权限允许读取和编辑任务文件；需要时，可以请求额外访问权限。") }>
        <button type="button" className="settings-toggle" role="switch" aria-label={t("始终显示默认权限")} aria-checked="true" data-active="true" disabled><span /></button>
      </SettingsRow>
      <SettingsRow title={t("完整访问权限")} description={<>{t("当 MiniCode 以完整访问权限运行时，它无需你的批准即可编辑电脑上的任何文件，并运行可访问网络的命令。这会显著增加数据丢失、泄露或意外行为的风险。")}{" "}<a href={PERMISSIONS_LEARN_MORE_URL} className="settings-full-access-learn-more" onClick={event => {
        event.preventDefault(); void import("../lib/open-preferred-target").then(({ openPreferredWebLink }) => openPreferredWebLink(PERMISSIONS_LEARN_MORE_URL));
      }}>{t("了解更多")}</a></>}>
        <button type="button" className="settings-toggle" role="switch" aria-label={t("完整访问权限")} aria-checked={preferences.showFullAccess} data-active={preferences.showFullAccess}
          onClick={() => preferences.showFullAccess ? void update({ showFullAccess: false }) : setFullAccessConfirmation(true)}><span /></button>
      </SettingsRow>
    </SettingsGroup>
    <SettingsGroup title={t("常规")}>
      {desktop()!.platformInfo.platform === "win32" && <StartupAppsPreference />}
      <SettingsRow title={t("无项目任务文件夹")} description={t("在项目外启动的任务默认存储数据的位置。")}>
        <code className="settings-general-task-path" title={preferences.defaultTaskDirectory}>{preferences.defaultTaskDirectory}</code>
        <div className="settings-general-task-actions">
          <button type="button" className="settings-action-button" disabled={changingTaskDirectory} aria-busy={changingTaskDirectory} onClick={() => {
            setChangingTaskDirectory(true);
            void desktop()!.preferences.chooseTaskDirectory()
              .catch((error) => pushToast(error instanceof Error ? error.message : String(error), "error"))
              .finally(() => setChangingTaskDirectory(false));
          }}>{t("更改")}</button>
          {preferences.taskDirectoryOverride != null && <button type="button" className="settings-action-button settings-general-task-reset" onClick={() => void update({ defaultTaskDirectory: null })}>{t("重置")}</button>}
        </div>
      </SettingsRow>
      <SettingsRow title={t("默认文件打开位置")} description={t("默认打开文件和文件夹的位置。")}>
        <SelectMenu ariaLabel={t("默认文件打开位置")} className="settings-select settings-select-regular" align="end" menuMaxHeight={320}
          renderOptionIcon={(value) => <FileOpenTargetIcon target={preferences.fileOpenTargets.find(target => target.id === value)!} />}
          value={preferences.fileOpenTarget} onValueChange={(value) => void update({ fileOpenTarget: value as typeof preferences.fileOpenTarget })}>
          {preferences.fileOpenTargets.map((target) => <option key={target.id} value={target.id}>{target.label}</option>)}
        </SelectMenu>
      </SettingsRow>
      <SettingsRow title={t("集成终端 Shell")} description={t("选择要在集成终端中打开的 Shell。")}>
        <SelectMenu ariaLabel={t("集成终端 Shell")} className="settings-select settings-select-regular" align="end" value={preferences.terminalShell} onValueChange={(value) => void update({ terminalShell: value as typeof preferences.terminalShell })}>
          {preferences.terminalShellTargets.map((target) => <option key={target.id} value={target.id}>{target.label}</option>)}
        </SelectMenu>
      </SettingsRow>
      <DefaultTerminalLocationPreference />
      <SettingsRow title={t("语言")} description={t("应用界面语言。")}>
        <SelectMenu ariaLabel={t("语言")} className="settings-select settings-select-regular" align="end" value={preferences.localeOverride ?? "auto"} onValueChange={(value) => void update({ localeOverride: value === "auto" ? null : value as "zh-CN" | "en-US" })}>
          <option value="auto">{t("自动检测")}</option><option value="zh-CN">简体中文</option><option value="en-US">English</option>
        </SelectMenu>
      </SettingsRow>
      <SettingsRow title={t("关闭窗口前确认")} description={preferences.confirmBeforeClose === "keyboardOnly" ? t("当关闭标签页快捷键将关闭窗口时提醒。") : t("选择何时在关闭窗口前提醒；Alt+F4 和关闭按钮仅在始终确认时提醒。")}>
        <SelectMenu ariaLabel={t("关闭窗口前确认")} className="settings-select settings-select-regular" align="end" value={preferences.confirmBeforeClose} onValueChange={(value) => void update({ confirmBeforeClose: value as typeof preferences.confirmBeforeClose })}>
          <option value="never">{t("从不")}</option><option value="keyboardOnly">{t("仅关闭快捷键")}</option><option value="always">{t("始终")}</option>
        </SelectMenu>
      </SettingsRow>
      <SettingsRow title={t("底部面板")} description={t("在应用顶部显示底部面板入口。") }>
        <button type="button" className="settings-toggle" role="switch" aria-label={t("底部面板")}
          aria-checked={preferences.showBottomPanel} data-active={preferences.showBottomPanel}
          onClick={() => void update({ showBottomPanel: !preferences.showBottomPanel })}><span /></button>
      </SettingsRow>
      <SettingsRow title={t("自动归档闲置聊天")} description={t("在一段时间没有活动后归档本地聊天。") }>
        <SelectMenu ariaLabel={t("自动归档闲置聊天")} className="settings-select settings-select-regular" align="end"
          value={String(preferences.autoArchiveAfterDays)} onValueChange={value => void update({ autoArchiveAfterDays: Number(value) as typeof preferences.autoArchiveAfterDays })}>
          {[0, 1, 7, 14, 30, 90].map(days => <option key={days} value={days}>{days === 0 ? t("从不") : t("{count} 天", { count: days })}</option>)}
        </SelectMenu>
      </SettingsRow>
      <SettingsRow title={t("压缩本地聊天历史")} description={t("压缩较早的本地聊天历史以节省磁盘空间。重启 MiniCode 后生效。") }>
        <button type="button" className="settings-toggle" role="switch" aria-label={t("压缩本地聊天历史")}
          aria-checked={preferences.localHistoryCompression} data-active={preferences.localHistoryCompression}
          onClick={() => void update({ localHistoryCompression: !preferences.localHistoryCompression })}><span /></button>
      </SettingsRow>
      <SettingsRow title={t("开源许可证")} description={t("已打包依赖的第三方声明。") }>
        <button type="button" className="settings-action-button" onClick={onOpenLicenses}>{t("查看")}</button>
      </SettingsRow>
      <PluginsFeaturePreference />
    </SettingsGroup>

    {fullAccessConfirmation && <FullAccessAvailabilityDialog onClose={() => setFullAccessConfirmation(false)} onConfirm={() => {
      void update({ showFullAccess: true });
      setFullAccessConfirmation(false);
    }} />}
  </>;
};

const PluginsFeaturePreference = () => {
  const { t } = useI18n();
  const feature = usePluginsFeature();
  const [saving, setSaving] = useState(false);
  const [changed, setChanged] = useState(false);
  if (feature.error) return <p role="alert">{feature.error}</p>;
  if (feature.enabled === undefined) return null;
  return <SettingsRow title={t("插件")} description={changed ? t("重启 MiniCode 以完整应用此更改。") : t("允许 MiniCode 使用已安装的插件。") }>
    <button type="button" className="settings-toggle" role="switch" aria-label={t("启用插件")}
      aria-checked={feature.enabled} data-active={feature.enabled} disabled={saving || feature.locked}
      onClick={async () => {
        setSaving(true);
        try { await setPluginsFeatureEnabled(!feature.enabled); setChanged(true); }
        catch (reason) { pushToast(reason instanceof Error ? reason.message : String(reason), "error"); }
        finally { setSaving(false); }
      }}><span /></button>
  </SettingsRow>;
};

const StartupAppsPreference = () => {
  const { t } = useI18n();
  const [status, setStatus] = useState<boolean | null>();
  const [error, setError] = useState("");
  const [opening, setOpening] = useState(false);
  useEffect(() => {
    let active = true;
    const read = () => void desktop()!.preferences.getStartupStatus().then(value => {
      if (active) { setStatus(value); setError(""); }
    }).catch(reason => { if (active) { setStatus(null); setError(reason instanceof Error ? reason.message : String(reason)); } });
    read();
    window.addEventListener("focus", read);
    return () => { active = false; window.removeEventListener("focus", read); };
  }, []);
  return <SettingsRow title={t("登录时启动")} description={<span title={error || undefined}>{
    status === undefined ? t("正在检查启动状态…") : status === null ? t("启动状态不可用") : status ? t("已开启") : t("已关闭")
  }</span>}>
    <button type="button" className="settings-action-button" disabled={opening} onClick={async () => {
      setOpening(true);
      try { await desktop()!.preferences.openStartupSettings(); }
      catch (reason) { pushToast(reason instanceof Error ? reason.message : String(reason), "error"); }
      finally { setOpening(false); }
    }}>{t("打开启动应用")}</button>
  </SettingsRow>;
};

const DesktopNotificationPreferences = () => {
  const { t } = useI18n();
  const { preferences, error, update } = useDesktopPreferences();
  const [soundImporting, setSoundImporting] = useState(false);
  const setNotificationSound = async (value: string) => {
    try {
      let sound = preferences!.notificationSound;
      if (value === "choose-custom") {
        setSoundImporting(true);
        const selected = await desktop()!.preferences.pickNotificationSound();
        if (selected === null) return;
        const saved = await desktop()!.preferences.importNotificationSound({ name: selected.name, wav: await notificationSoundWav(selected.bytes) });
        sound = saved.notificationSound;
      } else {
        sound = value === "custom" ? sound : value as "default" | "classic" | "none";
        await update({ notificationSound: sound });
      }
      await desktop()!.preferences.previewNotificationSound(sound);
    } catch (error) {
      pushToast(error instanceof Error ? error.message : String(error), "error");
    } finally {
      setSoundImporting(false);
    }
  };
  if (error) return <p role="alert">{error}</p>;
  if (!preferences) return null;
  return (
    <SettingsGroup title={t("通知")}>
      <SettingsRow title={t("任务完成通知")} description={t("任务或回复完成时提醒。")}>
        <SelectMenu ariaLabel={t("任务完成通知")} className="settings-select settings-select-regular" align="end" value={preferences.turnCompletionNotifications} onValueChange={(value) => void update({ turnCompletionNotifications: value as typeof preferences.turnCompletionNotifications })}>
          <option value="never">{t("从不")}</option><option value="onlyWhenUnfocused">{t("仅在应用未聚焦时")}</option><option value="always">{t("始终")}</option>
        </SelectMenu>
      </SettingsRow>
      {([['permissionNotifications', t("权限请求通知")], ['questionNotifications', t("问题通知")]] as const).map(([key, label]) => <SettingsRow key={key} title={label} description={key === 'permissionNotifications' ? t("需要批准访问权限时提醒。") : t("需要你回答问题时提醒。")}>
        <button type="button" className="settings-toggle" role="switch" aria-label={label} aria-checked={preferences[key]} data-active={preferences[key]} onClick={() => void update({ [key]: !preferences[key] })}><span /></button>
      </SettingsRow>)}
      <SettingsRow title={t("通知声音")} description={t("任务完成、权限请求和提问时播放的声音。") }>
        <SelectMenu ariaLabel={t("通知声音")} className="settings-select settings-select-regular" align="end" disabled={soundImporting}
          value={typeof preferences.notificationSound === "object" ? "custom" : preferences.notificationSound}
          onValueChange={value => void setNotificationSound(value)}>
          <option value="default">{t("默认")}</option><option value="classic">{t("经典")}</option><option value="none">{t("无")}</option>
          {typeof preferences.notificationSound === "object" && <option value="custom">{preferences.notificationSound.name}</option>}
          <option value="choose-custom">{t("选择自定义声音…")}</option>
        </SelectMenu>
      </SettingsRow>
    </SettingsGroup>
  );
};

const PERMISSIONS_LEARN_MORE_URL = "https://developers.openai.com/codex/concepts/sandboxing#how-you-control-it";
const FullAccessAvailabilityDialog = ({ onClose, onConfirm }: { onClose: () => void; onConfirm: () => void }) => {
  const { t } = useI18n();
  const dialogRef = useFocusTrap(true);
  const title = t("要启用完整访问权限吗？");
  return createPortal(<div className="settings-full-access-backdrop" onClick={onClose}>
    <div ref={dialogRef} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}
      className="settings-full-access-dialog" onClick={event => event.stopPropagation()} onKeyDown={event => {
        if (event.key !== "Escape") return;
        event.preventDefault(); event.stopPropagation(); onClose();
      }}>
      <form onSubmit={event => { event.preventDefault(); onConfirm(); }}>
        <h3><ShieldAlert size={17} aria-hidden="true" />{title}</h3>
        <div className="settings-full-access-copy">
          <p>{t("选中后，MiniCode 可以访问互联网，并在无需请求批准的情况下读取和编辑文件，包括执行可能具有破坏性的命令")}</p>
          <p>{t("开启后会将“完整访问权限”添加到权限菜单中，但不会开启“完整访问权限”。")}{" "}<a href={PERMISSIONS_LEARN_MORE_URL} onClick={event => {
            event.preventDefault(); void desktop()!.openExternal(PERMISSIONS_LEARN_MORE_URL);
          }}>{t("了解更多")}</a></p>
        </div>
        <div className="settings-full-access-actions">
          <button type="button" className="settings-action-button" onClick={onClose}>{t("取消")}</button>
          <button type="submit" className="settings-action-button" data-danger="true"><ShieldAlert size={15} aria-hidden="true" />{t("确认")}</button>
        </div>
      </form>
    </div>
  </div>, document.body);
};

const DefaultPermissionSettings = () => {
  const { t } = useI18n();
  const { preferences, update } = useDesktopPreferences();
  if (!preferences) return null;
  return <SettingsGroup title={t("新任务")}>
    <SettingsRow title={t("默认权限")} description={t("为之后新建的任务设置权限；当前任务保留自己的权限。")}>
      <SelectMenu ariaLabel={t("新任务默认权限")} className="settings-select settings-select-regular" align="end" value={preferences.defaultPermissionMode} onValueChange={(value) => void update({ defaultPermissionMode: value as typeof preferences.defaultPermissionMode })}>
        <option value="confirm">{t("默认权限")}</option><option value="auto">{t("自动批准")}</option><option value="plan">{t("计划")}</option><option value="bypass">{t("完全访问")}</option>
      </SelectMenu>
    </SettingsRow>
  </SettingsGroup>;
};

const DesktopUpdates = () => {
  const { t } = useI18n();
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
    let cancelled = false;
    const requestedSequence = statusSequenceRef.current;
    const applyStatus = (payload: UpdateStatus) => {
      if (cancelled) return;
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
      if (cancelled || statusSequenceRef.current !== requestedSequence) return;
      const detail = error instanceof Error ? error.message : String(error || "未知错误");
      setStatus("error");
      setMessage(detail);
    });
    return () => { cancelled = true; unsubscribe?.(); };
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
            : status === "unavailable" ? "此构建未启用自动更新。"
              : status === "error" ? `更新服务失败${message ? `：${message}` : "，请稍后重试。"}`
              : "MiniCode 会通过已配置的发布源检查更新。";
  const updatesUnavailable = status === "unavailable" || status === "recovery_required" || status === "rollback_launching";
  const actionPending = Boolean(pendingAction);
  return (
    <SettingsGroup title={t("桌面更新")}>
      <SettingsRow title={t("更新通道")} description={label}>
        <button type="button" className="settings-action-button" onClick={() => void runUpdateAction("check")} disabled={updatesUnavailable || actionPending || status === "checking" || status === "downloading" || status === "ready"}>
          {pendingAction === "check" ? t("正在请求…") : t("检查更新")}
        </button>
        {status === "available" && <button type="button" className="settings-action-button" data-primary="true" disabled={actionPending} onClick={() => void runUpdateAction("download")}>{pendingAction === "download" ? t("正在请求…") : t("下载")}</button>}
        {status === "ready" && <button type="button" className="settings-action-button" data-primary="true" disabled={actionPending} onClick={() => void runUpdateAction("install")}>{pendingAction === "install" ? t("正在请求…") : t("重启并安装")}</button>}
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
    "download.active": "浏览器下载仍在进行",
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
