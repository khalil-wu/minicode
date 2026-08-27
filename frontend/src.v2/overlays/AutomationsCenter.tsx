import { useEffect } from "react";
import { X } from "lucide-react";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { SchedulerTab } from "./SchedulerTab";
import { backdropStyle, closeBtn, contentStyle, headerStyle, modalStyle } from "./settingsShared";

export const AutomationsCenter = () => {
  const automationsOpen = useAppStore((s) => s.automationsOpen);
  const toggleAutomations = useAppStore((s) => s.toggleAutomations);
  const conversationId = useAppStore((s) => s.conversationId);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const dialogRef = useFocusTrap(automationsOpen);

  useEffect(() => {
    if (automationsOpen) sendClientCommand({
      type: "scheduler.list",
      owner_conversation_id: conversationId ?? undefined,
      workspace_root: workingDirectory || undefined,
    });
  }, [automationsOpen, conversationId, workingDirectory]);

  if (!automationsOpen) return null;

  return (
    <div className="overlay-backdrop" onClick={toggleAutomations} style={backdropStyle}>
      <div
        ref={dialogRef}
        className="modal-content automations-center"
        role="dialog"
        aria-modal="true"
        aria-label="自动任务"
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            toggleAutomations();
          }
        }}
        style={{ ...modalStyle, width: "min(820px, 94vw)", height: "min(640px, 88vh)" }}
      >
        <div style={headerStyle}>
          <h2 style={{ margin: 0, fontSize: "var(--text-lg)", color: "var(--text-primary)", fontWeight: "var(--fw-bold)" }}>自动任务</h2>
          <button type="button" className="automations-close" onClick={toggleAutomations} style={closeBtn} aria-label="关闭自动任务"><X size={16} /></button>
        </div>

        <div style={contentStyle}>
          <SchedulerTab title="运行记录" />
        </div>
      </div>
    </div>
  );
};

