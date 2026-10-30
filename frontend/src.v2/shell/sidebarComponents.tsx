import { useI18n } from "../i18n";
import React, { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { useEscapeKey, useFocusTrap } from "../hooks/useFocusTrap";

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

export const threadMenuIcon = (name: "copy" | "local" | "worktree" | "rename" | "archive" | "pin" | "unpin" | "read" | "project" | "project-target" | "remove-project" | "section" | "project-remove" | "project-folder" | "mark-read-all" | "pin-row" | "pin-row-filled" | "new-window") => <span aria-hidden="true" style={{
  width: 16, height: 16, background: "currentColor", mask: `url(${import.meta.env.BASE_URL}apps/thread-${name}.svg) center / contain no-repeat`,
}} />;

export { ConversationMenu } from "./ConversationMenu";

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
