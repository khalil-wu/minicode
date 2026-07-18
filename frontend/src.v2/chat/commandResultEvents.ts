import { useAppStore } from "../stores";
import type { CommandResultEvent, ServerEvent } from "../protocol/events";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import { openAutomations } from "../lib/automations-navigation";
import { openSettings } from "../lib/settings-navigation";
import type { PanelKind, RightStackTab, WorkspaceSlice } from "../stores/types";

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
  if (command === "subagent.resume") {
    const failed = commandResultStatus(ev.level) === "failed";
    pushToast(failed ? "任务未能继续，请查看详情" : "任务已继续", failed ? "error" : "success", 4000);
    return;
  }
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

const commandResultTargetsActiveConversation = (conversationId?: string): boolean => {
  if (!conversationId) return true;
  return useAppStore.getState().conversationId === conversationId;
};

const RIGHT_STACK_TABS = new Set<RightStackTab>([
  "preview",
  "terminal",
  "tasks",
  "plan",
  "subagents",
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
  "inspector",
]);

const PLUGIN_COMPONENTS = new Set(["prompt-form"]);

const SETTINGS_TABS = new Set([
  "general",
  "provider",
  "connectors",
  "scheduler",
  "features",
  "plugins",
  "advanced",
  "diagnostics",
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
  useAppStore.getState().setActiveBottomTab(tab);
  useAppStore.setState({ dockCollapsed: false });
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

  if (action === "open_plugin_component") {
    if (!capabilityFeatureEnabled(capabilities, "plugin_local_jsx_commands", true)) return true;
    const component = String(data?.component || "").trim();
    if (PLUGIN_COMPONENTS.has(component)) {
      state.openPluginCommandPanel({ ...(data ?? {}), component });
    } else if (component) {
      pushToast(`Unsupported plugin component: ${component}`, "warning");
    }
    return true;
  }

  if (action === "open_skills_marketplace") {
    if (!state.skillsMarketplaceOpen) state.toggleSkillsMarketplace();
    sendClientCommand({ type: "skills.list" });
    sendClientCommand({ type: "skills.marketplace.list" });
    return true;
  }

  if (action === "open_settings") {
    const tab = (suffix || String(data?.tab || "").trim().toLowerCase());
    openSettings(SETTINGS_TABS.has(tab) ? tab as Parameters<typeof openSettings>[0] : undefined);
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
      budget?: {
        used?: number;
        total?: number;
        breakdown?: Record<string, number>;
      };
    };
  };

  if (
    ev.command === "usage" &&
    ev.data?.budget?.used != null &&
    ev.data.budget.total != null &&
    commandResultTargetsActiveConversation(ev.data.conversation_id)
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

  handleUiAction(ev.data);

  surfaceCommandResult(ev);

  if (ev.command === "conversation.worktree.cleanup") {
    if (ev.data?.needs_force && ev.data.conversation_id) {
      const convId = ev.data.conversation_id;
      const msg = ev.message || ev.data.error || "Worktree has local changes.";
      import("../overlays/DialogService").then(({ showConfirm }) =>
        showConfirm({
          title: "Worktree cleanup",
          message: `${msg}\n\nForce cleanup and discard local changes?`,
          confirmLabel: "Force cleanup",
          danger: true,
        }).then((ok) => {
          if (ok) {
            sendClientCommand({
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
        ...(state.conversationId === ev.data?.conversation_id && workspaceRoot
          ? { workingDirectory: workspaceRoot }
          : {}),
      }));
    }
  }

  if (ev.command === "conversation.worktree.handoff.preflight") {
    const conversationId = typeof ev.data?.conversation_id === "string" ? ev.data.conversation_id : "";
    const target = ev.data?.target === "local" ? "local" : "worktree";
    const fingerprint = typeof ev.data?.fingerprint === "string" ? ev.data.fingerprint : "";
    const checks = Array.isArray(ev.data?.checks) ? ev.data.checks as Array<{ severity?: string; message?: string }> : [];
    if (ev.data?.allowed && conversationId && fingerprint) {
      const warnings = checks.filter((check) => check.severity === "warning").map((check) => check.message).filter(Boolean);
      import("../overlays/DialogService").then(({ showConfirm }) => showConfirm({
        title: target === "local" ? "Move task to local checkout?" : "Move task to protected workspace?",
        message: [
          target === "local"
            ? "MiniCode will remove the clean protected workspace and switch the local checkout to this task branch."
            : "MiniCode will create a clean isolated worktree for this task.",
          ...warnings,
        ].join("\n\n"),
        confirmLabel: "Move task",
      }).then((ok) => {
        if (ok) sendClientCommand({
          type: "conversation.worktree.handoff.execute",
          conversation_id: conversationId,
          target,
          fingerprint,
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
      ...(state.conversationId === conversationId && ev.data?.workspace_root
        ? { workingDirectory: String(ev.data.workspace_root) }
        : {}),
    }));
  }

  return true;
};
