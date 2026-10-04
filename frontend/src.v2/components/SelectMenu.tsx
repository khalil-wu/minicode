import { Children, isValidElement, useEffect, useLayoutEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { CSSProperties, KeyboardEvent, OptionHTMLAttributes, ReactElement, ReactNode } from "react";
import { Check, ChevronDown } from "lucide-react";
import "./select-menu.css";

type SelectOption = {
  value: string;
  label: string;
  disabled: boolean;
  group?: string;
};

type SelectMenuProps = {
  value: string;
  onValueChange: (value: string) => void;
  children: ReactNode;
  ariaLabel: string;
  ariaDescribedBy?: string;
  id?: string;
  title?: string;
  disabled?: boolean;
  className?: string;
  style?: CSSProperties;
  menuMaxHeight?: number;
};

const nodeText = (node: ReactNode): string => Children.toArray(node)
  .map((item) => typeof item === "string" || typeof item === "number" ? String(item) : "")
  .join("")
  .trim();

const optionsFromChildren = (children: ReactNode, group?: string, groupDisabled = false): SelectOption[] => {
  const options: SelectOption[] = [];
  Children.forEach(children, (child) => {
    if (!isValidElement(child)) return;
    if (child.type === "optgroup") {
      const props = child.props as { label?: string; disabled?: boolean; children?: ReactNode };
      options.push(...optionsFromChildren(props.children, String(props.label || "").trim() || undefined, Boolean(props.disabled)));
      return;
    }
    if (child.type !== "option") return;
    const option = child as ReactElement<OptionHTMLAttributes<HTMLOptionElement>>;
    options.push({
      value: String(option.props.value ?? ""),
      label: nodeText(option.props.children) || String(option.props.value ?? ""),
      disabled: groupDisabled || Boolean(option.props.disabled),
      group,
    });
  });
  return options;
};

export const SelectMenu = ({
  value,
  onValueChange,
  children,
  ariaLabel,
  ariaDescribedBy,
  id,
  title,
  disabled = false,
  className = "",
  style,
  menuMaxHeight = 280,
}: SelectMenuProps) => {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0, width: 0, maxHeight: menuMaxHeight, above: false });
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const options = useMemo(() => optionsFromChildren(children), [children]);
  const selected = options.find((option) => option.value === value);
  const enabledOptions = options.filter((option) => !option.disabled);

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (!open) return undefined;
    const close = (event: PointerEvent) => {
      const path = event.composedPath();
      if (!path.includes(rootRef.current!) && !path.includes(menuRef.current!)) setOpen(false);
    };
    const closeOnViewportChange = () => setOpen(false);
    const closeOnScroll = (event: Event) => { if (!event.composedPath().includes(menuRef.current!)) setOpen(false); };
    document.addEventListener("pointerdown", close);
    window.addEventListener("resize", closeOnViewportChange);
    window.addEventListener("scroll", closeOnScroll, true);
    return () => {
      document.removeEventListener("pointerdown", close);
      window.removeEventListener("resize", closeOnViewportChange);
      window.removeEventListener("scroll", closeOnScroll, true);
    };
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const rect = triggerRef.current!.getBoundingClientRect();
      const menu = menuRef.current!;
      const height = Math.min(menu.scrollHeight, menuMaxHeight);
      const below = window.innerHeight - rect.bottom - 14;
      const above = rect.top - 14;
      const useAbove = below < height && above > below;
      const maxHeight = Math.max(0, Math.min(menuMaxHeight, useAbove ? above : below));
      const width = Math.min(rect.width, window.innerWidth - 16);
      setPosition({ left: Math.max(8, Math.min(rect.left, window.innerWidth - width - 8)),
        top: useAbove ? rect.top - 6 - Math.min(menu.scrollHeight, maxHeight) : rect.bottom + 6,
        width, maxHeight, above: useAbove });
    };
    place();
    const observer = new ResizeObserver(place);
    observer.observe(triggerRef.current!); observer.observe(menuRef.current!);
    return () => observer.disconnect();
  }, [open, menuMaxHeight, options]);

  useEffect(() => {
    if (!open) return;
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      const selectedOption = menuRef.current?.querySelector<HTMLButtonElement>('[role="option"][aria-selected="true"]:not(:disabled)');
      const first = menuRef.current?.querySelector<HTMLButtonElement>('[role="option"]:not(:disabled)');
      (selectedOption ?? first)?.focus();
    });
    return () => { active = false; };
  }, [open]);

  const openMenu = () => {
    if (disabled) return;
    setOpen(true);
  };

  const selectValue = (nextValue: string) => {
    onValueChange(nextValue);
    setOpen(false);
    triggerRef.current!.focus({ preventScroll: true });
  };

  const handleTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (open && event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      event.stopPropagation();
      openMenu();
    }
  };

  const handleOptionKeyDown = (event: KeyboardEvent<HTMLButtonElement>, option: SelectOption) => {
    if (event.key === "Tab") { setOpen(false); triggerRef.current!.focus({ preventScroll: true }); return; }
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      triggerRef.current!.focus({ preventScroll: true });
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      event.stopPropagation();
      selectValue(option.value);
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    event.stopPropagation();
    const current = enabledOptions.findIndex((item) => item.value === option.value);
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? enabledOptions.length - 1
        : (current + (event.key === "ArrowDown" ? 1 : -1) + enabledOptions.length) % enabledOptions.length;
    const nextValue = enabledOptions[nextIndex]?.value;
    Array.from(menuRef.current!.querySelectorAll<HTMLButtonElement>('[role="option"]'))
      .find((item) => item.dataset.value === nextValue)
      ?.focus();
  };

  let previousGroup = "";
  return (
    <div ref={rootRef} className={`mc-select-menu ${className}`.trim()} style={style} data-open={open ? "true" : "false"}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget) && !menuRef.current?.contains(event.relatedTarget)) setOpen(false);
      }}
    >
      <select
        aria-label={ariaLabel}
        aria-describedby={ariaDescribedBy}
        aria-hidden="true"
        tabIndex={-1}
        value={value}
        disabled={disabled}
        onChange={(event) => onValueChange(event.target.value)}
        className="mc-select-native-proxy"
      >
        {children}
      </select>
      <button
        id={id}
        ref={triggerRef}
        type="button"
        className="mc-select-trigger"
        aria-label={`${ariaLabel}，当前：${selected?.label || "未选择"}`}
        aria-describedby={ariaDescribedBy}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-controls={open ? menuId : undefined}
        title={title || selected?.label || ariaLabel}
        disabled={disabled}
        onClick={() => open ? setOpen(false) : openMenu()}
        onKeyDown={handleTriggerKeyDown}
      >
        <span>{selected?.label || "请选择"}</span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          id={menuId}
          role="listbox"
          aria-label={ariaLabel}
          className={`mc-select-popover${className.split(/\s+/).includes("mc-select-menu-mono") ? " mc-select-menu-mono" : ""}`}
          data-placement={position.above ? "top" : "bottom"}
          style={{ left: position.left, top: position.top, width: position.width, maxHeight: position.maxHeight }}
        >
          {options.map((option) => {
            const showGroup = Boolean(option.group && option.group !== previousGroup);
            previousGroup = option.group || "";
            const active = option.value === value;
            return (
              <div key={`${option.group || ""}:${option.value}`} className="mc-select-option-wrap">
                {showGroup && <div className="mc-select-group">{option.group}</div>}
                <button
                  type="button"
                  role="option"
                  tabIndex={-1}
                  aria-selected={active}
                  data-value={option.value}
                  className="mc-select-option"
                  disabled={option.disabled}
                  onClick={() => selectValue(option.value)}
                  onKeyDown={(event) => handleOptionKeyDown(event, option)}
                >
                  <span className="mc-select-check" data-visible={active ? "true" : "false"}><Check size={14} /></span>
                  <span className="mc-select-option-label">{option.label}</span>
                </button>
              </div>
            );
          })}
        </div>, document.body
      )}
    </div>
  );
};
