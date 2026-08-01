import React, { useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Archive,
  Copy,
  CopyPlus,
  Download,
  FolderOpen,
  GitBranch,
  GitMerge,
  LogIn,
  RotateCcw,
  Trash2,
  XCircle,
} from "lucide-react";

// ── Small shared components ────────────────────────────────────────────

export const SectionTitle = ({ label }: { label: string }) => (
  <div style={{ color: "var(--text-secondary)", fontSize: "var(--text-sm)", fontWeight: 650 }}>
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
}: {
  children: React.ReactNode;
  label: string;
  onClick: React.MouseEventHandler<HTMLButtonElement>;
  buttonRef?: React.Ref<HTMLButtonElement>;
  expanded?: boolean;
  controls?: string;
}) => (
  <button
    ref={buttonRef}
    type="button"
    onClick={onClick}
    title={label}
    aria-label={label}
    aria-haspopup="menu"
    aria-expanded={expanded}
    aria-controls={controls}
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
  anchor,
  menuId,
  archived,
  isIsolated,
  canDelete,
  canReveal,
  canCopy,
  canSwitch = true,
  canMerge = false,
  onSwitch,
  onReveal,
  onCopy,
  onCleanup,
  onHandoff,
  onArchive,
  onClone,
  onMerge,
  onExport,
  onDelete,
}: {
  anchor: HTMLElement | null;
  menuId: string;
  archived: boolean;
  isIsolated: boolean;
  canDelete: boolean;
  canReveal: boolean;
  canCopy: boolean;
  canSwitch?: boolean;
  canMerge?: boolean;
  onSwitch: () => void;
  onReveal: () => void;
  onCopy: () => void;
  onCleanup: () => void;
  onHandoff: () => void;
  onArchive: () => void;
  onClone: () => void;
  onMerge: () => void;
  onExport: () => void;
  onDelete: () => void;
}) => {
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [position, setPosition] = useState<{
    top: number;
    left: number;
    width: number;
    maxHeight: number;
  } | null>(null);

  useLayoutEffect(() => {
    if (!anchor) return;

    const placeMenu = () => {
      if (!anchor.isConnected) return;
      const rect = anchor.getBoundingClientRect();
      const viewportWidth = document.documentElement.clientWidth || window.innerWidth || 1024;
      const viewportHeight = document.documentElement.clientHeight || window.innerHeight || 768;
      const margin = 8;
      const gap = 6;
      const width = Math.min(236, Math.max(160, viewportWidth - margin * 2));
      const measuredHeight = menuRef.current?.scrollHeight || 320;
      const spaceBelow = Math.max(0, viewportHeight - rect.bottom - gap - margin);
      const spaceAbove = Math.max(0, rect.top - gap - margin);
      const openAbove = spaceBelow < Math.min(measuredHeight, 240) && spaceAbove > spaceBelow;
      const availableHeight = openAbove ? spaceAbove : spaceBelow;
      const maxHeight = Math.max(96, Math.min(measuredHeight, availableHeight, viewportHeight - margin * 2));
      const maxLeft = Math.max(margin, viewportWidth - margin - width);
      const left = Math.min(Math.max(margin, rect.right - width), maxLeft);
      const preferredTop = openAbove ? rect.top - gap - maxHeight : rect.bottom + gap;
      const top = Math.max(margin, Math.min(preferredTop, viewportHeight - margin - maxHeight));

      setPosition((current) => {
        const next = { top, left, width, maxHeight };
        if (
          current
          && current.top === next.top
          && current.left === next.left
          && current.width === next.width
          && current.maxHeight === next.maxHeight
        ) return current;
        return next;
      });
    };

    placeMenu();
    const frame = window.requestAnimationFrame(placeMenu);
    window.addEventListener("resize", placeMenu);
    window.addEventListener("scroll", placeMenu, true);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(placeMenu);
    observer?.observe(anchor);
    if (menuRef.current) observer?.observe(menuRef.current);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", placeMenu);
      window.removeEventListener("scroll", placeMenu, true);
      observer?.disconnect();
    };
  }, [anchor, archived, canCopy, canDelete, canMerge, canReveal, canSwitch, isIsolated]);

  if (typeof document === "undefined") return null;
  return createPortal(
    <div
      ref={menuRef}
      id={menuId}
      role="menu"
      aria-label="会话操作"
      onClick={(e) => e.stopPropagation()}
      style={{
        position: "fixed",
        left: position?.left ?? 0,
        top: position?.top ?? 0,
        zIndex: 1100,
        width: position?.width ?? 236,
        maxWidth: "calc(100vw - 16px)",
        maxHeight: position?.maxHeight ?? "calc(100vh - 16px)",
        overflowY: "auto",
        visibility: position ? "visible" : "hidden",
        padding: 5,
        background: "var(--surface-raised)",
        border: "1px solid var(--border-soft)",
        borderRadius: "var(--radius-sm, 6px)",
        boxShadow: "var(--shadow-strong, var(--shadow-md))",
      }}
    >
      {canSwitch && <MenuItem icon={<LogIn size={14} />} label="切换会话" onClick={onSwitch} />}
      <MenuItem icon={<CopyPlus size={14} />} label="克隆会话" onClick={onClone} />
      {canMerge && <MenuItem icon={<GitMerge size={14} />} label="合并到父会话" onClick={onMerge} />}
      <MenuItem icon={<Download size={14} />} label="导出会话树" onClick={onExport} />
      {canReveal && <MenuItem icon={<FolderOpen size={14} />} label="在资源管理器中显示" onClick={onReveal} />}
      {canCopy && <MenuItem icon={<Copy size={14} />} label="复制工作区路径" onClick={onCopy} />}
      <MenuItem
        icon={<GitBranch size={14} />}
        label={isIsolated ? "移到本地工作区" : "移到隔离工作区"}
        onClick={onHandoff}
      />
      {isIsolated && (
        <MenuItem icon={<XCircle size={14} />} label="清理工作区" onClick={onCleanup} />
      )}
      <MenuDivider />
      <MenuItem
        icon={archived ? <RotateCcw size={14} /> : <Archive size={14} />}
        label={archived ? "取消归档" : "归档"}
        onClick={onArchive}
      />
      {canDelete && <MenuItem danger icon={<Trash2 size={14} />} label="删除" onClick={onDelete} />}
    </div>,
    document.body,
  );
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
