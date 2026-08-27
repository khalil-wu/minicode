import { useEffect, useMemo, useRef, useState } from "react";
import { Clock3, Search, Trash2 } from "lucide-react";
import { fuzzyFilter } from "../lib/fuzzy-match";

interface Props {
  open: boolean;
  items: string[];
  placement?: "above" | "below";
  onSelect: (prompt: string) => void;
  onClose: () => void;
  onClear: () => void;
}

export const PromptHistoryOverlay = ({ open, items, placement = "above", onSelect, onClose, onClear }: Props) => {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const filtered = useMemo(() => fuzzyFilter(items, query, (item) => item), [items, query]);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
    queueMicrotask(() => inputRef.current?.focus());
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((index) => filtered.length ? (index + 1) % filtered.length : 0);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((index) => filtered.length ? (index - 1 + filtered.length) % filtered.length : 0);
      } else if (event.key === "Enter" && filtered[activeIndex]) {
        event.preventDefault();
        onSelect(filtered[activeIndex]);
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, [activeIndex, filtered, onClose, onSelect, open]);

  if (!open) return null;
  return (
    <div className="prompt-history-overlay" data-placement={placement}>
      <div className="prompt-history-card">
        <div className="prompt-history-search-row">
          <Search size={14} aria-hidden="true" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => { setQuery(event.target.value); setActiveIndex(0); }}
            placeholder="搜索输入历史"
            aria-label="搜索输入历史"
          />
          {items.length > 0 && (
            <button type="button" onClick={onClear} title="清空输入历史" aria-label="清空输入历史">
              <Trash2 size={14} />
            </button>
          )}
        </div>
        <div role="listbox" aria-label="输入历史" className="prompt-history-list">
          {filtered.length === 0 ? (
            <div className="prompt-history-empty">{items.length === 0 ? "此工作区还没有输入记录" : "没有匹配的输入记录"}</div>
          ) : filtered.map((item, index) => (
            <button
              type="button"
              role="option"
              aria-selected={index === activeIndex}
              key={`${item}-${index}`}
              data-active={index === activeIndex ? "true" : "false"}
              onMouseEnter={() => setActiveIndex(index)}
              onClick={() => onSelect(item)}
            >
              <Clock3 size={14} aria-hidden="true" />
              <span>{item}</span>
            </button>
          ))}
        </div>
        <div className="prompt-history-hint">Ctrl+R · 方向键选择 · Enter 重新使用</div>
      </div>
    </div>
  );
};
