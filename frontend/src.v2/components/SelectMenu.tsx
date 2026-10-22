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
  selectedLabel?: string;
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
  menuWidth?: number;
  align?: "start" | "end";
  renderOptionIcon?: (value: string) => ReactNode;
  optionDescriptions?: Readonly<Record<string, string>>;
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
  selectedLabel,
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
  menuWidth,
  align = "start",
  renderOptionIcon,
  optionDescriptions,
}: SelectMenuProps) => {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0, width: 0, maxHeight: menuMaxHeight, above: false });
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const typeahead = useRef({ value: "", time: 0 });
  const menuId = useId();
  const options = useMemo(() => optionsFromChildren(children), [children]);
  const settingsSelect = className.split(/\s+/).includes("settings-select");
  const selected = options.find((option) => option.value === value);
  const selectedText = selectedLabel ?? selected?.label;
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
      const sideOffset = settingsSelect ? 2 : 6;
      const height = Math.min(menu.scrollHeight, menuMaxHeight);
      const below = window.innerHeight - rect.bottom - 14;
      const above = rect.top - 14;
      const useAbove = below < height && above > below;
      const maxHeight = Math.max(0, Math.min(menuMaxHeight, useAbove ? above : below));
      // The portal keeps its intrinsic content width. A short trigger must not
      // constrain longer options, and a long label wraps only at the viewport.
      const minWidth = Math.min(menuWidth ?? Math.max(rect.width, 220), window.innerWidth - 16);
      const width = Math.min(Math.max(menu.getBoundingClientRect().width, minWidth), window.innerWidth - 16);
      const left = align === "end" ? rect.right - width : rect.left;
      setPosition({ left: Math.max(8, Math.min(left, window.innerWidth - width - 8)),
        top: useAbove ? rect.top - sideOffset - Math.min(menu.scrollHeight, maxHeight) : rect.bottom + sideOffset,
        width: minWidth, maxHeight, above: useAbove });
    };
    place();
    const observer = new ResizeObserver(place);
    observer.observe(triggerRef.current!); observer.observe(menuRef.current!);
    return () => observer.disconnect();
  }, [open, menuMaxHeight, menuWidth, options, align, settingsSelect]);

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
    typeahead.current = { value: "", time: 0 };
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
    if (event.key === "Enter" || (event.key === " " && (!typeahead.current.value || Date.now() - typeahead.current.time >= 1000))) {
      event.preventDefault();
      event.stopPropagation();
      selectValue(option.value);
      return;
    }
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      event.stopPropagation();
      const now = Date.now();
      const value = `${now - typeahead.current.time < 1000 ? typeahead.current.value : ""}${event.key.toLocaleLowerCase()}`;
      typeahead.current = { value, time: now };
      const query = [...value].every(char => char === value[0]) ? value[0] : value;
      const current = enabledOptions.findIndex(item => item.value === option.value);
      const start = query.length === 1 ? current + 1 : current;
      const ordered = [...enabledOptions.slice(start), ...enabledOptions.slice(0, start)];
      const match = ordered.find(item => item.label.toLocaleLowerCase().startsWith(query));
      if (match) Array.from(menuRef.current!.querySelectorAll<HTMLButtonElement>('[role="option"]'))
        .find(item => item.dataset.value === match.value)!.focus({ preventScroll: true });
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
        {!selected && selectedLabel !== undefined && <option value={value}>{selectedLabel}</option>}
        {children}
      </select>
      <button
        id={id}
        ref={triggerRef}
        type="button"
        className="mc-select-trigger"
        aria-label={`${ariaLabel}，当前：${selectedText || "未选择"}`}
        aria-describedby={ariaDescribedBy}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-controls={open ? menuId : undefined}
        title={title || selectedText || ariaLabel}
        disabled={disabled}
        onClick={() => open ? setOpen(false) : openMenu()}
        onKeyDown={handleTriggerKeyDown}
      >
        <span className="mc-select-trigger-value">
          {selected && renderOptionIcon && <span className="mc-select-option-icon" aria-hidden="true">{renderOptionIcon(selected.value)}</span>}
          <span className="mc-select-trigger-label">{selectedText || "请选择"}</span>
        </span>
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
          data-settings-select={settingsSelect ? "true" : undefined}
          style={{ left: position.left, top: position.top, minWidth: position.width, width: menuWidth, maxHeight: position.maxHeight }}
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
                  title={option.label}
                  className="mc-select-option"
                  disabled={option.disabled}
                  onMouseMove={event => { if (!option.disabled) event.currentTarget.focus({ preventScroll: true }); }}
                  onClick={() => selectValue(option.value)}
                  onKeyDown={(event) => handleOptionKeyDown(event, option)}
                >
                  <span className="mc-select-check" data-visible={active ? "true" : "false"}><Check size={14} /></span>
                  <span className="mc-select-option-content">
                    {renderOptionIcon && <span className="mc-select-option-icon" aria-hidden="true">{renderOptionIcon(option.value)}</span>}
                    {optionDescriptions?.[option.value] ? <span className="mc-select-option-copy">
                      <span className="mc-select-option-label">{option.label}</span><small>{optionDescriptions[option.value]}</small>
                    </span> : <span className="mc-select-option-label">{option.label}</span>}
                  </span>
                </button>
              </div>
            );
          })}
        </div>, document.body
      )}
    </div>
  );
};
