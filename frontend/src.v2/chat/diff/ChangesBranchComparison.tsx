import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, GitBranch, LoaderCircle, Search } from "../../lib/icons";
import { Tip } from "../../components/Tooltip";
import { useI18n } from "../../i18n";
import "./ChangesBranchComparison.css";

export interface ChangesBranchComparisonProps {
  currentBranch?: string | null;
  defaultTargetBranch?: string | null;
  targetBranch?: string | null;
  branches?: readonly string[] | null;
  loading?: boolean;
  error?: boolean | string | null;
  onRetry?: () => void;
  onSelectBranch: (branch: string) => void;
}

function BranchName({ name, suffixCharacters }: { name: string; suffixCharacters: number }) {
  const box = useRef<HTMLSpanElement>(null);
  const [overflows, setOverflows] = useState(false);
  const boundary = Math.max(0, name.length - suffixCharacters);
  useLayoutEffect(() => {
    const measure = () => {
      const target = box.current!.querySelector<HTMLElement>("[data-tooltip-overflow-target]")!;
      setOverflows(target.scrollWidth > target.clientWidth);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(box.current!);
    return () => observer.disconnect();
  }, [name, suffixCharacters, overflows]);
  const text = boundary === 0
    ? <span className="mc-changes-branch-prefix" data-tooltip-overflow-target>{name}</span>
    : <span className="mc-changes-branch-suffix-layout"><span className="mc-changes-branch-prefix" data-tooltip-overflow-target>{name.slice(0, boundary)}</span>
      <span className="mc-changes-branch-suffix">{name.slice(boundary)}</span></span>;
  return <span ref={box} className="mc-changes-branch-name" data-suffix-characters={suffixCharacters}>
    {overflows ? <Tip content={name}>{text}</Tip> : text}
  </span>;
}

type BranchMenuProps = {
  anchor: HTMLButtonElement;
  branches: readonly string[] | undefined;
  selectedBranch?: string | null;
  loading: boolean;
  error: boolean;
  keyboardOpen: boolean;
  onRetry?: () => void;
  onSelectBranch: (branch: string) => void;
  onClose: () => void;
};

/** Current desktop Ya → Fi/h, shared $8/rxn/gxn/hxn. */
function BranchMenu({ anchor, branches, selectedBranch, loading, error, keyboardOpen, onRetry, onSelectBranch, onClose }: BranchMenuProps) {
  const { t } = useI18n();
  const menu = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const typeahead = useRef("");
  const typeaheadTimer = useRef<number>();
  const [query, setQuery] = useState("");
  const [position, setPosition] = useState<CSSProperties>({});
  const trimmed = query.trim();
  const searching = trimmed.length > 0;
  const results = searching ? branches?.filter(branch => branch.toLowerCase().includes(trimmed.toLowerCase())) : branches;
  const exact = results?.find(branch => branch === trimmed);
  const showCustom = searching && exact == null;
  // Ya supplies base-list loading/error only. A typed exact ref remains usable
  // while that initial request is pending or failed; Fi's search lane is separate.
  const showLoading = !searching && loading;
  const showError = !searching && !loading && error;
  const enabledItems = () => Array.from(menu.current!.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'));
  const close = (restoreFocus = false) => {
    onClose();
    if (restoreFocus) anchor.focus({ preventScroll: true });
  };
  const choose = (branch: string) => { onSelectBranch(branch); close(true); };

  useLayoutEffect(() => {
    const place = () => {
      const rect = anchor.getBoundingClientRect(), popup = menu.current!;
      const width = popup.getBoundingClientRect().width;
      const below = window.innerHeight - rect.bottom - 7, above = rect.top - 7;
      const flip = below < popup.scrollHeight && above > below;
      setPosition({ left: Math.max(6, Math.min(rect.left, window.innerWidth - width - 6)),
        top: flip ? undefined : rect.bottom + 1,
        bottom: flip ? window.innerHeight - rect.top + 1 : undefined,
        maxHeight: Math.max(0, Math.min(flip ? above : below, window.innerHeight - 16)) });
    };
    place();
    const observer = new ResizeObserver(place);
    observer.observe(anchor); observer.observe(menu.current!);
    window.addEventListener("resize", place); window.addEventListener("scroll", place, true);
    return () => { observer.disconnect(); window.removeEventListener("resize", place); window.removeEventListener("scroll", place, true); };
  }, [anchor]);
  useEffect(() => {
    // Fi explicitly passes autoFocus=false for Ya. Pointer opening focuses the
    // nonmodal menu; keyboard entry focuses its first enabled item.
    (keyboardOpen ? enabledItems()[0] ?? menu.current! : menu.current!).focus({ preventScroll: true });
    const outside = (event: PointerEvent) => {
      if (!event.composedPath().includes(menu.current!) && !event.composedPath().includes(anchor)) close();
    };
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(true); }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape, true);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape, true); };
  }, [anchor, keyboardOpen, onClose]);
  useEffect(() => () => window.clearTimeout(typeaheadTimer.current), []);

  const searchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    event.stopPropagation();
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "a") {
      event.preventDefault(); event.currentTarget.select(); return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      // Fi's input callback calls the exact/custom selector directly; Ya does
      // not supply its optional onClose. Ordinary menu rows close on select.
      if (searching) onSelectBranch(exact ?? trimmed);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const items = enabledItems();
      const next = event.key === "ArrowDown" ? items[0] : items.at(-1);
      if (next) { event.preventDefault(); next.focus(); }
    }
  };
  const menuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target === input.current) return;
    const items = enabledItems(), current = items.indexOf(event.target as HTMLButtonElement);
    if (!event.ctrlKey && !event.altKey && !event.metaKey && event.key.length === 1) {
      if (event.key === " " && current >= 0 && typeahead.current.length === 0) return;
      // Source common MenuContent handleTypeaheadSearch/CSe/SSe: repeated
      // characters cycle by that one character; ordinary strings build for 1s.
      const search = typeahead.current + event.key;
      typeahead.current = search;
      window.clearTimeout(typeaheadTimer.current);
      typeaheadTimer.current = window.setTimeout(() => { typeahead.current = ""; }, 1000);
      const names = items.map(item => item.textContent!.trim());
      const pattern = search.length > 1 && Array.from(search).every(character => character === search[0]) ? search[0] : search;
      const previous = names[current];
      const start = Math.max(current, 0);
      let rotated = names.map((_, index) => names[(start + index) % names.length]);
      if (pattern.length === 1) rotated = rotated.filter(name => name !== previous);
      const match = rotated.find(name => name.toLowerCase().startsWith(pattern.toLowerCase()));
      if (match != null && match !== previous) window.setTimeout(() => items[names.indexOf(match)].focus(), 0);
      if (event.key === " ") event.preventDefault();
      return;
    }
    if (current === 0 && (event.key === "ArrowUp" || event.key === "Tab" && event.shiftKey)) {
      event.preventDefault(); event.stopPropagation(); input.current!.focus(); return;
    }
    if (event.key === "Tab") { event.preventDefault(); return; }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) || items.length === 0) return;
    event.preventDefault();
    const index = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
      : Math.max(0, Math.min(items.length - 1, current + (event.key === "ArrowDown" ? 1 : -1)));
    items[index].focus();
  };

  return createPortal(<div ref={menu} className="mc-changes-branch-menu" role="menu" tabIndex={-1}
    aria-label={t("选择比较分支")} style={position} onKeyDown={menuKeyDown}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget) && event.relatedTarget !== anchor) close(); }}>
    <div className="mc-changes-branch-menu-body">
      <div className="mc-changes-branch-search"><Search size={14} aria-hidden="true" />
        <input ref={input} value={query} placeholder={t("搜索分支")} aria-label={t("搜索分支")}
          autoComplete="off" spellCheck={false} onChange={event => setQuery(event.target.value)} onKeyDown={searchKeyDown} />
      </div>
      <div className="mc-changes-branch-list">
        <div className="mc-changes-branch-section-label">{t("分支")}</div>
        <div className="mc-changes-branch-items" data-error={showError}>
        {showLoading ? <div className="mc-changes-branch-message" role="status"><LoaderCircle size={12} aria-hidden="true" className="mc-changes-branch-spinner" />
          {t("正在加载分支…")}</div>
          : showError ? <><div className="mc-changes-branch-section-label">{t("无法加载分支")}</div>
            {onRetry && <button type="button" role="menuitem" onClick={() => { onRetry(); close(true); }}>{t("重试")}</button>}</>
            : <>{showCustom && <button type="button" role="menuitem" data-custom-branch={trimmed} onClick={() => choose(trimmed)}>
              <span className="mc-changes-branch-menu-name">{t("使用 {branch}", { branch: trimmed })}</span></button>}
              {results?.map(branch => <button type="button" role="menuitem" key={branch} data-branch={branch}
                data-selected={branch === selectedBranch} onClick={() => choose(branch)} onPointerMove={event => {
                  if (event.pointerType === "mouse") event.currentTarget.focus({ preventScroll: true });
                }}>
                <GitBranch size={14} aria-hidden="true" /><span className="mc-changes-branch-menu-name"><BranchName name={branch} suffixCharacters={branch.length} /></span>
                {branch === selectedBranch && <Check size={14} aria-hidden="true" className="mc-changes-branch-selected" />}
              </button>)}
              {!showCustom && (results == null || results.length === 0) && <div className="mc-changes-branch-message">{t("未找到分支")}</div>}
            </>}
        </div>
      </div>
    </div>
  </div>, document.body);
}

export function ChangesBranchComparison({ currentBranch, defaultTargetBranch, targetBranch, branches, loading = false,
  error = false, onRetry, onSelectBranch }: ChangesBranchComparisonProps) {
  const { t } = useI18n();
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const closeMenu = useCallback(() => setOpen(false), []);
  const keyboardOpen = useRef(false);
  const selectedBranch = targetBranch ?? defaultTargetBranch;
  const choices = useMemo(() => branches == null ? undefined
    : Array.from(new Set([selectedBranch, defaultTargetBranch, ...branches].filter((branch): branch is string => branch != null))),
  [branches, selectedBranch, defaultTargetBranch]);
  if (selectedBranch == null && !loading && !error && (branches == null || branches.length === 0)) return null;
  return <div className="mc-changes-branch-comparison" role="group" aria-label={t("分支比较")}>
    <span className="mc-changes-current-branch"><BranchName name={currentBranch ?? "HEAD"} suffixCharacters={18} /></span>
    <span className="mc-changes-branch-arrow" aria-hidden="true">→</span>
    <button ref={trigger} type="button" className="mc-changes-branch-trigger" aria-haspopup="menu" aria-expanded={open}
      aria-label={selectedBranch ?? t("选择分支")} onPointerDown={() => { keyboardOpen.current = false; }}
      onKeyDown={event => {
        if (["ArrowDown", "Enter", " "].includes(event.key)) {
          event.preventDefault(); keyboardOpen.current = true; setOpen(true);
        }
      }} onClick={() => { keyboardOpen.current = false; setOpen(value => !value); }}>
      {selectedBranch == null ? <span>{t("选择分支")}</span> : <BranchName name={selectedBranch} suffixCharacters={28} />}
      <ChevronDown size={12} aria-hidden="true" />
    </button>
    {open && <BranchMenu anchor={trigger.current!} branches={choices} selectedBranch={selectedBranch} loading={loading}
      error={Boolean(error)} keyboardOpen={keyboardOpen.current} onRetry={onRetry} onSelectBranch={onSelectBranch} onClose={closeMenu} />}
  </div>;
}
