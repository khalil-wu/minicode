import { AnimatePresence, motion } from "motion/react";
import {
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { Check, X } from "lucide-react";
import { cx } from "../lib/util";

type Placement = "bottom-start" | "bottom-end" | "top-start" | "top-end" | "right-start";

const spring = { type: "spring", stiffness: 520, damping: 34, mass: 0.7 } as const;

export function Popover({
  anchor,
  open,
  onClose,
  placement = "bottom-start",
  children,
  className,
  offset = 6,
  label,
}: {
  anchor: RefObject<HTMLElement | null>;
  open: boolean;
  onClose: () => void;
  placement?: Placement;
  children: ReactNode;
  className?: string;
  offset?: number;
  label?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number; origin: string } | null>(null);

  const place = useCallback(() => {
    const a = anchor.current?.getBoundingClientRect();
    const el = ref.current;
    if (!a || !el) return;
    const w = el.offsetWidth;
    const h = el.offsetHeight;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let left = 0;
    let top = 0;
    let vertical = placement.startsWith("top") ? "top" : placement.startsWith("right") ? "right" : "bottom";
    if (vertical === "bottom" && a.bottom + offset + h > vh - 8 && a.top - offset - h > 8) vertical = "top";
    if (vertical === "top" && a.top - offset - h < 8 && a.bottom + offset + h < vh - 8) vertical = "bottom";
    if (vertical === "right") {
      left = a.right + offset;
      top = a.top;
    } else {
      left = placement.endsWith("end") ? a.right - w : a.left;
      top = vertical === "bottom" ? a.bottom + offset : a.top - offset - h;
    }
    left = Math.min(Math.max(8, left), vw - w - 8);
    top = Math.min(Math.max(8, top), vh - h - 8);
    const origin = `${placement.endsWith("end") ? "right" : "left"} ${vertical === "top" ? "bottom" : "top"}`;
    setPos({ left, top, origin });
  }, [anchor, placement, offset]);

  useLayoutEffect(() => {
    if (!open) {
      setPos(null);
      return;
    }
    place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (ref.current?.contains(t) || anchor.current?.contains(t)) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        anchor.current?.focus();
      }
    };
    const ro = new ResizeObserver(place);
    if (ref.current) ro.observe(ref.current);
    window.addEventListener("pointerdown", onDown, true);
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", place);
    return () => {
      ro.disconnect();
      window.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", place);
    };
  }, [open, onClose, anchor, place]);

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          ref={ref}
          role="dialog"
          aria-label={label}
          className={cx("popover", className)}
          style={{ left: pos?.left ?? -9999, top: pos?.top ?? -9999, transformOrigin: pos?.origin }}
          // 门户里的事件仍会沿 React 树冒泡，挡住它们以免触发锚点所在行的点击
          onClick={(e) => e.stopPropagation()}
          onDoubleClick={(e) => e.stopPropagation()}
          onContextMenu={(e) => e.stopPropagation()}
          initial={{ opacity: 0, scale: 0.96, y: placement.startsWith("top") ? 4 : -4 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.97, transition: { duration: 0.1 } }}
          transition={spring}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

export interface MenuItem {
  id: string;
  label: string;
  icon?: ReactNode;
  hint?: string;
  desc?: string;
  checked?: boolean;
  danger?: boolean;
  disabled?: boolean;
  onSelect?: () => void;
}

export type MenuEntry = MenuItem | "divider" | { heading: string };

export function Menu({ items, onClose, autoFocus = true }: { items: MenuEntry[]; onClose: () => void; autoFocus?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(-1);
  const selectable = items.filter((i): i is MenuItem => typeof i === "object" && "id" in i && !i.disabled);

  useEffect(() => {
    if (autoFocus) ref.current?.focus();
  }, [autoFocus]);

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const d = e.key === "ArrowDown" ? 1 : -1;
      setActive((a) => (a + d + selectable.length) % selectable.length);
    } else if (e.key === "Enter" && active >= 0) {
      e.preventDefault();
      selectable[active].onSelect?.();
      onClose();
    }
  };

  return (
    <div className="menu" role="menu" tabIndex={-1} ref={ref} onKeyDown={onKey}>
      {items.map((item, i) => {
        if (item === "divider") return <div key={`d${i}`} className="menu-divider" />;
        if ("heading" in item) return <div key={`h${i}`} className="menu-heading">{item.heading}</div>;
        const index = selectable.indexOf(item);
        return (
          <button
            key={item.id}
            role="menuitem"
            className={cx("menu-item", item.danger && "danger", index === active && "active")}
            disabled={item.disabled}
            onMouseEnter={() => setActive(index)}
            onClick={() => {
              item.onSelect?.();
              onClose();
            }}
          >
            {item.icon && <span className="menu-icon">{item.icon}</span>}
            <span className="menu-text">
              <span className="menu-label">{item.label}</span>
              {item.desc && <span className="menu-desc">{item.desc}</span>}
            </span>
            {item.hint && <span className="menu-hint">{item.hint}</span>}
            {item.checked !== undefined && (
              <span className="menu-check">{item.checked && <Check size={15} strokeWidth={2.2} />}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  width = 440,
  className,
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
  className?: string;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open, onClose]);

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="scrim"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.16 }}
          onPointerDown={(e) => {
            if (e.target === e.currentTarget) onClose();
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label={typeof title === "string" ? title : undefined}
            className={cx("dialog", className)}
            style={{ width }}
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: 4, transition: { duration: 0.12 } }}
            transition={spring}
          >
            {title && (
              <div className="dialog-head">
                <div className="dialog-title">{title}</div>
                <button className="icon-btn" aria-label="关闭" onClick={onClose}>
                  <X size={16} />
                </button>
              </div>
            )}
            <div className="dialog-body">{children}</div>
            {footer && <div className="dialog-foot">{footer}</div>}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

export function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      aria-label={label}
      className={cx("switch", checked && "on")}
      onClick={() => onChange(!checked)}
    >
      <motion.span className="switch-thumb" layout transition={spring} />
    </button>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { id: T; label: ReactNode }[];
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.id}
          role="radio"
          aria-checked={value === o.id}
          className={cx("segmented-item", value === o.id && "on")}
          onClick={() => onChange(o.id)}
        >
          {value === o.id && <motion.span layoutId={`seg-${label}`} className="segmented-pill" transition={spring} />}
          <span className="segmented-label">{o.label}</span>
        </button>
      ))}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

export function Spinner({ size = 14 }: { size?: number }) {
  return (
    <svg className="spinner" width={size} height={size} viewBox="0 0 16 16" aria-hidden>
      <circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" strokeOpacity="0.18" strokeWidth="1.8" />
      <path d="M8 1.8a6.2 6.2 0 0 1 6.2 6.2" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

export function useDisclosure(initial = false) {
  const [open, setOpen] = useState(initial);
  const close = useCallback(() => setOpen(false), []);
  const toggle = useCallback(() => setOpen((o) => !o), []);
  return { open, setOpen, close, toggle };
}

/** 每秒刷新一次，用于显示运行中的耗时 */
export function useTick(active: boolean, ms = 1000) {
  const [, setN] = useState(0);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setN((n) => n + 1), ms);
    return () => clearInterval(t);
  }, [active, ms]);
}
