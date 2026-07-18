import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface ContextMenuItem {
  label: string;
  icon?: React.ReactNode;
  shortcut?: string;
  disabled?: boolean;
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

  // Dismiss on outside mousedown or Escape
  useEffect(() => {
    const handleMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose();
      }
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    // Use setTimeout so the current click event doesn't immediately fire
    const timer = window.setTimeout(() => {
      window.addEventListener("mousedown", handleMouseDown);
      window.addEventListener("keydown", handleKeyDown);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("mousedown", handleMouseDown);
      window.removeEventListener("keydown", handleKeyDown);
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
    ref.current?.focus();
  }, []);

  return createPortal(
    <div
      ref={ref}
      className="context-menu-surface"
      tabIndex={-1}
      role="menu"
      style={{
        position: "fixed",
        left: adjusted.x,
        top: adjusted.y,
        zIndex: "var(--z-context-menu)",
        minWidth: 180,
        maxWidth: 260,
        background: "var(--surface-raised, #2a2a2e)",
        border: "1px solid var(--border-subtle, rgba(255,255,255,0.08))",
        borderRadius: "var(--radius-md, 8px)",
        boxShadow: "0 6px 20px rgba(0,0,0,0.22), 0 1px 4px rgba(0,0,0,0.12)",
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
              background: "var(--border-subtle, rgba(255,255,255,0.06))",
              margin: "4px 8px",
            }}
          />
        ) : (
          <button
            key={i}
            role="menuitem"
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
                ? "var(--text-muted, #666)"
                : "var(--text-secondary, #ccc)",
              cursor: item.disabled ? "default" : "pointer",
              padding: "6px 12px",
              fontSize: "var(--text-sm, 13px)",
              lineHeight: 1.4,
              opacity: item.disabled ? 0.45 : 1,
            }}
            onMouseEnter={(e) => {
              if (!item.disabled) {
                (e.currentTarget as HTMLButtonElement).style.background =
                  "var(--surface-hover, rgba(255,255,255,0.06))";
              }
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLButtonElement).style.background = "transparent";
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
                className="shrink-0 font-mono"
                style={{
                  color: "var(--text-muted, #888)",
                  fontSize: "var(--text-xs, 11px)",
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
