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
  const activeIndexRef = useRef(-1);

  const actionableIndexes = items
    .map((item, index) => (item.separator || item.disabled ? -1 : index))
    .filter((index) => index >= 0);

  const focusItemAt = useCallback((index: number) => {
    const menu = ref.current;
    if (!menu) return;
    const buttons = Array.from(menu.querySelectorAll<HTMLButtonElement>('button[role="menuitem"]:not([disabled])'));
    const next = buttons[index];
    if (next) {
      activeIndexRef.current = index;
      next.focus();
    }
  }, []);

  const moveActive = useCallback(
    (delta: 1 | -1) => {
      if (actionableIndexes.length === 0) return;
      const current = activeIndexRef.current;
      const currentPos = actionableIndexes.indexOf(current);
      const nextPos = currentPos < 0
        ? (delta === 1 ? 0 : actionableIndexes.length - 1)
        : (currentPos + delta + actionableIndexes.length) % actionableIndexes.length;
      focusItemAt(actionableIndexes[nextPos]);
    },
    [actionableIndexes, focusItemAt],
  );

  // Dismiss on outside mousedown or Escape; arrow/Home/End roving focus
  useEffect(() => {
    const handleMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose();
      }
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key === "ArrowDown") { e.preventDefault(); moveActive(1); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); moveActive(-1); return; }
      if (e.key === "Home") { e.preventDefault(); focusItemAt(actionableIndexes[0] ?? -1); return; }
      if (e.key === "End") { e.preventDefault(); focusItemAt(actionableIndexes[actionableIndexes.length - 1] ?? -1); return; }
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
  }, [onClose, moveActive, focusItemAt, actionableIndexes]);

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
            role="menuitem"
            aria-disabled={item.disabled || undefined}
            className="mc-menu-item"
            disabled={item.disabled}
            onClick={() => {
              item.onClick?.();
              onClose();
            }}
            onFocus={() => {
              const index = items.slice(0, i + 1).filter((it) => !it.separator && !it.disabled).length - 1;
              activeIndexRef.current = index;
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
                : "var(--text-secondary)",
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
