import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, MouseEvent as ReactMouseEvent, RefObject } from "react";
import { createPortal } from "react-dom";
import { MenuSourceIcon } from "../lib/menu-source-icons";
import { SidebarSortSourceIcon } from "../lib/sidebar-sort-icons";
import { Tip } from "./Tooltip";

export interface ContextMenuItem {
  label: string;
  content?: React.ReactNode;
  icon?: React.ReactNode;
  iconWidth?: number;
  rightIcon?: React.ReactNode;
  rightIconTooltip?: string;
  shortcut?: string;
  disabled?: boolean;
  danger?: boolean;
  separator?: boolean;
  onClick?: () => void;
  submenu?: ContextMenuItem[];
  submenuClassName?: string;
  submenuMaxHeight?: number;
  alignToParentBottom?: boolean;
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
  style?: CSSProperties;
  itemFontSize?: string;
  returnFocusTo?: HTMLElement;
  anchor?: HTMLElement;
  side?: "top" | "bottom" | "left" | "right";
  align?: "start" | "end";
  alignOffset?: number;
}

export const ContextMenu = ({ items, position, onClose, id, ariaLabel, className, style, itemFontSize, returnFocusTo, anchor, side = "bottom", align = "end", alignOffset = 0 }: ContextMenuProps) => {
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
    // Use setTimeout so the current click event doesn't immediately fire
    const timer = window.setTimeout(() => {
      window.addEventListener("mousedown", handleMouseDown);
      window.addEventListener("focusin", handleFocus);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("mousedown", handleMouseDown);
      window.removeEventListener("focusin", handleFocus);
    };
  }, [onClose, anchor]);

  const dismiss = () => { closeMenu(); triggerRef.current?.focus({ preventScroll: true }); };
  const select = (item: ContextMenuItem) => { dismiss(); item.onClick?.(); };
  return createPortal(<MenuSurface items={items} position={position} surfaces={surfaces.current} onDismiss={dismiss} onSelect={select}
    id={id} ariaLabel={ariaLabel} className={className} style={style} itemFontSize={itemFontSize} trigger={anchor} side={side} align={align} alignOffset={alignOffset} />, document.body);
};

const surfaceStyle: CSSProperties = {
  position: "fixed", zIndex: "var(--z-context-menu)",
  maxWidth: "calc(100vw - 16px)",
  maxHeight: "calc(100dvh - 16px)", overflowY: "auto", padding: "4px", outline: "none",
};

type Point = { x: number; y: number };
const containsPoint = (point: Point, polygon: Point[]) => {
  let inside = false;
  for (let i = 0, previous = polygon.length - 1; i < polygon.length; previous = i++) {
    const a = polygon[i], b = polygon[previous];
    if ((a.y > point.y) !== (b.y > point.y) && point.x < (b.x - a.x) * (point.y - a.y) / (b.y - a.y) + a.x) inside = !inside;
  }
  return inside;
};

function MenuSurface({ items, position, anchor, trigger, side, align = "end", alignOffset = 0, focusFirst = false, surfaces, onDismiss, onSelect, onBack, id, ariaLabel, className, style, itemFontSize, maxHeight, alignToParentBottom, surfaceRef }: {
  items: ContextMenuItem[];
  position: { x: number; y: number };
  anchor?: HTMLButtonElement;
  trigger?: HTMLElement;
  side?: "top" | "bottom" | "left" | "right";
  align?: "start" | "end";
  alignOffset?: number;
  focusFirst?: boolean;
  surfaces: Set<HTMLElement>;
  onDismiss: () => void;
  onSelect: (item: ContextMenuItem) => void;
  onBack?: () => void;
  id?: string;
  ariaLabel?: string;
  className?: string;
  style?: CSSProperties;
  maxHeight?: number;
  itemFontSize?: string;
  alignToParentBottom?: boolean;
  surfaceRef?: RefObject<HTMLDivElement>;
}) {
  const ownRef = useRef<HTMLDivElement>(null);
  const ref = surfaceRef ?? ownRef;
  const submenuRef = useRef<HTMLDivElement>(null);
  const [adjusted, setAdjusted] = useState({ ...position, side: "right" as "right" | "left" });
  const [submenu, setSubmenu] = useState<{ index: number; anchor: HTMLButtonElement; focusFirst: boolean } | null>(null);
  const typeahead = useRef({ value: "", time: 0 });
  const hover = useRef<{ index: number; timer: number } | null>(null);
  const grace = useRef<{ side: "left" | "right"; area: Point[]; timer: number } | null>(null);
  const pointer = useRef({ x: 0, direction: "right" as "left" | "right" });
  const reserveIcon = items.some(item => item.icon !== undefined);
  const clearHover = () => { if (hover.current) window.clearTimeout(hover.current.timer); hover.current = null; };
  const clearGrace = () => { if (grace.current) window.clearTimeout(grace.current.timer); grace.current = null; };
  const headingToSubmenu = (event: ReactMouseEvent) => grace.current?.side === pointer.current.direction
    && containsPoint({ x: event.clientX, y: event.clientY }, grace.current.area);
  const openSubmenu = (index: number, button: HTMLButtonElement, focusFirst: boolean) => {
    clearHover(); clearGrace();
    setSubmenu({ index, anchor: button, focusFirst });
  };
  const enterItem = (event: ReactMouseEvent<HTMLButtonElement>, item: ContextMenuItem, index: number) => {
    if (headingToSubmenu(event)) return;
    clearGrace();
    if (item.disabled) { clearHover(); setSubmenu(null); ref.current!.focus({ preventScroll: true }); return; }
    const button = event.currentTarget;
    button.focus({ preventScroll: true });
    if (!item.submenu || submenu?.index === index) { clearHover(); return; }
    if (hover.current?.index === index) return;
    clearHover();
    hover.current = { index, timer: window.setTimeout(() => {
      openSubmenu(index, button, false);
      button.focus({ preventScroll: true });
    }, 200) };
  };
  const leaveItem = (event: ReactMouseEvent<HTMLButtonElement>, index: number) => {
    clearHover();
    if (submenu?.index !== index || !submenuRef.current) {
      if (!headingToSubmenu(event)) { setSubmenu(null); ref.current!.focus({ preventScroll: true }); }
      return;
    }
    clearGrace();
    const rect = submenuRef.current.getBoundingClientRect();
    const side = submenuRef.current.dataset.side as "left" | "right";
    const near = side === "right" ? rect.left : rect.right;
    const far = side === "right" ? rect.right : rect.left;
    grace.current = { side, area: [{ x: event.clientX + (side === "right" ? -5 : 5), y: event.clientY },
      { x: near, y: rect.top }, { x: far, y: rect.top }, { x: far, y: rect.bottom }, { x: near, y: rect.bottom }],
      timer: window.setTimeout(() => { grace.current = null; }, 300) };
    if (!headingToSubmenu(event)) { clearGrace(); setSubmenu(null); ref.current!.focus({ preventScroll: true }); }
  };
  useEffect(() => {
    const cancelHover = () => { clearHover(); clearGrace(); };
    document.addEventListener("keydown", cancelHover, true);
    return () => { cancelHover(); document.removeEventListener("keydown", cancelHover, true); };
  }, []);
  useEffect(() => {
    if (hover.current && (!items[hover.current.index]?.submenu || items[hover.current.index].disabled)) clearHover();
    if (submenu && (!items[submenu.index]?.submenu || items[submenu.index].disabled)) setSubmenu(null);
  }, [items, submenu]);
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Tab") {
      event.preventDefault(); event.stopPropagation();
      return;
    }
    if (event.key === "Escape") {
      event.stopPropagation();
      event.preventDefault();
      onDismiss();
      return;
    }
    if (event.key === "ArrowLeft" && onBack) { event.preventDefault(); event.stopPropagation(); onBack(); return; }
    if (event.key === "ArrowRight") {
      const button = document.activeElement as HTMLButtonElement;
      const index = Number(button.dataset.menuIndex);
      if (items[index]?.submenu && !items[index].disabled) { event.preventDefault(); event.stopPropagation(); openSubmenu(index, button, true); }
      return;
    }
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey
      && !(event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || (event.target as HTMLElement).isContentEditable)) {
      const now = Date.now();
      const previous = now - typeahead.current.time < 1000 ? typeahead.current.value : "";
      if (event.key === " " && !previous) return;
      if (event.key === " ") event.preventDefault();
      const value = `${previous}${event.key.toLocaleLowerCase()}`;
      typeahead.current = { value, time: now };
      const query = [...value].every(char => char === value[0]) ? value[0] : value;
      const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button[role^="menuitem"]:not(:disabled)'));
      const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const start = query.length === 1 ? current + 1 : Math.max(current, 0);
      const ordered = [...buttons.slice(start), ...buttons.slice(0, start)];
      const match = ordered.find(button => items[Number(button.dataset.menuIndex)].label.toLocaleLowerCase().startsWith(query));
      if (match) { event.preventDefault(); event.stopPropagation(); match.focus(); }
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button[role^="menuitem"]:not(:disabled)'));
    const current = buttons.findIndex(button => button === document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
      : current < 0 ? (event.key === "ArrowDown" ? 0 : buttons.length - 1)
      : Math.max(0, Math.min(buttons.length - 1, current + (event.key === "ArrowDown" ? 1 : -1)));
    buttons[next]?.focus();
  };

  // Adjust position to stay within viewport
  useLayoutEffect(() => {
    const el = ref.current!;
    surfaces.add(el);
    const place = () => {
      // Placement uses layout size; the menu's entrance transform only affects paint.
      const width = el.offsetWidth, height = el.offsetHeight;
      const parent = anchor?.getBoundingClientRect();
      const triggerRect = trigger?.getBoundingClientRect();
      const parentMenu = anchor?.closest('[role="menu"]')!.getBoundingClientRect();
      const offset = parent && parentMenu ? 4 + (alignToParentBottom ? 0 : Math.max(parent.left - parentMenu.left, parentMenu.right - parent.right, 0)) : 0;
      const opensLeft = parent != null && parent.right + offset + width + 8 > window.innerWidth;
      const x = parent ? (opensLeft ? parent.left - width - offset : parent.right + offset)
        : triggerRect ? side === "right" ? triggerRect.right + 4 : side === "left" ? triggerRect.left - width - 4
          : align === "start" ? triggerRect.left + alignOffset : triggerRect.right - width - alignOffset : position.x;
      const y = parent ? alignToParentBottom ? parent.bottom + 4 - height : parent.top - Number.parseFloat(getComputedStyle(el).paddingTop)
        : triggerRect ? side === "top" ? triggerRect.top - height - 6 : side === "bottom" ? triggerRect.bottom + 6
          : align === "start" ? triggerRect.top + alignOffset : triggerRect.bottom - height - alignOffset : position.y;
      setAdjusted({
        x: Math.max(8, Math.min(x, window.innerWidth - width - 8)),
        y: Math.max(8, Math.min(y, window.innerHeight - height - 8)),
        side: opensLeft ? "left" : "right",
      });
    };
    place();
    const observer = new ResizeObserver(place);
    observer.observe(el);
    const followScroll = (event: Event) => { if (!event.composedPath().includes(el)) place(); };
    window.addEventListener("resize", place);
    window.addEventListener("scroll", followScroll, true);
    return () => { surfaces.delete(el); observer.disconnect(); window.removeEventListener("resize", place); window.removeEventListener("scroll", followScroll, true); };
  }, [position, anchor, trigger, side, align, alignOffset, surfaces, alignToParentBottom]);

  // Focus the menu so keyboard events work
  useEffect(() => {
    if (!anchor || focusFirst) (focusFirst ? ref.current!.querySelector<HTMLElement>('button[role^="menuitem"]:not(:disabled)') ?? ref.current : ref.current)?.focus({ preventScroll: true });
  }, [anchor, focusFirst]);

  return <>
    <div
      ref={ref}
      id={id}
      className={className ? `context-menu-surface ${className}` : "context-menu-surface"}
      tabIndex={-1}
      role="menu"
      data-side={anchor ? adjusted.side : side}
      onKeyDown={handleKeyDown}
      onMouseMoveCapture={event => {
        if (event.clientX !== pointer.current.x) pointer.current.direction = event.clientX > pointer.current.x ? "right" : "left";
        pointer.current.x = event.clientX;
      }}
      aria-label={ariaLabel ?? anchor?.textContent ?? undefined}
      style={{ ...surfaceStyle, ...style, ...(maxHeight === undefined ? {} : { maxHeight: `min(${maxHeight}px, calc(100dvh - 16px))` }), left: adjusted.x, top: adjusted.y }}
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
        ) : item.content ? <div key={i} className="mc-menu-custom-row" role="group" aria-label={item.label}>{item.content}</div> : (
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
            onMouseEnter={event => enterItem(event, item, i)}
            onMouseMove={event => enterItem(event, item, i)}
            onMouseLeave={event => leaveItem(event, i)}
            onFocus={() => { if (submenu?.index !== i) setSubmenu(null); }}
            onClick={event => item.submenu ? openSubmenu(i, event.currentTarget, true) : onSelect(item)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "var(--mc-menu-row-gap, 6px)",
              width: "100%",
              minHeight: 0,
              textAlign: "left",
              background: "transparent",
              border: 0,
              color: item.danger ? "var(--state-danger)" : "var(--text-primary)",
              cursor: item.disabled ? "default" : "pointer",
              padding: "5px 8px",
              fontSize: itemFontSize ?? "var(--text-chrome)",
              lineHeight: 1.25 / .875,
              opacity: item.disabled ? 0.5 : 1,
            }}
          >
            <span className="mc-menu-content">
              {reserveIcon && <span aria-hidden="true" className="mc-menu-leading-icon shrink-0" style={{ display: "inline-flex", width: item.iconWidth ?? 16, alignItems: "center", justifyContent: "center" }}>
                {item.icon}
              </span>}
              <span className="flex-fill truncate">{item.label}</span>
            </span>
            {item.shortcut && (
              <span
                aria-hidden="true"
                className="mc-menu-shortcut shrink-0"
                style={{
                  color: "var(--text-muted)",
                  marginLeft: 8,
                  fontFamily: "var(--font-ui)",
                  fontSize: "var(--mc-font-caption)",
                }}
              >
                {item.shortcut}
              </span>
            )}
            {(item.checkType === "radio" || item.checked) && <span className="shrink-0" style={{ display: "inline-flex", visibility: item.checked ? "visible" : "hidden" }}><SidebarSortSourceIcon kind="sort-check" /></span>}
            {item.rightIcon && (item.rightIconTooltip
              ? <Tip content={item.rightIconTooltip}><span className="shrink-0" aria-hidden="true">{item.rightIcon}</span></Tip>
              : <span className="shrink-0" aria-hidden="true">{item.rightIcon}</span>)}
            {item.submenu && <MenuSourceIcon kind="submenu" />}
          </button>
        ),
      )}
    </div>
    {submenu && !items[submenu.index]?.disabled && items[submenu.index]?.submenu && createPortal(<MenuSurface items={items[submenu.index].submenu!} position={{ x: 0, y: 0 }} anchor={submenu.anchor}
      className={items[submenu.index].submenuClassName}
      ariaLabel={typeof items[submenu.index].label === "string" ? items[submenu.index].label as string : undefined}
      focusFirst={submenu.focusFirst} surfaces={surfaces} onDismiss={onDismiss} onSelect={onSelect}
      itemFontSize={itemFontSize}
      surfaceRef={submenuRef}
      alignToParentBottom={items[submenu.index].alignToParentBottom}
      maxHeight={items[submenu.index].submenuMaxHeight}
      onBack={() => { const button = submenu.anchor; setSubmenu(null); button.focus({ preventScroll: true }); }} />, document.body)}
  </>;
}
