import { useI18n } from "../i18n";
import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { RotateCcw } from "lucide-react";
import { ContextMenu } from "../components/ContextMenu";
import { useEscapeKey, useFocusTrap } from "../hooks/useFocusTrap";
import { canOpenThreadWindow, openThreadWindow } from "./threadWindowActions";
import { useWorkspaceOpenMenu } from "./useWorkspaceOpenMenu";
import type { ConversationMeta } from "../stores/types";
import { desktop, type DesktopPetState } from "../desktop/runtime";
import { pushToast } from "../overlays/ToastContainer";

// ── Small shared components ────────────────────────────────────────────

export const SectionTitle = ({ label }: { label: string }) => (
  <div style={{ color: "var(--text-secondary)", fontSize: "var(--text-sm)", fontWeight: "var(--fw-semibold)" }}>
    {label}
  </div>
);

export const IconAction = ({
  children,
  label,
  onClick,
  buttonRef,
  expanded,
  controls,
  disabled,
  hasPopup = true,
}: {
  children: React.ReactNode;
  label: string;
  onClick: React.MouseEventHandler<HTMLButtonElement>;
  buttonRef?: React.Ref<HTMLButtonElement>;
  expanded?: boolean;
  controls?: string;
  disabled?: boolean;
  hasPopup?: boolean;
}) => (
  <button
    ref={buttonRef}
    type="button"
    onClick={onClick}
    title={label}
    aria-label={label}
    aria-haspopup={hasPopup ? "menu" : undefined}
    aria-expanded={expanded}
    aria-controls={controls}
    disabled={disabled}
    className="btn-ghost mc-icon-button mc-icon-button-compact"
  >
    {children}
  </button>
);

export const MenuItem = ({
  icon,
  label,
  onClick,
  disabled,
  danger,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
}) => (
  <button
    type="button"
    role="menuitem"
    tabIndex={-1}
    className={danger ? "btn-ghost-danger" : "btn-ghost"}
    onClick={onClick}
    disabled={disabled}
    style={{
      width: "100%",
      display: "flex",
      alignItems: "center",
      gap: 8,
      padding: "6px 8px",
      border: 0,
      borderRadius: "var(--radius-sm, 4px)",
      color: disabled
        ? "var(--text-muted)"
        : danger
          ? "var(--state-danger)"
          : "var(--text-secondary)",
      cursor: disabled ? "not-allowed" : "pointer",
      fontSize: "var(--text-xs)",
      fontFamily: "var(--font-ui)",
      fontWeight: "var(--fw-medium)",
      lineHeight: 1.45,
      textAlign: "left",
      opacity: disabled ? 0.55 : 1,
      pointerEvents: disabled ? "none" : "auto",
    }}
  >
    {icon}
    <span>{label}</span>
  </button>
);

export const threadMenuIcon = (name: "copy" | "local" | "worktree" | "rename" | "archive" | "pin" | "unpin" | "read" | "project" | "pin-row" | "pin-row-filled" | "new-window") => <span aria-hidden="true" style={{
  width: 16, height: 16, background: "currentColor", mask: `url(${import.meta.env.BASE_URL}apps/thread-${name}.svg) center / contain no-repeat`,
}} />;

export const ConversationMenu = ({
  anchor, menuId, conversationId, workspaceRoot, conversationType, environment, archived, isIsolated, canCopy, canHandoff,
  pinned = false, hasUnreadTurn = false, currentProject = "", projectOptions = [], onPin, onReadState, onProjectChange,
  onRename, onCopy, onCopyLink, onCopyMarkdown, onHandoff, onArchive, onClose,
}: {
  anchor: HTMLElement | null;
  menuId: string;
  conversationId: string;
  workspaceRoot?: string;
  conversationType?: ConversationMeta["conversationType"];
  environment?: ConversationMeta["environment"];
  archived: boolean;
  isIsolated: boolean;
  canCopy: boolean;
  canHandoff: boolean;
  pinned?: boolean;
  hasUnreadTurn?: boolean;
  currentProject?: string;
  projectOptions?: { id: string; name: string }[];
  onPin: () => void;
  onReadState: () => void;
  onProjectChange: (projectId: string) => void;
  onRename: () => void;
  onCopy: () => void;
  onCopyLink: () => void;
  onCopyMarkdown: () => void;
  onHandoff: () => void;
  onArchive: () => void;
  onClose: () => void;
}) => {
  const { t: tr } = useI18n();
  const [miniMuted, setMiniMuted] = useState(false);
  useEffect(() => {
    const pets = desktop()?.pets;
    if (!pets) return;
    let mounted = true;
    const apply = (state: DesktopPetState) => { if (mounted) setMiniMuted(state.mutedConversationIds.includes(conversationId)); };
    const unsubscribe = pets.onStateChanged(apply);
    void pets.getState().then(apply).catch(error => { if (mounted) pushToast(String(error), "error"); });
    return () => { mounted = false; unsubscribe(); };
  }, [conversationId]);
  const workspaceItems = useWorkspaceOpenMenu(!environment || environment === "local" ? workspaceRoot : undefined, true);
  const windowItems = [
    ...(canOpenThreadWindow({ archived, conversationType, environment }) ? [{ label: tr("在新窗口中打开"), icon: threadMenuIcon("new-window"), onClick: () => void openThreadWindow(conversationId) }] : []),
    ...workspaceItems,
  ];
  useEffect(() => {
    document.getElementById(menuId)!.querySelector<HTMLButtonElement>('button[role="menuitem"]')!.focus();
  }, [menuId]);
  const bounds = anchor!.getBoundingClientRect();
  return <ContextMenu id={menuId} ariaLabel={tr("会话操作")} className="mc-conversation-menu" itemFontSize="var(--text-xs)"
    returnFocusTo={anchor!}
    position={{ x: bounds.right - 236, y: bounds.bottom + 6 }} onClose={onClose} items={[
      ...(!archived ? [
        { label: tr("重命名"), icon: threadMenuIcon("rename"), onClick: onRename },
        { label: tr(pinned ? "取消固定" : "固定"), icon: threadMenuIcon(pinned ? "unpin" : "pin"), onClick: onPin },
        { label: tr(hasUnreadTurn ? "标记为已读" : "标记为未读"), icon: threadMenuIcon("read"), onClick: onReadState },
        ...(projectOptions.some(project => project.id !== currentProject) || currentProject ? [{ label: tr("项目"), icon: threadMenuIcon("project"), submenu: [
          ...projectOptions.filter(project => project.id !== currentProject).map(project => ({ label: project.name, icon: threadMenuIcon("project"), onClick: () => onProjectChange(project.id) })),
          ...(currentProject ? [{ label: tr("移出项目"), icon: threadMenuIcon("project"), onClick: () => onProjectChange("") }] : []),
        ] }] : []),
        { label: "", separator: true },
      ] : []),
      { label: tr("复制"), icon: threadMenuIcon("copy"), submenu: [
        { label: tr("复制深度链接"), icon: threadMenuIcon("copy"), onClick: onCopyLink },
        { label: tr("复制为 Markdown"), icon: threadMenuIcon("copy"), onClick: onCopyMarkdown },
        { label: tr("复制工作目录"), icon: threadMenuIcon("copy"), disabled: !canCopy, onClick: onCopy },
      ] },
      ...(windowItems.length || canHandoff ? [{ label: "", separator: true }] : []),
      ...windowItems,
      ...(canHandoff ? [{ label: isIsolated ? tr("移到本地工作区") : tr("移到隔离工作区"), icon: threadMenuIcon(isIsolated ? "local" : "worktree"), onClick: onHandoff }] : []),
      ...(miniMuted ? [
        { label: "", separator: true },
        { label: tr("取消静音聊天"), onClick: () => { void desktop()!.pets.setNotificationMuted({ conversationId, muted: false }).catch(error => pushToast(String(error), "error")); } },
      ] : []),
      { label: "", separator: true },
      { label: archived ? tr("取消归档") : tr("归档"), icon: archived ? <RotateCcw size={14} /> : threadMenuIcon("archive"), onClick: onArchive },
    ]} />;
};

export type ConfirmDialogState = {
  title: string;
  message: string;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
} | null;

export const ConfirmDialog = ({
  dialog,
  onCancel,
  onConfirm,
}: {
  dialog: NonNullable<ConfirmDialogState>;
  onCancel: () => void;
  onConfirm: () => void;
}) => {
  const { t: tr } = useI18n();
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useFocusTrap(true);
  useEscapeKey(onCancel, true, dialogRef);
  useEffect(() => {
    cancelRef.current?.focus();
  }, []);
  return createPortal(
  <div
    role="presentation"
    onClick={onCancel}
    style={{
      position: "fixed",
      inset: 0,
      zIndex: "var(--z-dialog)",
      display: "grid",
      placeItems: "center",
      background: "var(--backdrop-strong)",
      padding: 16,
    }}
  >
    <div
      ref={dialogRef}
      role="dialog"
      tabIndex={-1}
      aria-modal="true"
      aria-label={dialog.title}
      onClick={(e) => e.stopPropagation()}
      style={{
        width: "min(360px, 100%)",
        background: "var(--surface-raised)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-md, 8px)",
        boxShadow: "var(--shadow-strong, var(--shadow-md))",
        padding: 14,
      }}
    >
      <div style={{ fontSize: "var(--text-md)", color: "var(--text-primary)", fontWeight: "var(--fw-bold)" }}>
        {dialog.title}
      </div>
      <div style={{ marginTop: 8, color: "var(--text-secondary)", fontSize: "var(--text-sm)", lineHeight: 1.45 }}>
        {dialog.message}
      </div>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
        <button ref={cancelRef} type="button" onClick={onCancel} style={dialogCancelStyle}>
          {tr("取消")}</button>
        <button
          type="button"
          onClick={onConfirm}
          style={{
            ...dialogConfirmStyle,
            background: dialog.danger ? "var(--state-danger)" : "var(--accent-primary)",
          }}
        >
          {dialog.confirmLabel}
        </button>
      </div>
    </div>
  </div>, document.body,
  );
};

// ── Dialog styles (kept local - only used here) ────────────────────────

const dialogCancelStyle: React.CSSProperties = {
  border: "1px solid var(--border-subtle)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "6px 10px",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
};

const dialogConfirmStyle: React.CSSProperties = {
  border: 0,
  color: "var(--text-on-accent)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "6px 10px",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  fontWeight: "var(--fw-bold)",
};
