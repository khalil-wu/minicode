import { useAppStore } from "../stores";
import type { CommandResultEvent, ServerEvent } from "../protocol/events";
import * as wsOutbox from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { openAutomations } from "../lib/automations-navigation";
import { openSettings } from "../lib/settings-navigation";
import type { PanelKind, RightStackTab, WorkspaceSlice } from "../stores/types";
import { workspaceRootsEqual } from "../lib/workspace-path";

export const downloadConversationExport = (
  filename: string,
  content: string,
  mimeType = "application/json;charset=utf-8",
): boolean => {
  if (typeof document === "undefined" || typeof URL === "undefined" || typeof URL.createObjectURL !== "function") return false;
  const safeFilename = (filename || "minicode-conversation.json")
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_")
    .slice(0, 180);
  const url = URL.createObjectURL(new Blob([content], { type: mimeType }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = safeFilename || "minicode-conversation.json";
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
  return true;
};

const commandResultStatus = (level?: string): "completed" | "failed" => {
  const normalized = String(level || "").toLowerCase();
  return normalized === "error" || normalized === "failed" ? "failed" : "completed";
};

const toastType = (level?: string): "info" | "success" | "error" => {
  const normalized = String(level || "").toLowerCase();
  if (normalized === "error" || normalized === "failed") return "error";
  if (normalized === "warning") return "info";
  return "info";
};

// Inspect-type slash command results (/usage, /status, /help, /memory, ...) are
// transient feedback, not conversation content. Surface them as an ephemeral
// toast plus a compact activity-trace entry — never as a persistent transcript
// message. (Meaningful persistent notices — context compaction, system_notice,
// guidelines — flow through other paths and are unaffected.)
const surfaceCommandResult = (ev: CommandResultEvent) => {
  const command = String(ev.command || "command").trim() || "command";
  if (command === "subagent.status") return;
  const message = String(ev.message || "").trim();
  if (!message) return;
  const title = ev.title ? String(ev.title).trim() : `/${command.replace(/^\//, "")}`;
  const level = String(ev.level || "").toLowerCase();
  const duration = level === "error" || level === "failed" || level === "warning" ? 7000 : 5000;

  pushToast(`${title} — ${message}`, toastType(ev.level), duration);

  useAppStore.getState().appendAgentProgress({
    id: `command-result-${command}`,
    stage: "status",
    phase: "status",
    status: commandResultStatus(ev.level),
    message,
    label: title,
    summary: message,
    visibility: "compact",
  });
};

const isUserInvokedCommandResult = (command: string): boolean => {
  const normalized = String(command || "").trim();
  // Slash/plugin commands use their user-facing name (for example "usage"
  // or "agent-ui"). Dotted names are control-plane operations owned by a
  // settings panel, menu, or other caller and must never be written into the
  // conversation process area.
  return Boolean(normalized) && !normalized.includes(".");
};

const RIGHT_STACK_TABS = new Set<RightStackTab>([
  "preview",
  "browser",
  "tasks",
  "diff",
  "plan",
  "subagents",
  "artifacts",
  "inspector",
  "diagnostics",
]);

const DOCK_TABS = new Set<WorkspaceSlice["activeBottomTab"]>([
  "terminal",
  "git",
  "tasks",
  "timeline",
  "debug",
  "budget",
]);

const PANEL_KINDS = new Set<PanelKind>([
  "chat",
  "diff",
  "editor",
  "preview",
  "terminal",
  "plan",
  "tasks",
  "subagents",
  "artifacts",
  "inspector",
]);

const SETTINGS_TABS = new Map<string, Parameters<typeof openSettings>[0]>([
  ["general", "general"],
  ["appearance", "appearance"],
  ["personalization", "personalization"],
  ["shortcuts", "shortcuts"],
  ["keyboard", "shortcuts"],
  ["provider", "provider"],
  ["model", "provider"],
  ["plugins", "plugins"],
  ["skills", "skills"],
  ["connectors", "connectors"],
  ["mcp", "connectors"],
  ["browser", "browser"],
  ["scheduler", "scheduler"],
  ["automations", "scheduler"],
  ["workspacegit", "workspaceGit"],
  ["workspace-git", "workspaceGit"],
  ["git", "workspaceGit"],
  ["features", "features"],
  ["advanced", "advanced"],
  ["archived", "archived"],
]);

const splitUiAction = (action: string): [string, string] => {
  const [name, ...rest] = action.split(":");
  return [name.trim().toLowerCase(), rest.join(":").trim().toLowerCase()];
};

const titleCase = (value: string): string => value.charAt(0).toUpperCase() + value.slice(1);

const openRightStack = (tab: RightStackTab) => {
  const state = useAppStore.getState();
  state.setAppMode("code");
  state.setRightStackTab(tab);
};

const openDock = (tab: WorkspaceSlice["activeBottomTab"]) => {
  useAppStore.getState().openBottomTab(tab);
};

const openPanel = (kind: PanelKind) => {
  const state = useAppStore.getState();
  const existing = state.panelSlots.find((slot) => slot.kind === kind);
  if (existing) {
    state.focusPanel(existing.id);
    return;
  }
  state.addPanel({ id: `${kind}-${Date.now()}`, kind, label: titleCase(kind) });
};

const handleUiAction = (
  data?: CommandResultEvent["data"] & { ui_action?: string; tab?: string; panel?: string; component?: string },
): boolean => {
  const rawAction = String(data?.ui_action || "").trim();
  if (!rawAction) return false;

  const state = useAppStore.getState();
  const [action, suffix] = splitUiAction(rawAction);
  const capabilities = state.runtimeCapabilities;

  if (action === "open_skills_marketplace") {
    if (!state.skillsMarketplaceOpen) state.toggleSkillsMarketplace();
    wsOutbox.sendClientCommand({ type: "skills.list" });
    wsOutbox.sendClientCommand({ type: "skills.marketplace.list" });
    return true;
  }

  if (action === "open_settings") {
    const tab = (suffix || String(data?.tab || "").trim().toLowerCase());
    openSettings(SETTINGS_TABS.get(tab));
    return true;
  }

  if (action === "open_quick_open") {
    if (!capabilityFeatureEnabled(capabilities, "global_search", true)) return true;
    if (!state.quickOpenVisible) state.toggleQuickOpen();
    return true;
  }

  if (action === "open_agent_editor") {
    if (!capabilityFeatureEnabled(capabilities, "agent_editor", true)) return true;
    if (!state.agentEditorOpen) state.toggleAgentEditor();
    return true;
  }

  if (action === "open_automations") {
    openAutomations();
    return true;
  }

  if (action === "open_live_artifacts") {
    if (!state.liveArtifactsOpen) state.toggleLiveArtifacts();
    return true;
  }

  if (action === "open_right_stack" && RIGHT_STACK_TABS.has(suffix as RightStackTab)) {
    openRightStack(suffix as RightStackTab);
    return true;
  }

  if (action === "open_right_stack" && suffix === "terminal") {
    openDock("terminal");
    return true;
  }

  if (action === "open_dock" && DOCK_TABS.has(suffix as WorkspaceSlice["activeBottomTab"])) {
    openDock(suffix as WorkspaceSlice["activeBottomTab"]);
    return true;
  }

  const panel = suffix || String(data?.panel || "").trim().toLowerCase();
  if (action === "open_panel" && PANEL_KINDS.has(panel as PanelKind)) {
    openPanel(panel as PanelKind);
    return true;
  }

  return false;
};

export const handleCommandResultEvent = (e: ServerEvent): boolean => {
  if (e.type !== "command.result") return false;

  const ev = e as CommandResultEvent & {
    data?: CommandResultEvent["data"] & {
      conversation_id?: string;
      needs_force?: boolean;
      removed?: boolean;
      error?: string;
      ui_action?: string;
      filename?: string;
      mime_type?: string;
      content?: string;
      budget?: {
        used?: number;
        total?: number;
        breakdown?: Record<string, number>;
      };
    };
  };

  // Resolve the owning operation before doing any generic presentation. A
  // settings page waiting on this result owns its inline state/toast; surfacing
  // the same MCP/plugin/scheduler result as agent progress mixes control-plane
  // UI into the conversation and produces duplicate feedback.
  const consumedByCaller = wsOutbox.resolveClientCommandResult(ev);
  const envelope = ev as CommandResultEvent & { conversation_id?: string; workspace_root?: string };
  const owner = typeof ev.data?.conversation_id === "string" ? ev.data.conversation_id : envelope.conversation_id;
  const workspaceRoot = typeof ev.data?.workspace_root === "string" ? ev.data.workspace_root : envelope.workspace_root;
  if (
    ev.command === "conversation.switch"
    && ev.level !== "success"
    && owner === useAppStore.getState().pendingConversationSwitchId
  ) {
    useAppStore.setState({ pendingConversationSwitchId: null });
    if (!consumedByCaller) pushToast(ev.message, toastType(ev.level), 7000);
  }
  const targetsActiveScope = () => owner !== undefined && typeof workspaceRoot === "string"
    && owner === (useAppStore.getState().conversationId || "")
    && workspaceRootsEqual(workspaceRoot, useAppStore.getState().workingDirectory);

  if (commandResultStatus(ev.level) === "failed") {
    const state = useAppStore.getState();
    const requestId = ev.command === "diff.git_working_tree"
      ? state.gitChanges.workingTreeRequestId
      : ev.command === "diff.git_staged" ? state.gitChanges.stagedRequestId : undefined;
    if (requestId && e.client_command_id === requestId) {
      state.setGitChanges({ loading: false, error: ev.message || "无法加载 Git 更改" });
      if (!consumedByCaller) pushToast(ev.message || "无法加载 Git 更改", "error", 5000);
    }
  }

  if (
    ev.command === "usage" &&
    ev.data?.budget?.used != null &&
    ev.data.budget.total != null &&
    targetsActiveScope()
  ) {
    const used = ev.data.budget.used;
    const total = ev.data.budget.total;
    const breakdown = ev.data.budget.breakdown;
    const s = useAppStore.getState();
    const currentUsage = useAppStore.getState().contextUsage;
    const buckets = breakdown
      ? Object.entries(breakdown).map(([name, tokens]) => ({ name, used: tokens, limit: 0 }))
      : [];
    s.setBudget(buckets, total > 0 ? used / total : 0);
    s.setContextUsage({
      used,
      limit: total,
      compactedAt: currentUsage?.compactedAt,
      compactSummary: currentUsage?.compactSummary,
    });
  }

  if (targetsActiveScope()) handleUiAction(ev.data);

  if (ev.command === "send_message" && typeof ev.data?.message_id === "string") {
    const messageId = ev.data.message_id;
    const recipient = typeof ev.data.recipient === "string" ? ev.data.recipient : "";
    const deliveryStatus = commandResultStatus(ev.level) === "failed" ? "failed" : "sent";
    const state = useAppStore.getState();
    const ownedAgents = owner === state.conversationId
      ? state.subagents : owner ? state.conversationAgentStates[owner]?.subagents ?? [] : [];
    const target = ownedAgents.find((subagent) =>
      (recipient && subagent.id === recipient)
      || subagent.messages?.some((message) => message.messageId === messageId)
    );
    if (target) {
      state.updateSubagent(target.id, {
        messages: (target.messages ?? []).map((message) =>
          message.messageId === messageId ? { ...message, deliveryStatus } : message,
        ),
      }, owner || undefined);
    }
  }

  if (
    ev.command === "conversation.export" &&
    typeof ev.data?.content === "string" &&
    typeof ev.data?.filename === "string"
  ) {
    if (!downloadConversationExport(ev.data.filename, ev.data.content, ev.data.mime_type)) {
    pushToast("导出文件已生成，但当前环境无法开始下载。", "error", 5000);
    }
  }

  if (!consumedByCaller && targetsActiveScope() && isUserInvokedCommandResult(ev.command)) {
    surfaceCommandResult(ev);
  }

  if (ev.command === "conversation.worktree.cleanup") {
    if (targetsActiveScope() && ev.data?.needs_force && ev.data.conversation_id) {
      const convId = ev.data.conversation_id;
    const msg = ev.message || ev.data.error || "Worktree 中存在本地更改。";
      import("../overlays/DialogService").then(({ showConfirm }) =>
        showConfirm({
      title: "清理 Worktree",
          message: `${msg}\n\nForce cleanup and discard local changes?`,
      confirmLabel: "强制清理",
          danger: true,
        }).then((ok) => {
          if (ok && targetsActiveScope()) {
            wsOutbox.sendClientCommand({
              type: "conversation.worktree.cleanup",
              conversation_id: convId,
              force: true,
            });
          }
        }),
      );
    }
    if (ev.data?.removed && ev.data.conversation_id) {
      const workspaceRoot = typeof ev.data.workspace_root === "string" ? ev.data.workspace_root : "";
      useAppStore.setState((state) => ({
        conversations: state.conversations.map((conversation) =>
          conversation.id === ev.data?.conversation_id
            ? {
                ...conversation,
                gitIsolated: false,
                worktreePath: undefined,
                gitBranch: undefined,
                ...(workspaceRoot ? { workspaceRoot } : {}),
              }
            : conversation,
        ),
      }));
      const state = useAppStore.getState();
      if (state.conversationId === ev.data.conversation_id && workspaceRoot) state.setWorkingDirectory(workspaceRoot);
    }
  }

  if (ev.command === "conversation.worktree.handoff.preflight") {
    const conversationId = typeof ev.data?.conversation_id === "string" ? ev.data.conversation_id : "";
    const target = ev.data?.target === "local" ? "local" : "worktree";
    const fingerprint = typeof ev.data?.fingerprint === "string" ? ev.data.fingerprint : "";
    const checks = Array.isArray(ev.data?.checks) ? ev.data.checks as Array<{ severity?: string; message?: string }> : [];
    const dirty = checks.some((check) => check.severity === "blocking" && /local changes|source\.dirty/i.test(String(check.message || "")));
    if (targetsActiveScope() && !ev.data?.allowed && dirty && conversationId) {
      import("../overlays/DialogService").then(({ showConfirm }) => showConfirm({
        title: "检测到未提交改动",
        message: "可将已跟踪和未跟踪文件暂存，并在目标工作区恢复。发生冲突时暂存内容会保留，可手动恢复。",
        confirmLabel: "暂存后继续",
        danger: true,
      }).then((ok) => {
        if (ok && targetsActiveScope()) wsOutbox.sendClientCommand({
          type: "conversation.worktree.handoff.preflight",
          conversation_id: conversationId,
          target,
          dirty_action: "stash",
        });
      }));
    }
    if (targetsActiveScope() && ev.data?.allowed && conversationId && fingerprint) {
      const warnings = checks.filter((check) => check.severity === "warning").map((check) => check.message).filter(Boolean);
      import("../overlays/DialogService").then(({ showConfirm }) => showConfirm({
      title: target === "local" ? "将任务移到本地检出？" : "将任务移到受保护工作区？",
        message: [
          target === "local"
        ? "MiniCode 将移除干净的受保护工作区，并把本地检出切换到此任务分支。"
        : "MiniCode 将为此任务创建干净、隔离的 Worktree。",
          ...warnings,
        ].join("\n\n"),
      confirmLabel: "移动任务",
      }).then((ok) => {
        if (ok && targetsActiveScope()) wsOutbox.sendClientCommand({
          type: "conversation.worktree.handoff.execute",
          conversation_id: conversationId,
          target,
          fingerprint,
          dirty_action: ev.data?.dirty_action === "stash" ? "stash" : "block",
        });
      }));
    }
  }

  if (ev.command === "conversation.worktree.handoff.execute" && ev.data?.completed && ev.data.conversation_id) {
    const conversationId = String(ev.data.conversation_id);
    useAppStore.setState((state) => ({
      conversations: state.conversations.map((conversation) => conversation.id === conversationId ? {
        ...conversation,
        workspaceRoot: String(ev.data?.workspace_root || conversation.workspaceRoot || ""),
        worktreePath: String(ev.data?.worktree_path || "") || undefined,
        gitBranch: String(ev.data?.git_branch || "") || undefined,
        gitIsolated: Boolean(ev.data?.git_isolated),
      } : conversation),
    }));
    const state = useAppStore.getState();
    if (state.conversationId === conversationId && ev.data.workspace_root) {
      state.setWorkingDirectory(String(ev.data.workspace_root));
    }
  }

  return true;
};
