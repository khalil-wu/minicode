import { Children, isValidElement, useEffect, useId, useMemo, useRef, useState } from "react";
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

const optionsFromChildren = (children: ReactNode, group?: string): SelectOption[] => {
  const options: SelectOption[] = [];
  Children.forEach(children, (child) => {
    if (!isValidElement(child)) return;
    if (child.type === "optgroup") {
      const props = child.props as { label?: string; children?: ReactNode };
      options.push(...optionsFromChildren(props.children, String(props.label || "").trim() || undefined));
      return;
    }
    if (child.type !== "option") return;
    const option = child as ReactElement<OptionHTMLAttributes<HTMLOptionElement>>;
    options.push({
      value: String(option.props.value ?? ""),
      label: nodeText(option.props.children) || String(option.props.value ?? ""),
      disabled: Boolean(option.props.disabled),
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
  const [opensAbove, setOpensAbove] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuId = useId();
  const options = useMemo(() => optionsFromChildren(children), [children]);
  const selected = options.find((option) => option.value === value) ?? options[0];
  const enabledOptions = options.filter((option) => !option.disabled);

  useEffect(() => {
    if (!open) return undefined;
    const close = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnViewportChange = () => setOpen(false);
    document.addEventListener("pointerdown", close);
    window.addEventListener("resize", closeOnViewportChange);
    return () => {
      document.removeEventListener("pointerdown", close);
      window.removeEventListener("resize", closeOnViewportChange);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    queueMicrotask(() => {
      const active = rootRef.current?.querySelector<HTMLButtonElement>('[role="option"][aria-selected="true"]');
      const first = rootRef.current?.querySelector<HTMLButtonElement>('[role="option"]:not(:disabled)');
      (active ?? first)?.focus();
    });
  }, [open]);

  const openMenu = () => {
    if (disabled) return;
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) {
      const estimatedHeight = Math.min(menuMaxHeight, options.length * 36 + 16);
      setOpensAbove(window.innerHeight - rect.bottom < estimatedHeight && rect.top > window.innerHeight - rect.bottom);
    }
    setOpen(true);
  };

  const selectValue = (nextValue: string) => {
    onValueChange(nextValue);
    setOpen(false);
    queueMicrotask(() => triggerRef.current?.focus());
  };

  const handleTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openMenu();
    }
  };

  const handleOptionKeyDown = (event: KeyboardEvent<HTMLButtonElement>, option: SelectOption) => {
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      queueMicrotask(() => triggerRef.current?.focus());
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectValue(option.value);
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = enabledOptions.findIndex((item) => item.value === option.value);
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? enabledOptions.length - 1
        : (current + (event.key === "ArrowDown" ? 1 : -1) + enabledOptions.length) % enabledOptions.length;
    const nextValue = enabledOptions[nextIndex]?.value;
    Array.from(rootRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? [])
      .find((item) => item.dataset.value === nextValue)
      ?.focus();
  };

  let previousGroup = "";
  return (
    <div ref={rootRef} className={`mc-select-menu ${className}`.trim()} style={style} data-open={open ? "true" : "false"}>
      <select
        id={id}
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
      {open && (
        <div
          id={menuId}
          role="listbox"
          aria-label={ariaLabel}
          className="mc-select-popover"
          data-placement={opensAbove ? "top" : "bottom"}
          style={{ maxHeight: menuMaxHeight }}
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
        </div>
      )}
    </div>
  );
};
