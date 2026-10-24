import { commandResultSucceeded, LONG_COMMAND_RESULT_TIMEOUT_MS, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import type { ClientCommand } from "../protocol/events";
import { showConfirm } from "../overlays/DialogService";
import { pushToast } from "../overlays/ToastContainer";
import { translateApp as tr } from "../i18n";

export async function handoffConversationWorktree(id: string, taskName: string, target: "local" | "worktree") {
  const request = async (command: ClientCommand) => {
    const result = await sendClientCommandAwaitResult(command, command.type, { silent: true, timeoutMs: LONG_COMMAND_RESULT_TIMEOUT_MS });
    if (!commandResultSucceeded(result)) throw new Error(result.message);
    return result;
  };
  try {
    let result = await request({ type: "conversation.worktree.handoff.preflight", conversation_id: id, target });
    let checks = (result.data?.checks ?? []) as Array<{ code: string; severity: string; message: string }>;
    if (!result.data?.allowed && checks.some(check => check.code === "source.dirty" && check.severity === "blocking")) {
      if (!await showConfirm({ title: tr("检测到未提交改动"), message: tr("将已跟踪和未跟踪文件暂存，并在目标工作区恢复。发生冲突时暂存内容保留，可手动恢复。"), confirmLabel: tr("暂存后继续"), danger: true })) return;
      result = await request({ type: "conversation.worktree.handoff.preflight", conversation_id: id, target, dirty_action: "stash" });
      checks = (result.data?.checks ?? []) as Array<{ code: string; severity: string; message: string }>;
    }
    if (!result.data?.allowed) {
      pushToast(checks.filter(check => check.severity === "blocking").map(check => check.message).join("\n") || result.message, "warning", 6000);
      return;
    }
    if (!await showConfirm({
      title: target === "local" ? tr("将“{name}”移到本地检出？", { name: taskName }) : tr("将“{name}”移到隔离工作区？", { name: taskName }),
      message: [target === "local" ? tr("移除干净的隔离工作区，并把本地检出切换到此任务分支。") : tr("为此任务创建干净、隔离的 Worktree。"), ...checks.filter(check => check.severity === "warning").map(check => check.message)].join("\n\n"),
      confirmLabel: tr("移动任务"),
    })) return;
    const completed = await request({ type: "conversation.worktree.handoff.execute", conversation_id: id, target,
      fingerprint: result.data!.fingerprint as string, dirty_action: result.data!.dirty_action === "stash" ? "stash" : "block" });
    pushToast(completed.message, completed.data?.completed && completed.level !== "warning" ? "success" : "warning", 5000);
  } catch (error) {
    pushToast(error instanceof Error ? error.message : String(error), "error", 6000);
  }
}
