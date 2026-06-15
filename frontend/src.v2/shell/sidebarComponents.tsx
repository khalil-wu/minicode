import React from "react";
import {
  Archive,
  Copy,
  FolderOpen,
  LogIn,
  RotateCcw,
  Trash2,
  XCircle,
} from "lucide-react";

// ── Small shared components ────────────────────────────────────────────

export const SectionTitle = ({ label }: { label: string }) => (
  <div style={{ color: "var(--text-muted)", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0 }}>
    {label}
  </div>
);

export const IconAction = ({
  children,
  label,
  onClick,
}: {
  children: React.ReactNode;
  label: string;
  onClick: React.MouseEventHandler<HTMLButtonElement>;
}) => (
  <button
    onClick={onClick}
    title={label}
    aria-label={label}
    className="btn-ghost"
    style={{
      border: 0,
      color: "var(--text-muted)",
      cursor: "pointer",
      padding: "2px 4px",
      opacity: 0.7,
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center",
      borderRadius: "var(--radius-sm, 4px)",
    }}
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
      fontWeight: 500,
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

const MenuDivider = () => (
  <div style={{ height: 1, margin: "4px 2px", background: "var(--border-subtle)" }} />
);

export const ConversationMenu = ({
  archived,
  isIsolated,
  canDelete,
  canReveal,
  canCopy,
  onSwitch,
  onReveal,
  onCopy,
  onCleanup,
  onArchive,
  onDelete,
}: {
  archived: boolean;
  isIsolated: boolean;
  canDelete: boolean;
  canReveal: boolean;
  canCopy: boolean;
  onSwitch: () => void;
  onReveal: () => void;
  onCopy: () => void;
  onCleanup: () => void;
  onArchive: () => void;
  onDelete: () => void;
}) => (
  <div
    onClick={(e) => e.stopPropagation()}
    style={{
      position: "absolute",
      right: 8,
      top: 34,
      zIndex: 20,
      minWidth: 204,
      padding: 5,
      background: "var(--surface-page)",
      border: "1px solid var(--border-soft)",
      borderRadius: "var(--radius-sm, 6px)",
      boxShadow: "var(--shadow-soft)",
    }}
  >
    <MenuItem icon={<LogIn size={13} />} label="Switch session" onClick={onSwitch} />
    {canReveal && <MenuItem icon={<FolderOpen size={13} />} label="Reveal workspace" onClick={onReveal} />}
    {canCopy && <MenuItem icon={<Copy size={13} />} label="Copy workspace path" onClick={onCopy} />}
    {isIsolated && (
      <MenuItem icon={<XCircle size={13} />} label="Clean up workspace" onClick={onCleanup} />
    )}
    <MenuDivider />
    <MenuItem
      icon={archived ? <RotateCcw size={13} /> : <Archive size={13} />}
      label={archived ? "Unarchive" : "Archive"}
      onClick={onArchive}
    />
    {canDelete && <MenuItem danger icon={<Trash2 size={13} />} label="Delete" onClick={onDelete} />}
  </div>
);

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
}) => (
  <div
    role="presentation"
    onClick={onCancel}
    style={{
      position: "fixed",
      inset: 0,
      zIndex: 1200,
      display: "grid",
      placeItems: "center",
      background: "var(--backdrop-strong)",
      padding: 16,
    }}
  >
    <div
      role="dialog"
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
      <div style={{ fontSize: "var(--text-md)", color: "var(--text-primary)", fontWeight: 700 }}>
        {dialog.title}
      </div>
      <div style={{ marginTop: 8, color: "var(--text-secondary)", fontSize: "var(--text-sm)", lineHeight: 1.45 }}>
        {dialog.message}
      </div>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 16 }}>
        <button type="button" onClick={onCancel} style={dialogCancelStyle}>
          Cancel
        </button>
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
  </div>
);

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
  fontWeight: 700,
};
