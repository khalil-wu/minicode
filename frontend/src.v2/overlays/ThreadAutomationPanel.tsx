import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, Info, MoreHorizontal, Search } from "../lib/icons";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import type { ConversationMeta, ScheduledTaskEntry } from "../stores/types";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { SelectMenu } from "../components/SelectMenu";
import { ContextMenu } from "../components/ContextMenu";
import { Tip } from "../components/Tooltip";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { getDesktopPreferences } from "../desktop/runtime";
import { selectableModelsForProvider } from "../lib/provider-models";
import { projectExecutionRoot, projectOwnsConversation, projectOwnsWorkspace } from "../lib/project-registry";
import { workspaceDisplayName } from "../lib/workspace-display";
import {
  loadThreadAutomations, openThreadAutomationSettings, removeThreadAutomationEditor, renameThreadAutomationEditor,
  saveThreadAutomationEditor, type ThreadAutomationEditor,
} from "../lib/automations-navigation";
import {
  buildAutomationSchedule, buildCustomSchedule, parseAutomationRRule, readAutomationSchedule, readCustomSchedule,
  type AutomationScheduleDraft, type CustomScheduleDraft,
} from "../lib/automation-schedule";
import { AutomationFrequencySection } from "./AutomationFrequencySection";
import { pushToast } from "./ToastContainer";
import "./thread-automation-panel.css";

const localTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error);
const taskKind = (task?: ScheduledTaskEntry) => task?.kind ?? (task && !task.conversation_id ? "cron" : "heartbeat");
const sourceIcon = (name: string) => <span className="automation-source-icon" aria-hidden="true" style={{ maskImage: `url(${import.meta.env.BASE_URL}apps/automation-${name}.svg)` }} />;
const DetailRow = ({ label, children }: { label: string; children: ReactNode }) => <div className="automation-detail-row"><span>{label}</span>{children}</div>;

export function ThreadAutomationPanel({ editor, onClose, inSettings = false }: { editor: ThreadAutomationEditor; onClose?: () => void; inSettings?: boolean }) {
  const { t: tr } = useI18n();
  const projects = useAppStore(state => state.recentWorkspaces);
  const conversations = useAppStore(state => state.conversations);
  const selectedConversationId = useAppStore(state => state.conversationId);
  const currentProvider = useAppStore(state => state.currentProvider);
  const currentModel = useAppStore(state => state.currentModel);
  const availableModels = useAppStore(state => state.availableModels);
  const availableModelLabels = useAppStore(state => state.availableModelLabels);
  const availableModelOptions = useAppStore(state => state.availableModelOptions);
  const modelsSource = useAppStore(state => state.modelsSource);
  const [catalog, setCatalog] = useState(() => {
    const state = useAppStore.getState();
    if (state.conversationId !== editor.owner.id) return { provider: editor.task?.provider ?? "", currentModel: "", labels: {} as Record<string, string>, models: [] as string[], options: [] as typeof state.availableModelOptions };
    return { provider: state.currentProvider as string, currentModel: state.currentModel, labels: state.availableModelLabels,
      models: selectableModelsForProvider(state.availableModels, state.currentModel, state.currentProvider, state.modelsSource), options: state.availableModelOptions };
  });
  useEffect(() => {
    if (selectedConversationId !== editor.owner.id) return;
    setCatalog({ provider: currentProvider, currentModel, labels: availableModelLabels,
      models: selectableModelsForProvider(availableModels, currentModel, currentProvider, modelsSource), options: availableModelOptions });
  }, [editor.owner.id, selectedConversationId, currentProvider, currentModel, availableModels, availableModelLabels, availableModelOptions, modelsSource]);
  const [savedTask, setSavedTask] = useState(editor.task);
  const [name, setName] = useState(editor.task?.name ?? editor.owner.title);
  const [prompt, setPrompt] = useState(editor.task?.prompt ?? "");
  const [kind, setKind] = useState(taskKind(editor.task));
  const [targetId, setTargetId] = useState(editor.task?.conversation_id || editor.owner.id);
  const [executionRoot, setExecutionRoot] = useState<string | null>(editor.task?.execution_workspace_root ?? null);
  const [provider, setProvider] = useState<string | null>(editor.task?.provider ?? null);
  const [model, setModel] = useState<string | null>(editor.task?.model ?? null);
  const [effort, setEffort] = useState<string | null>(editor.task?.reasoning_effort ?? null);
  const [notificationPolicy, setNotificationPolicy] = useState(editor.task?.notification_policy ?? null);
  const [schedule, setSchedule] = useState(() => readAutomationSchedule(editor.task?.schedule));
  const [custom, setCustom] = useState(() => readCustomSchedule(schedule.customRrule));
  const [customDirty, setCustomDirty] = useState(false);
  const [scheduleDirty, setScheduleDirty] = useState(false);
  const [originalSchedule, setOriginalSchedule] = useState(editor.task?.schedule ?? buildAutomationSchedule(schedule)!);
  const [pending, setPending] = useState<"save" | "toggle" | "delete" | null>(null);
  const pendingRef = useRef(false);
  const deleted = useRef(false);
  const [failedPatch, setFailedPatch] = useState<string | null>(null);
  const [saveError, setSaveError] = useState("");
  const [actionMenu, setActionMenu] = useState<HTMLElement | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const effectiveSchedule = scheduleDirty ? schedule.mode === "custom" && custom && customDirty ? buildCustomSchedule(custom) : buildAutomationSchedule(schedule) : originalSchedule;
  const fields = useMemo(() => ({
    name: name.trim(), prompt: prompt.trim(), schedule: effectiveSchedule ?? "", kind,
    timezone: savedTask?.timezone ?? localTimezone, isolation: savedTask?.isolation ?? "workspace" as const,
    permission_mode: savedTask?.permission_mode ?? "auto", notification_policy: notificationPolicy,
    conversation_id: kind === "heartbeat" ? targetId : "",
    execution_workspace_root: kind === "cron" ? executionRoot : null,
    provider: kind === "cron" ? provider : null, model: kind === "cron" ? model : null,
    reasoning_effort: kind === "cron" ? effort : null,
  }), [name, prompt, effectiveSchedule, kind, savedTask?.timezone, savedTask?.isolation, savedTask?.permission_mode, notificationPolicy, targetId, executionRoot, provider, model, effort]);
  const patch = JSON.stringify(fields);
  const [savedPatch, setSavedPatch] = useState(patch);
  const canSave = Boolean(fields.name && fields.prompt && effectiveSchedule && (kind === "cron" || targetId));
  const ownerScope = { owner_conversation_id: editor.owner.id, workspace_root: editor.owner.workspaceRoot };
  const currentTarget = conversations.find(conversation => conversation.id === targetId);
  const targetLabel = currentTarget?.title ?? (targetId === editor.owner.id ? editor.owner.title : tr("聊天不可用"));
  const root = executionRoot ?? savedTask?.workspace_root ?? editor.owner.workspaceRoot;
  const selectedProject = projects.find(project => projectOwnsWorkspace(project, root));
  const projectValue = root === "" ? "__none__" : selectedProject?.path ?? "__owner__";
  const modelOptions = model ? catalog.options.find(option => option.model === model) : undefined;
  const effortOptions = modelOptions?.reasoningEffortLevels ?? [];
  const selectableModels = provider && provider !== catalog.provider ? [] : catalog.models;
  const savedTaskRef = useRef(savedTask);
  const externalTaskRef = useRef(editor.task);
  savedTaskRef.current = savedTask;

  // Settings and the kept-mounted sidebar share the acknowledged task. Adopt an
  // external edit only when this pane has no unsaved patch of its own.
  useEffect(() => {
    const task = editor.task;
    if (task === externalTaskRef.current) return;
    externalTaskRef.current = task;
    if (!task || task === savedTask || pending || patch !== savedPatch) return;
    const nextKind = taskKind(task);
    if (task.name !== savedTask?.name) setName(task.name);
    if (task.prompt !== savedTask?.prompt) setPrompt(task.prompt);
    setKind(nextKind); setTargetId(task.conversation_id || editor.owner.id);
    setExecutionRoot(task.execution_workspace_root ?? null); setProvider(task.provider ?? null);
    setModel(task.model ?? null); setEffort(task.reasoning_effort ?? null); setNotificationPolicy(task.notification_policy ?? null);
    const nextSchedule = readAutomationSchedule(task.schedule);
    setSchedule(nextSchedule); setCustom(readCustomSchedule(nextSchedule.customRrule));
    setOriginalSchedule(task.schedule); setScheduleDirty(false); setCustomDirty(false);
    setSavedTask(task);
    setSavedPatch(JSON.stringify({ name: task.name.trim(), prompt: task.prompt.trim(), schedule: task.schedule, kind: nextKind,
      timezone: task.timezone ?? localTimezone, isolation: task.isolation ?? "workspace", permission_mode: task.permission_mode,
      notification_policy: task.notification_policy ?? null, conversation_id: nextKind === "heartbeat" ? task.conversation_id || editor.owner.id : "",
      execution_workspace_root: nextKind === "cron" ? task.execution_workspace_root ?? null : null,
      provider: nextKind === "cron" ? task.provider ?? null : null, model: nextKind === "cron" ? task.model ?? null : null,
      reasoning_effort: nextKind === "cron" ? task.reasoning_effort ?? null : null }));
  }, [editor.task, editor.owner.id, savedTask, pending, patch, savedPatch]);

  const save = useCallback(async () => {
    if (!canSave || pendingRef.current || deleted.current) return;
    pendingRef.current = true;
    setPending("save"); setSaveError(""); setFailedPatch(null);
    const requestedPatch = patch;
    try {
      const projectlessBase = fields.execution_workspace_root === "" ? (await getDesktopPreferences()).defaultTaskDirectory : null;
      const configuration = { ...fields, projectless_base: projectlessBase,
        owner_conversation_id: editor.owner.id, workspace_root: editor.owner.workspaceRoot };
      const task = savedTaskRef.current;
      const result = await sendClientCommandAwaitResult(task
        ? { type: "scheduler.update", task_id: task.id, task_workspace_root: task.workspace_root, ...configuration }
        : { type: "scheduler.add", ...configuration }, task ? "scheduler.update" : "scheduler.add");
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("无法保存定时任务"));
      const saved = (result.data as unknown as { task: ScheduledTaskEntry }).task;
      setSavedTask(saved); setSavedPatch(requestedPatch);
      saveThreadAutomationEditor(editor.id, saved);
    } catch (error) {
      const message = errorMessage(error);
      setFailedPatch(requestedPatch); setSaveError(message);
      pushToast(`${tr("无法保存定时任务")}：${message}`, "error");
    } finally { pendingRef.current = false; setPending(null); }
  }, [canSave, patch, fields, editor.id, editor.owner.id, editor.owner.workspaceRoot, tr]);

  useEffect(() => {
    if (!savedTask || !canSave || pending || deleteOpen || deleted.current || patch === savedPatch || patch === failedPatch) return;
    const timeout = window.setTimeout(() => { void save(); }, 600);
    return () => window.clearTimeout(timeout);
  }, [savedTask, canSave, pending, deleteOpen, patch, savedPatch, failedPatch, save]);

  const changeSchedule = (next: AutomationScheduleDraft) => {
    if (next.mode === "weekly" && schedule.mode !== "weekly") next = { ...next, weekdays: [next.weekdays[0]] };
    if (next.mode === "custom" && schedule.mode !== "custom") {
      const value = effectiveSchedule && parseAutomationRRule(effectiveSchedule) ? effectiveSchedule : buildAutomationSchedule(schedule) ?? "";
      next = { ...next, customRrule: value };
      setCustom(readCustomSchedule(value));
      setCustomDirty(false);
    }
    setSchedule(next); setScheduleDirty(true);
  };
  const changeCustom = (next: CustomScheduleDraft) => { setCustom(next); setCustomDirty(true); setScheduleDirty(true); };
  const saveRule = (value: string) => { setSchedule(current => ({ ...current, customRrule: value })); setCustom(readCustomSchedule(value)); setCustomDirty(false); setScheduleDirty(true); };
  const toggle = async () => {
    if (!savedTask || pendingRef.current) return;
    pendingRef.current = true; setPending("toggle");
    try {
      const result = await sendClientCommandAwaitResult({ type: "scheduler.toggle", task_id: savedTask.id, task_workspace_root: savedTask.workspace_root, enabled: !savedTask.enabled, ...ownerScope }, "scheduler.toggle");
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("无法更新定时任务状态"));
      const task = (result.data as unknown as { task: ScheduledTaskEntry }).task;
      setSavedTask(task); saveThreadAutomationEditor(editor.id, task);
    } catch (error) { pushToast(errorMessage(error), "error"); }
    finally { pendingRef.current = false; setPending(null); }
  };
  const remove = async () => {
    if (!savedTask || pendingRef.current) return;
    pendingRef.current = true; setPending("delete");
    try {
      const result = await sendClientCommandAwaitResult({ type: "scheduler.remove", task_id: savedTask.id, task_workspace_root: savedTask.workspace_root, ...ownerScope }, "scheduler.remove");
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("无法删除定时任务"));
      deleted.current = true;
      onClose?.(); removeThreadAutomationEditor(editor.id);
    } catch (error) { pushToast(errorMessage(error), "error"); }
    finally { pendingRef.current = false; setPending(null); }
  };

  return <div className={`thread-automation-panel${inSettings ? " thread-automation-in-settings" : ""}`}>
    <div className="thread-automation-scroll"><div className="thread-automation-content">
      <div className="automation-heading"><input className="automation-title" value={name} aria-label={tr("任务名称")} placeholder={tr("任务名称")}
        onChange={event => { setName(event.target.value); renameThreadAutomationEditor(editor.id, event.target.value); }} />
        {savedTask && <button type="button" className="mc-icon-button" aria-label={tr("定时任务操作")} title={tr("定时任务操作")} aria-haspopup="menu" aria-expanded={Boolean(actionMenu)}
          disabled={Boolean(pending)} onClick={event => setActionMenu(event.currentTarget)}><MoreHorizontal size={16} /></button>}
      </div>
      <textarea className="automation-prompt" aria-label={tr("提示词")} placeholder={tr("要运行的提示词")} value={prompt} rows={3} onChange={event => setPrompt(event.target.value)} />
      <section className="automation-section"><div className="automation-section-heading"><h3>{tr("详情")}</h3>
        {kind === "cron" && <Tip content={fields.permission_mode === "bypass"
          ? tr("此定时任务使用完全访问权限，可在后台修改文件、运行命令和访问网络，无需确认。")
          : fields.permission_mode === "confirm"
            ? tr("此定时任务使用需要确认的权限设置。需要授权的操作会等待你的确认。")
            : tr("此定时任务使用正常自动权限。每次新建聊天时会继承任务配置的权限规则。") }>
          <button type="button" className="mc-icon-button automation-permission-info" aria-label={tr("定时任务权限详情")}><Info size={16} /></button>
        </Tip>}
      </div>
        <DetailRow label={tr("运行于")}><AutomationTargetPicker editor={editor} taskId={savedTask?.id} value={kind === "cron" ? "" : targetId} label={kind === "cron" ? tr("每次新建聊天") : targetLabel}
          onChange={(id, title) => { setKind(id ? "heartbeat" : "cron"); if (id) setTargetId(id); if (!name.trim() && title) { setName(title); renameThreadAutomationEditor(editor.id, title); } }} /></DetailRow>
        {kind === "cron" && <>
          <DetailRow label={tr("项目")}><SelectMenu className="automation-select" value={projectValue} selectedLabel={root === "" ? tr("无项目") : undefined} ariaLabel={tr("定时任务项目")} align="end"
            onValueChange={value => setExecutionRoot(value === "__none__" ? "" : value === "__owner__" ? null : projectExecutionRoot(projects.find(project => project.path === value)!))}>
            <option value="__none__">{tr("不在项目中工作")}</option>
            {!selectedProject && root !== "" && <option value="__owner__">{workspaceDisplayName(root)}</option>}
            {projects.map(project => <option key={project.path} value={project.path}>{project.name || workspaceDisplayName(project.path)}</option>)}
          </SelectMenu></DetailRow>
          <DetailRow label={tr("模型")}><SelectMenu className="automation-select" value={model ?? ""} ariaLabel={tr("定时任务模型")} align="end" onValueChange={value => {
            setModel(value || null); setProvider(value ? catalog.provider : null);
            setEffort(value ? catalog.options.find(option => option.model === value)?.defaultReasoningEffort ?? null : null);
          }}>
            <option value="">{tr("默认")}</option>
            {model && !selectableModels.includes(model) && <option value={model} disabled>{model}</option>}
            {selectableModels.map(value => <option key={value} value={value}>{catalog.labels[value] || value}</option>)}
          </SelectMenu></DetailRow>
          <DetailRow label={tr("思考强度")}><SelectMenu className="automation-select" value={effort ?? ""} disabled={!model || !effortOptions.length} ariaLabel={tr("定时任务思考强度")} align="end" onValueChange={value => setEffort(value || null)}>
            <option value="">{tr("默认")}</option>
            {effort && !effortOptions.includes(effort) && <option value={effort} disabled>{effort}</option>}
            {effortOptions.map(value => <option key={value} value={value}>{value}</option>)}
          </SelectMenu></DetailRow>
        </>}
      </section>
      <AutomationFrequencySection draft={schedule} custom={custom} onChange={changeSchedule} onCustomChange={changeCustom} onRuleSave={saveRule} afterScheduleRows={
      <DetailRow label={tr("通知")}><SelectMenu className="automation-select" value={notificationPolicy ?? ""} onValueChange={value => setNotificationPolicy(value === "failed_runs_only" ? value : null)} ariaLabel={tr("通知")} align="end">
        <option value="">{kind === "heartbeat" ? tr("重要更新") : tr("所有运行")}</option><option value="failed_runs_only">{tr("仅失败运行")}</option>
      </SelectMenu></DetailRow>} />
    </div></div>
    {(!inSettings || canSave && failedPatch === patch) && <div className="thread-automation-footer">
      {savedTask ? <>
        {canSave && failedPatch === patch && <button type="button" title={saveError} disabled={Boolean(pending)} onClick={() => void save()}>{tr("重试保存")}</button>}
        {!inSettings && <button type="button" onClick={() => openThreadAutomationSettings(editor.owner, savedTask)}>{tr("打开设置")}<span className="automation-source-icon" aria-hidden="true" style={{ maskImage: `url(${import.meta.env.BASE_URL}apps/open-in.svg)` }} /></button>}
      </> : <><button type="button" className="automation-ghost-button" disabled={Boolean(pending)} onClick={onClose}>{tr("取消")}</button><button type="button" disabled={!canSave || Boolean(pending)} onClick={() => void save()}>{pending === "save" ? tr("正在保存…") : tr("创建定时任务")}</button></>}
    </div>}
    {actionMenu && savedTask && <ContextMenu anchor={actionMenu} position={{ x: actionMenu.getBoundingClientRect().right, y: actionMenu.getBoundingClientRect().bottom }} onClose={() => setActionMenu(null)} ariaLabel={tr("定时任务操作")} items={[
      { label: savedTask.enabled ? tr("暂停") : tr("恢复"), icon: sourceIcon(savedTask.enabled ? "pause" : "resume"), onClick: () => { void toggle(); } },
      { label: tr("删除"), icon: sourceIcon("delete"), onClick: () => setDeleteOpen(true) },
    ]} />}
    {deleteOpen && savedTask && <AutomationDeleteDialog name={savedTask.name} pending={pending === "delete"} onClose={() => { if (pending !== "delete") setDeleteOpen(false); }} onConfirm={() => void remove()} />}
  </div>;
}

function AutomationTargetPicker({ editor, taskId, value, label, onChange }: {
  editor: ThreadAutomationEditor; taskId?: string; value: string; label: string; onChange: (id: string, title?: string) => void;
}) {
  const { t: tr } = useI18n();
  const allConversations = useAppStore(state => state.conversations);
  const projects = useAppStore(state => state.recentWorkspaces);
  const [tasks, setTasks] = useState<ScheduledTaskEntry[]>([]);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const localChats = allConversations.filter(conversation => !conversation.archived && !conversation.parentConversationId && conversation.conversationType !== "side_chat"
    && conversation.environment !== "remote" && conversation.environment !== "ssh" && !["subagent", "pull_request_fix_automation", "aeon"].includes(conversation.threadSource ?? ""));
  const close = () => { setOpen(false); trigger.current!.focus(); };
  useEffect(() => {
    if (!open) return;
    search.current!.focus();
    let cancelled = false;
    setLoading(true); setError("");
    // Each inventory is scoped to its real chat owner. A selected main chat's
    // global projection cannot answer whether another project's chat is occupied.
    const owners = [editor.owner];
    for (const conversation of localChats) {
      const workspaceRoot = conversation.executionCwd || conversation.worktreePath || conversation.workspaceRoot;
      if (workspaceRoot && !owners.some(owner => owner.id === conversation.id)) {
        owners.push({ id: conversation.id, title: conversation.title, workspaceRoot, permissionMode: editor.owner.permissionMode });
      }
    }
    void Promise.all(owners.map(owner => loadThreadAutomations(owner))).then(snapshots => {
      if (!cancelled) setTasks(snapshots.flat());
    }).catch(error => { if (!cancelled) setError(errorMessage(error)); }).finally(() => { if (!cancelled) setLoading(false); });
    const outside = (event: MouseEvent) => { if (!root.current!.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", outside);
    return () => { cancelled = true; document.removeEventListener("mousedown", outside); };
  }, [open, editor.owner.id, editor.owner.workspaceRoot]);
  const conversations = localChats;
  const occupied = new Set(tasks.filter(task => task.id !== taskId && task.enabled && taskKind(task) === "heartbeat").map(task => task.conversation_id));
  const filtered = conversations.filter(conversation => conversation.title.toLowerCase().includes(query.toLowerCase()));
  const groups = new Map<string, ConversationMeta[]>();
  for (const conversation of filtered) {
    const project = projects.find(project => projectOwnsConversation(project, conversation));
    const group = conversation.pinned ? tr("已固定") : project?.name || (project ? workspaceDisplayName(project.path) : tr("其他聊天"));
    groups.set(group, [...(groups.get(group) ?? []), conversation]);
  }
  const groupOrder = [tr("已固定"), ...projects.map(project => project.name || workspaceDisplayName(project.path)), tr("其他聊天")];
  return <div className="automation-target-picker" ref={root}>
    <button ref={trigger} type="button" className="automation-target-trigger" aria-label={tr("定时任务目标")} title={label} aria-haspopup="dialog" aria-expanded={open}
      onClick={() => { setOpen(current => !current); setQuery(""); }}><span>{label}</span><ChevronDown size={14} /></button>
    {open && <div className="mc-dropdown-menu automation-target-menu" role="dialog" aria-label={tr("搜索聊天")} onKeyDown={event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) || event.target instanceof HTMLInputElement && event.key !== "ArrowDown") return;
      event.preventDefault();
      const buttons = Array.from(root.current!.querySelectorAll<HTMLButtonElement>('.automation-target-menu button:not([disabled])'));
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      buttons[event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length].focus();
    }}>
      <label className="automation-chat-search"><Search size={15} /><input ref={search} value={query} onChange={event => setQuery(event.target.value)} aria-label={tr("搜索聊天")} placeholder={tr("搜索聊天")} /></label>
      <div className="automation-chat-options">
        <button type="button" aria-pressed={!value} onClick={() => { onChange(""); close(); }}><span>{tr("每次新建聊天")}</span>{!value && <Check size={14} />}</button>
        {[...groups].sort(([left], [right]) => groupOrder.indexOf(left) - groupOrder.indexOf(right)).map(([group, chats]) => <div key={group} role="group" aria-label={group}><div className="automation-chat-group">{group}</div>{chats.map(conversation => <button key={conversation.id} type="button" aria-pressed={value === conversation.id}
          disabled={loading || Boolean(error) || !Boolean(conversation.executionCwd || conversation.worktreePath || conversation.workspaceRoot || conversation.id === editor.owner.id) || occupied.has(conversation.id)} title={occupied.has(conversation.id) ? tr("此聊天已有运行中的定时任务") : conversation.title}
          onClick={() => { onChange(conversation.id, conversation.title); close(); }}><span>{conversation.title}</span>{value === conversation.id && <Check size={14} />}</button>)}</div>)}
        {value && !conversations.some(conversation => conversation.id === value) && <button type="button" disabled aria-pressed="true"><span>{label}</span><Check size={14} /></button>}
        {!filtered.length && <p>{tr("没有匹配的聊天")}</p>}
        {loading && <p role="status">{tr("正在读取定时任务…")}</p>}{error && <p role="alert">{error}</p>}
      </div>
    </div>}
  </div>;
}

function AutomationDeleteDialog({ name, pending, onClose, onConfirm }: { name: string; pending: boolean; onClose: () => void; onConfirm: () => void }) {
  const { t: tr } = useI18n();
  const id = useId();
  const ref = useFocusTrap(true);
  return createPortal(<div className="mc-dialog-backdrop automation-dialog-backdrop" role="presentation" onClick={onClose}>
    <div ref={ref} className="mc-dialog-panel automation-dialog" role="dialog" aria-modal="true" aria-labelledby={id} aria-describedby={`${id}-description`} tabIndex={-1}
      onClick={event => event.stopPropagation()} onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); } }}>
      <h2 id={id}>{tr("删除 {name}？", { name })}</h2><p id={`${id}-description`}>{tr("这将永久删除定时任务并停止后续运行。")}</p>
      <div className="automation-dialog-actions"><button type="button" disabled={pending} onClick={onClose}>{tr("取消")}</button><button type="button" className="automation-danger-button" disabled={pending} onClick={onConfirm}>{pending ? tr("正在删除…") : tr("删除定时任务")}</button></div>
    </div>
  </div>, document.body);
}
