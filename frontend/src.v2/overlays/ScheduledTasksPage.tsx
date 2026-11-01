import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MoreHorizontal, Plus, Search, X } from "../lib/icons";
import { SidebarSortSourceIcon } from "../lib/sidebar-sort-icons";
import { useI18n, type Translate } from "../i18n";
import { useAppStore } from "../stores";
import type { ScheduledTaskEntry, ScheduledTaskRunEntry } from "../stores/types";
import { commandResultSucceeded, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import {
  closeAutomations, removeThreadAutomationEditor, saveThreadAutomationEditor, useAutomationsPageState, useAutomationSettingsIntent, useThreadAutomationEditors,
  type ThreadAutomationEditor, type ThreadAutomationOwner,
} from "../lib/automations-navigation";
import { readAutomationSchedule, readCustomSchedule } from "../lib/automation-schedule";
import { workspaceDisplayName } from "../lib/workspace-display";
import { ContextMenu } from "../components/ContextMenu";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { ThreadAutomationPanel } from "./ThreadAutomationPanel";
import { showConfirm } from "./DialogService";
import { pushToast } from "./ToastContainer";
import "./ScheduledTasksPage.css";

interface ScheduledTaskRow { task: ScheduledTaskEntry; owner: ThreadAutomationOwner }
interface PageEditor { editor: ThreadAutomationEditor; creating: boolean }
type StatusFilter = "all" | "active" | "paused" | "completed";
const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error);
const labels = (tr: Translate) => ({ all: tr("所有任务"), active: tr("进行中"), paused: tr("已暂停"), completed: tr("已完成") });
const taskOwner = (task: ScheduledTaskEntry): ThreadAutomationOwner => ({ id: task.conversation_id ?? "", title: task.name,
  workspaceRoot: task.workspace_root!, permissionMode: task.permission_mode as ThreadAutomationOwner["permissionMode"], schedulerScope: "global" });

function scheduleLabel(tr: Translate, value: string) {
  const draft = readAutomationSchedule(value);
  if (draft.mode === "hourly") return tr("每 {minutes} 分钟", { minutes: draft.intervalMinutes });
  if (draft.mode === "daily") return `${tr("每天")} ${draft.time}`;
  if (draft.mode === "weekdays") return `${tr("工作日")} ${draft.time}`;
  if (draft.mode === "weekly") return `${tr("每周")} ${draft.time}`;
  const custom = readCustomSchedule(draft.customRrule);
  if (custom?.frequency === "weekly") return tr("每 {weeks} 周", { weeks: custom.interval });
  return tr("自定义计划");
}

export function ScheduledTasksPage({ active = true }: { active?: boolean }) {
  const { t: tr } = useI18n();
  const { sidebarOpen, navigationId } = useAutomationsPageState();
  const settingsIntent = useAutomationSettingsIntent();
  const conversations = useAppStore(state => state.conversations);
  const conversationId = useAppStore(state => state.conversationId);
  const workingDirectory = useAppStore(state => state.workingDirectory);
  const permissionMode = useAppStore(state => state.permissionMode);
  const inventory = useAppStore(state => state.scheduledTasks);
  const runInventory = useAppStore(state => state.scheduledTaskRuns);
  const isConnected = useAppStore(state => state.isConnected);
  const sidebarWidth = useAppStore(state => state.leftSidebarExpandedWidth);
  const threadEditors = useThreadAutomationEditors();
  const currentOwner = useMemo(() => {
    const conversation = conversations.find(item => item.id === conversationId);
    if (!conversation || conversation.archived || conversation.environment === "remote" || conversation.environment === "ssh") return null;
    return { id: conversation.id, title: conversation.title, permissionMode,
      workspaceRoot: conversation.executionCwd || conversation.worktreePath || conversation.workspaceRoot || workingDirectory };
  }, [conversations, conversationId, workingDirectory, permissionMode]);
  const [rows, setRows] = useState<ScheduledTaskRow[]>([]);
  const [runs, setRuns] = useState(runInventory);
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [filterAnchor, setFilterAnchor] = useState<HTMLButtonElement | null>(null);
  const [taskMenu, setTaskMenu] = useState<{ row: ScheduledTaskRow; anchor: HTMLButtonElement } | null>(null);
  const [pendingTaskId, setPendingTaskId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [listError, setListError] = useState("");
  const [reload, setReload] = useState(0);
  const [editors, setEditors] = useState<PageEditor[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [createId, setCreateId] = useState<string | null>(null);
  useEffect(() => { if (searchOpen && active) searchRef.current!.focus(); }, [searchOpen, active]);

  // The local aggregate is independent of the loaded chat history. Its ACK
  // never replaces the current workspace's scoped task inventory or draft.
  useEffect(() => {
    if (!active || !isConnected) { setLoading(false); return; }
    let cancelled = false;
    setLoading(true); setListError("");
    void sendClientCommandAwaitResult({ type: "scheduler.list", scheduler_scope: "global" }, "scheduler.list").then(result => {
      if (cancelled) return;
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("无法读取定时任务。"));
      const data = result.data as unknown as { tasks: ScheduledTaskEntry[]; runs: ScheduledTaskRunEntry[] };
      setRows(data.tasks.map(task => ({ task, owner: taskOwner(task) }))); setRuns(data.runs);
    }).catch(error => { if (!cancelled) setListError(errorMessage(error)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [active, isConnected, reload, tr]);

  const previousInventory = useRef(inventory);
  useEffect(() => {
    if (inventory === previousInventory.current) return;
    previousInventory.current = inventory;
    setReload(version => version + 1);
  }, [inventory]);
  useEffect(() => {
    setRuns(previous => [...new Map([...previous, ...runInventory].map(run => [run.id, run])).values()]);
  }, [runInventory]);

  useEffect(() => {
    setRows(previous => previous.map(row => {
      const task = threadEditors.find(editor => editor.task?.id === row.task.id)?.task;
      return task ? { ...row, task } : row;
    }));
  }, [threadEditors]);

  const acknowledgeTask = (task: ScheduledTaskEntry, owner: ThreadAutomationOwner) => {
    const mountedEditor = threadEditors.find(editor => editor.task?.id === task.id);
    if (mountedEditor && mountedEditor.task !== task) saveThreadAutomationEditor(mountedEditor.id, task);
    setRows(previous => {
      const next = { task, owner };
      return previous.some(row => row.task.id === task.id) ? previous.map(row => row.task.id === task.id ? next : row) : [...previous, next];
    });
  };
  const forgetTask = (task: ScheduledTaskEntry) => {
    const mountedEditor = threadEditors.find(editor => editor.task?.id === task.id);
    if (mountedEditor) removeThreadAutomationEditor(mountedEditor.id);
    setRows(previous => previous.filter(row => row.task.id !== task.id));
    setEditors(previous => previous.filter(entry => entry.editor.task?.id !== task.id));
    setSelectedId(current => current === task.id ? null : current);
  };
  const openTask = (row: ScheduledTaskRow) => {
    setSelectedId(row.task.id);
    setEditors(previous => previous.some(entry => entry.editor.task?.id === row.task.id) ? previous : [...previous,
      { creating: false, editor: { id: threadEditors.find(editor => editor.task?.id === row.task.id)?.id ?? `scheduled:${row.task.id}`,
        owner: row.owner, task: row.task, open: true } }]);
  };
  useEffect(() => {
    setCreateId(null);
    if (!settingsIntent) { setSelectedId(null); return; }
    const row = { owner: taskOwner(settingsIntent.task), task: settingsIntent.task };
    acknowledgeTask(row.task, row.owner); openTask(row);
  }, [settingsIntent, navigationId]);
  const createTask = () => {
    const owner = currentOwner!;
    const id = `scheduled-draft:${JSON.stringify([owner.id, owner.workspaceRoot])}`;
    setEditors(previous => previous.some(entry => entry.editor.id === id) ? previous : [...previous,
      { creating: true, editor: { id, owner: { ...owner }, open: true } }]);
    setCreateId(id);
  };
  const runTaskAction = async (row: ScheduledTaskRow, action: "scheduler.run_now" | "scheduler.toggle" | "scheduler.remove") => {
    if (action === "scheduler.remove" && !await showConfirm({ title: tr("删除 {name}？", { name: row.task.name }),
      message: tr("这将永久删除定时任务并停止后续运行。"), confirmLabel: tr("删除定时任务"), danger: true })) return;
    setPendingTaskId(row.task.id);
    try {
      const target = { task_id: row.task.id, task_workspace_root: row.task.workspace_root,
        ...(row.owner.schedulerScope === "global" ? { scheduler_scope: "global" as const } : { owner_conversation_id: row.owner.id, workspace_root: row.owner.workspaceRoot }) };
      const command = action === "scheduler.toggle" ? { type: action, ...target, enabled: !row.task.enabled } : { type: action, ...target };
      const result = await sendClientCommandAwaitResult(command, action);
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("定时任务操作未完成。"));
      if (action === "scheduler.remove") forgetTask(row.task);
      else if (action === "scheduler.toggle") acknowledgeTask((result.data as unknown as { task: ScheduledTaskEntry }).task, row.owner);
      setReload(version => version + 1);
    } catch (error) { pushToast(errorMessage(error), "error"); }
    finally { setPendingTaskId(null); }
  };
  const visibleRows = rows.filter(({ task, owner }) => (filter === "all" || filter === "active" && task.enabled || filter === "paused" && !task.enabled || filter === "completed" && Boolean(task.last_run_at) && !task.next_run_at)
    && [task.name, task.prompt, workspaceDisplayName(owner.workspaceRoot), scheduleLabel(tr, task.schedule)].join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
    .sort((left, right) => Number(right.task.enabled) - Number(left.task.enabled)
      || (left.task.next_run_at ?? "9999").localeCompare(right.task.next_run_at ?? "9999") || left.task.name.localeCompare(right.task.name));
  const canCreate = Boolean(currentOwner?.workspaceRoot);

  return <div className="scheduled-tasks-page">
    <nav className="scheduled-tasks-sidebar" aria-label={tr("已安排任务文件夹")} hidden={!sidebarOpen} style={{ width: sidebarWidth }}>
      <div className="scheduled-sidebar-heading"><h1>{tr("定时任务")}</h1>
        <button type="button" className="mc-icon-button" aria-label={tr("搜索定时任务")} title={tr("搜索定时任务")} aria-expanded={searchOpen} onClick={() => setSearchOpen(open => !open)}><Search size={16} /></button>
      </div>
      {searchOpen && <label className="scheduled-task-search"><Search size={15} /><input ref={searchRef} value={query} aria-label={tr("搜索定时任务")} placeholder={tr("搜索定时任务")} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === "Escape") setSearchOpen(false); }} /></label>}
      <button type="button" className="scheduled-sidebar-create" title={canCreate ? tr("新建任务") : tr("请先打开项目任务。")} disabled={!canCreate} onClick={createTask}><Plus size={17} />{tr("新建任务")}</button>
      <div className="scheduled-task-group-heading"><span>{filter === "paused" || filter === "completed" ? labels(tr)[filter] : tr("即将执行")}</span>
        <button type="button" className="mc-icon-button" aria-label={tr("筛选任务")} title={tr("筛选任务")} aria-haspopup="menu" aria-expanded={Boolean(filterAnchor)} data-active={filter !== "all"} onClick={event => setFilterAnchor(event.currentTarget)}><SidebarSortSourceIcon kind="sort" /></button>
      </div>
      <div className="scheduled-task-list" role="list">
        {visibleRows.map(row => <div className="scheduled-task-row" role="listitem" key={row.task.id} data-selected={selectedId === row.task.id}>
          <button type="button" className="scheduled-task-select" aria-current={selectedId === row.task.id ? "page" : undefined} onClick={() => openTask(row)}>
            <span className="scheduled-task-name">{row.task.name}</span><span className="scheduled-task-summary">{!row.task.enabled && <span>{tr("已暂停")}</span>}{scheduleLabel(tr, row.task.schedule)}</span>
          </button>
          <button type="button" className="mc-icon-button scheduled-task-actions" aria-label={tr("已安排任务操作")} title={tr("已安排任务操作")} aria-haspopup="menu" aria-expanded={taskMenu?.row.task.id === row.task.id} disabled={Boolean(pendingTaskId)} onClick={event => setTaskMenu({ row, anchor: event.currentTarget })}><MoreHorizontal size={16} /></button>
        </div>)}
        {loading && <p className="scheduled-task-note" role="status">{tr("正在读取定时任务…")}</p>}
        {listError && <div className="scheduled-task-note" role="alert">{listError}<button type="button" onClick={() => setReload(version => version + 1)}>{tr("重试")}</button></div>}
        {!isConnected && <p className="scheduled-task-note" role="status">{tr("后端连接尚未就绪，恢复连接后将重新加载。")}</p>}
      </div>
    </nav>
    <main className="scheduled-tasks-main" aria-label={tr("定时任务")}>
      {!selectedId && <div className="scheduled-tasks-welcome"><span className="scheduled-tasks-clock" aria-hidden="true">🕗</span><h2>{tr("安排任务")}</h2><p>{tr("MiniCode 可以帮你处理持续性任务，让你不必亲自动手。")}</p><button type="button" className="scheduled-create-button" disabled={!canCreate} title={canCreate ? undefined : tr("请先打开项目任务。")} onClick={createTask}>{tr("新建任务")}</button></div>}
      {editors.filter(entry => !entry.creating).map(({ editor }) => <div className="scheduled-task-detail" key={editor.id} hidden={selectedId !== editor.task?.id}>
        <ThreadAutomationPanel editor={{ ...editor, task: threadEditors.find(item => item.task?.id === editor.task?.id)?.task ?? rows.find(row => row.task.id === editor.task?.id)?.task ?? editor.task }} inSettings active={active && selectedId === editor.task?.id}
          onTaskSaved={task => acknowledgeTask(task, editor.owner)} onTaskRemoved={forgetTask} />
        <ScheduledTaskHistory task={editor.task!} owner={editor.owner} recentRuns={runs} active={active && selectedId === editor.task?.id} />
      </div>)}
    </main>
    {editors.filter(entry => entry.creating).map(({ editor }) => <ScheduledTaskCreateDialog key={editor.id} editor={editor} open={active && createId === editor.id} onClose={() => setCreateId(null)}
      onSaved={(task, draftUnchanged) => {
        acknowledgeTask(task, taskOwner(task));
        setReload(version => version + 1);
        if (draftUnchanged) { setCreateId(null); setSelectedId(task.id); setEditors(previous => previous.filter(entry => entry.editor.id !== editor.id)); openTask({ task, owner: taskOwner(task) }); }
      }} />)}
    {filterAnchor && <ContextMenu anchor={filterAnchor} position={{ x: filterAnchor.getBoundingClientRect().right, y: filterAnchor.getBoundingClientRect().bottom }} ariaLabel={tr("筛选任务")} onClose={() => setFilterAnchor(null)} items={(Object.keys(labels(tr)) as StatusFilter[]).map(value => ({ label: labels(tr)[value], checked: filter === value, checkType: "radio", onClick: () => setFilter(value) }))} />}
    {taskMenu && <ContextMenu anchor={taskMenu.anchor} position={{ x: taskMenu.anchor.getBoundingClientRect().right, y: taskMenu.anchor.getBoundingClientRect().bottom }} ariaLabel={tr("已安排任务操作")} onClose={() => setTaskMenu(null)} items={[
      { label: tr("立即运行"), onClick: () => { void runTaskAction(taskMenu.row, "scheduler.run_now"); } },
      { label: taskMenu.row.task.enabled ? tr("暂停") : tr("恢复"), onClick: () => { void runTaskAction(taskMenu.row, "scheduler.toggle"); } },
      { label: tr("删除"), onClick: () => { void runTaskAction(taskMenu.row, "scheduler.remove"); } },
    ]} />}
  </div>;
}

function ScheduledTaskCreateDialog({ editor, open, onClose, onSaved }: {
  editor: ThreadAutomationEditor; open: boolean; onClose: () => void; onSaved: (task: ScheduledTaskEntry, draftUnchanged: boolean) => void;
}) {
  const { t: tr } = useI18n();
  const id = useId();
  const ref = useFocusTrap(open);
  return createPortal(<div className="mc-dialog-backdrop automation-dialog-backdrop" hidden={!open} style={{ display: open ? undefined : "none" }} onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div ref={ref} className="mc-dialog-panel automation-dialog scheduled-task-create-dialog" role="dialog" aria-modal="true" aria-labelledby={id} tabIndex={-1} onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); } }}>
      <div className="automation-dialog-heading"><h2 id={id}>{tr("设置定时任务")}</h2></div>
      <ThreadAutomationPanel editor={editor} variant="create-dialog" active={open} onClose={onClose} onTaskSaved={onSaved} />
      <button type="button" className="automation-dialog-close" aria-label={tr("关闭对话框")} onClick={onClose}><X size={14} /></button>
    </div>
  </div>, document.body);
}

function ScheduledTaskHistory({ task, owner, recentRuns, active }: {
  task: ScheduledTaskEntry; owner: ThreadAutomationOwner; recentRuns: ScheduledTaskRunEntry[]; active: boolean;
}) {
  const { t: tr, locale } = useI18n();
  const [page, setPage] = useState<{ runs: ScheduledTaskRunEntry[]; next_offset: number; has_more: boolean } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const runs = [...new Map([...(page?.runs ?? []), ...recentRuns.filter(run => run.task_id === task.id)].map(run => [run.id, run])).values()]
    .sort((left, right) => (right.started_at || right.scheduled_at).localeCompare(left.started_at || left.scheduled_at));
  const loadMore = async () => {
    setLoading(true); setError("");
    try {
      const result = await sendClientCommandAwaitResult({ type: "scheduler.history", task_id: task.id, task_workspace_root: task.workspace_root,
        ...(owner.schedulerScope === "global" ? { scheduler_scope: "global" as const } : { owner_conversation_id: owner.id, workspace_root: owner.workspaceRoot }),
        offset: page?.next_offset ?? runs.length, limit: 50 }, "scheduler.history");
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("无法读取定时任务。"));
      const next = result.data as unknown as NonNullable<typeof page>;
      setPage(previous => ({ ...next, runs: [...(previous?.runs ?? []), ...next.runs] }));
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setLoading(false); }
  };
  return <section className="scheduled-task-history" aria-label={tr("运行历史")}>
    {runs.map(run => <button type="button" key={run.id} disabled={!run.conversation_id} onClick={() => { closeAutomations(); useAppStore.getState().requestConversationSwitch(run.conversation_id!); }}>
      <span>{run.result_summary || run.status}</span><time dateTime={run.started_at || run.scheduled_at}>{new Date(run.started_at || run.scheduled_at).toLocaleString(locale, { timeZone: task.timezone, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</time>
    </button>)}
    {active && (!page || page.has_more) && <button type="button" className="scheduled-history-more" disabled={loading} onClick={() => { void loadMore(); }}>{loading ? tr("正在读取…") : tr("显示更多运行记录")}</button>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
