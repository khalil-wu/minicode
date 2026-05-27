import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { sendClientCommand } from "../protocol/ws-outbox";

export const handleCommandResultEvent = (e: ServerEvent): boolean => {
  if (e.type !== "command.result") return false;

  const ev = e as unknown as {
    command?: string;
    message?: string;
    data?: {
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
  const s = useAppStore.getState();

  if (ev.command === "usage" && ev.data?.budget?.used != null && ev.data.budget.total != null) {
    const used = ev.data.budget.used;
    const total = ev.data.budget.total;
    const breakdown = ev.data.budget.breakdown;
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
