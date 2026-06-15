import { useAppStore } from "../stores";
import type { CommandResultEvent, ServerEvent } from "../protocol/events";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";

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

const commandResultTargetsActiveConversation = (conversationId?: string): boolean => {
  if (!conversationId) return true;
  return useAppStore.getState().conversationId === conversationId;
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

  if (ev.data?.ui_action === "open_skills_marketplace") {
    const state = useAppStore.getState();
    if (!state.skillsMarketplaceOpen) state.toggleSkillsMarketplace();
    sendClientCommand({ type: "skills.list" });
    sendClientCommand({ type: "skills.marketplace.list" });
  }

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
      useAppStore.setState((state) => ({
        conversations: state.conversations.map((conversation) =>
          conversation.id === ev.data?.conversation_id
            ? { ...conversation, gitIsolated: false, worktreePath: undefined }
            : conversation,
        ),
      }));
    }
  }

  return true;
};
