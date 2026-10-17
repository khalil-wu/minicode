import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { ChevronRight } from "../lib/icons";
import { SidebarSortSourceIcon } from "../lib/sidebar-sort-icons";
import { Tip } from "./Tooltip";

export interface ContextMenuItem {
  label: string;
  icon?: React.ReactNode;
  rightIcon?: React.ReactNode;
  rightIconTooltip?: string;
  shortcut?: string;
  disabled?: boolean;
  danger?: boolean;
  separator?: boolean;
  onClick?: () => void;
  submenu?: ContextMenuItem[];
  submenuMaxHeight?: number;
  tooltip?: string;
  checked?: boolean;
  checkType?: "radio" | "checkbox";
}

interface ContextMenuProps {
  items: ContextMenuItem[];
  position: { x: number; y: number };
  onClose: () => void;
  id?: string;
  ariaLabel?: string;
  className?: string;
  itemFontSize?: string;
  returnFocusTo?: HTMLElement;
  anchor?: HTMLElement;
  side?: "top" | "bottom";
}

export const ContextMenu = ({ items, position, onClose, id, ariaLabel, className, itemFontSize, returnFocusTo, anchor, side = "bottom" }: ContextMenuProps) => {
  const surfaces = useRef(new Set<HTMLElement>());
  const triggerRef = useRef(returnFocusTo ?? document.activeElement as HTMLElement);
  const focusListenerRef = useRef<(event: FocusEvent) => void>();
  const closeMenu = () => {
    window.removeEventListener("focusin", focusListenerRef.current!);
    onClose();
  };

  // Dismiss on outside mousedown or Escape; arrow/Home/End roving focus
  useEffect(() => {
    const containsEvent = (event: Event) => (anchor != null && event.composedPath().includes(anchor))
      || [...surfaces.current].some(surface => event.composedPath().includes(surface));
    const handleMouseDown = (e: MouseEvent) => {
      if (!containsEvent(e)) closeMenu();
    };
    const handleFocus = (event: FocusEvent) => {
      if (!containsEvent(event)) closeMenu();
    };
    focusListenerRef.current = handleFocus;
    const handleScroll = (event: Event) => {
      if (!containsEvent(event)) {
        closeMenu();
        triggerRef.current?.focus({ preventScroll: true });
      }
    };
    // Use setTimeout so the current click event doesn't immediately fire
    const timer = window.setTimeout(() => {
      window.addEventListener("mousedown", handleMouseDown);
      window.addEventListener("focusin", handleFocus);
      window.addEventListener("scroll", handleScroll, true);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("mousedown", handleMouseDown);
      window.removeEventListener("focusin", handleFocus);
      window.removeEventListener("scroll", handleScroll, true);
    };
  }, [onClose, anchor]);

  const dismiss = () => { closeMenu(); triggerRef.current?.focus({ preventScroll: true }); };
  const select = (item: ContextMenuItem) => { dismiss(); item.onClick?.(); };
  return createPortal(<MenuSurface items={items} position={position} surfaces={surfaces.current} onDismiss={dismiss} onSelect={select}
    id={id} ariaLabel={ariaLabel} className={className} itemFontSize={itemFontSize} trigger={anchor} side={side} />, document.body);
};

const surfaceStyle: CSSProperties = {
  position: "fixed", zIndex: "var(--z-context-menu)",
  minWidth: "min(180px, calc(100vw - 16px))", maxWidth: "min(260px, calc(100vw - 16px))",
  maxHeight: "calc(100dvh - 16px)", overflowY: "auto", background: "var(--surface-raised)",
  border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)",
  boxShadow: "var(--shadow-strong-overlay)", padding: "6px", outline: "none",
};

function MenuSurface({ items, position, anchor, trigger, side, focusFirst = false, surfaces, onDismiss, onSelect, onBack, id, ariaLabel, className, itemFontSize, maxHeight }: {
  items: ContextMenuItem[];
  position: { x: number; y: number };
  anchor?: HTMLButtonElement;
  trigger?: HTMLElement;
  side?: "top" | "bottom";
  focusFirst?: boolean;
  surfaces: Set<HTMLElement>;
  onDismiss: () => void;
  onSelect: (item: ContextMenuItem) => void;
  onBack?: () => void;
  id?: string;
  ariaLabel?: string;
  className?: string;
  maxHeight?: number;
  itemFontSize?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [adjusted, setAdjusted] = useState(position);
  const [submenu, setSubmenu] = useState<{ index: number; anchor: HTMLButtonElement; focusFirst: boolean } | null>(null);
  const reserveIcon = items.some(item => item.icon !== undefined);
  const openSubmenu = (index: number, button: HTMLButtonElement, focusFirst: boolean) => setSubmenu({ index, anchor: button, focusFirst });
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape" || event.key === "Tab") {
      event.stopPropagation();
      if (event.key === "Escape") event.preventDefault();
      onDismiss();
      return;
    }
    if (event.key === "ArrowLeft" && onBack) { event.preventDefault(); event.stopPropagation(); onBack(); return; }
    if (event.key === "ArrowRight") {
      const button = document.activeElement as HTMLButtonElement;
      const index = Number(button.dataset.menuIndex);
      if (items[index]?.submenu) { event.preventDefault(); event.stopPropagation(); openSubmenu(index, button, true); }
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button[role^="menuitem"]:not(:disabled)'));
    const current = buttons.findIndex(button => button === document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
      : current < 0 ? (event.key === "ArrowDown" ? 0 : buttons.length - 1)
      : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
    buttons[next]?.focus();
  };

  // Adjust position to stay within viewport
  useLayoutEffect(() => {
    const el = ref.current!;
    surfaces.add(el);
    const place = () => {
      const rect = el.getBoundingClientRect();
      const parent = anchor?.getBoundingClientRect();
      const triggerRect = trigger?.getBoundingClientRect();
      const x = parent ? (parent.right + rect.width + 8 > window.innerWidth ? parent.left - rect.width : parent.right)
        : triggerRect ? triggerRect.right - rect.width : position.x;
      const y = parent ? parent.top - 6 : triggerRect ? side === "top" ? triggerRect.top - rect.height - 6 : triggerRect.bottom + 6 : position.y;
      setAdjusted({
        x: Math.max(8, Math.min(x, window.innerWidth - rect.width - 8)),
        y: Math.max(8, Math.min(y, window.innerHeight - rect.height - 8)),
      });
    };
    place();
    const observer = new ResizeObserver(place);
    observer.observe(el);
    window.addEventListener("resize", place);
    return () => { surfaces.delete(el); observer.disconnect(); window.removeEventListener("resize", place); };
  }, [position, anchor, trigger, side, surfaces]);

  // Focus the menu so keyboard events work
  useEffect(() => {
    if (!anchor || focusFirst) (focusFirst ? ref.current!.querySelector<HTMLElement>('button[role^="menuitem"]:not(:disabled)') ?? ref.current : ref.current)?.focus();
  }, [anchor, focusFirst]);

  return <>
    <div
      ref={ref}
      id={id}
      className={className ? `context-menu-surface ${className}` : "context-menu-surface"}
      tabIndex={-1}
      role="menu"
      onKeyDown={handleKeyDown}
      aria-label={ariaLabel ?? anchor?.textContent ?? undefined}
      style={{ ...surfaceStyle, ...(maxHeight === undefined ? {} : { maxHeight: `min(${maxHeight}px, calc(100dvh - 16px))` }), left: adjusted.x, top: adjusted.y }}
    >
      {items.map((item, i) =>
        item.separator ? (
          <div
            key={i}
            role="separator"
            style={{
              height: 1,
              background: "var(--border-subtle)",
              margin: "5px 10px",
            }}
          />
        ) : (
          <button
            key={i}
            type="button"
            tabIndex={-1}
            role={item.checked === undefined ? "menuitem" : item.checkType === "checkbox" ? "menuitemcheckbox" : "menuitemradio"}
            aria-checked={item.checked}
            data-menu-index={i}
            aria-disabled={item.disabled || undefined}
            aria-haspopup={item.submenu ? "menu" : undefined}
            aria-expanded={item.submenu ? submenu?.index === i : undefined}
            title={item.tooltip}
            className="mc-menu-item"
            disabled={item.disabled}
            onMouseEnter={event => item.submenu ? openSubmenu(i, event.currentTarget, false) : setSubmenu(null)}
            onFocus={() => { if (!item.submenu) setSubmenu(null); }}
            onClick={event => item.submenu ? openSubmenu(i, event.currentTarget, true) : onSelect(item)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              width: "100%",
              minHeight: 34,
              textAlign: "left",
              background: "transparent",
              border: 0,
              color: item.disabled
                ? "var(--text-muted)"
                : item.danger ? "var(--state-danger)" : "var(--text-primary)",
              cursor: item.disabled ? "default" : "pointer",
              padding: "6px 10px",
              fontSize: itemFontSize ?? "var(--mc-font-body)",
              lineHeight: "var(--leading-snug)",
              opacity: item.disabled ? 0.45 : 1,
            }}
          >
            {reserveIcon && <span aria-hidden="true" className="shrink-0" style={{ display: "inline-flex", width: 16, alignItems: "center", justifyContent: "center", color: item.danger ? "inherit" : "var(--text-secondary)" }}>
              {item.icon}
            </span>}
            <span className="flex-fill truncate">
              {item.label}
            </span>
            {item.shortcut && (
              <span
                className="shrink-0"
                style={{
                  color: "var(--text-muted)",
                  marginLeft: 12,
                  fontFamily: "var(--font-mono)",
                  fontSize: "var(--mc-font-caption)",
                }}
              >
                {item.shortcut}
              </span>
            )}
            {item.checked && <SidebarSortSourceIcon kind="sort-check" />}
            {item.rightIcon && (item.rightIconTooltip
              ? <Tip content={item.rightIconTooltip}><span className="shrink-0" aria-hidden="true">{item.rightIcon}</span></Tip>
              : <span className="shrink-0" aria-hidden="true">{item.rightIcon}</span>)}
            {item.submenu && <ChevronRight size={14} aria-hidden="true" />}
          </button>
        ),
      )}
    </div>
    {submenu && items[submenu.index]?.submenu && createPortal(<MenuSurface items={items[submenu.index].submenu!} position={{ x: 0, y: 0 }} anchor={submenu.anchor}
      focusFirst={submenu.focusFirst} surfaces={surfaces} onDismiss={onDismiss} onSelect={onSelect}
      itemFontSize={itemFontSize}
      maxHeight={items[submenu.index].submenuMaxHeight}
      onBack={() => { const button = submenu.anchor; setSubmenu(null); button.focus(); }} />, document.body)}
  </>;
}
