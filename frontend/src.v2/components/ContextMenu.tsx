import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { createPortal } from "react-dom";

export interface ContextMenuItem {
  label: string;
  icon?: React.ReactNode;
  shortcut?: string;
  disabled?: boolean;
  danger?: boolean;
  separator?: boolean;
  onClick?: () => void;
}

interface ContextMenuProps {
  items: ContextMenuItem[];
  position: { x: number; y: number };
  onClose: () => void;
}

export const ContextMenu = ({ items, position, onClose }: ContextMenuProps) => {
  const ref = useRef<HTMLDivElement>(null);
  const [adjusted, setAdjusted] = useState(position);
  const triggerRef = useRef<HTMLElement | null>(null);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape" || event.key === "Tab") {
      event.stopPropagation();
      if (event.key === "Escape") event.preventDefault();
      triggerRef.current?.focus();
      onClose();
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button[role="menuitem"]:not(:disabled)'));
    const current = buttons.findIndex((button) => button === document.activeElement);
    const next = event.key === "Home" ? 0
      : event.key === "End" ? buttons.length - 1
      : current < 0 ? (event.key === "ArrowDown" ? 0 : buttons.length - 1)
      : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next]?.focus();
  };

  // Dismiss on outside mousedown or Escape; arrow/Home/End roving focus
  useEffect(() => {
    const handleMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose();
      }
    };
    // Use setTimeout so the current click event doesn't immediately fire
    const timer = window.setTimeout(() => {
      window.addEventListener("mousedown", handleMouseDown);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("mousedown", handleMouseDown);
    };
  }, [onClose]);

  // Adjust position to stay within viewport
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let { x, y } = position;
    if (x + rect.width > vw - 8) x = Math.max(8, vw - rect.width - 8);
    if (y + rect.height > vh - 8) y = Math.max(8, vh - rect.height - 8);
    setAdjusted({ x, y });
  }, [position]);

  // Focus the menu so keyboard events work
  useEffect(() => {
    triggerRef.current = document.activeElement as HTMLElement;
    ref.current?.focus();
  }, []);

  return createPortal(
    <div
      ref={ref}
      className="context-menu-surface"
      tabIndex={-1}
      role="menu"
      onKeyDown={handleKeyDown}
      style={{
        position: "fixed",
        left: adjusted.x,
        top: adjusted.y,
        zIndex: "var(--z-context-menu)",
        minWidth: 180,
        maxWidth: 260,
        background: "var(--surface-raised)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-md)",
        boxShadow: "var(--shadow-strong-overlay)",
        padding: "4px 0",
        outline: "none",
      }}
    >
      {items.map((item, i) =>
        item.separator ? (
          <div
            key={i}
            role="separator"
            style={{
              height: 1,
              background: "var(--border-subtle)",
              margin: "4px 8px",
            }}
          />
        ) : (
          <button
            key={i}
            type="button"
            tabIndex={-1}
            role="menuitem"
            aria-disabled={item.disabled || undefined}
            className="mc-menu-item"
            disabled={item.disabled}
            onClick={() => {
              item.onClick?.();
              onClose();
            }}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              width: "100%",
              textAlign: "left",
              background: "transparent",
              border: 0,
              color: item.disabled
                ? "var(--text-muted)"
                : item.danger ? "var(--state-danger)" : "var(--text-secondary)",
              cursor: item.disabled ? "default" : "pointer",
              padding: "6px 12px",
              fontSize: "var(--text-sm, 13px)",
              lineHeight: 1.4,
              opacity: item.disabled ? 0.45 : 1,
            }}
          >
            {item.icon && (
              <span className="shrink-0" style={{ display: "inline-flex", width: 16, justifyContent: "center" }}>
                {item.icon}
              </span>
            )}
            <span className="flex-fill truncate">
              {item.label}
            </span>
            {item.shortcut && (
              <span
                className="shrink-0 mc-kbd"
                style={{
                  color: "var(--text-muted)",
                  marginLeft: 12,
                }}
              >
                {item.shortcut}
              </span>
            )}
          </button>
        ),
      )}
    </div>,
    document.body,
  );
};
