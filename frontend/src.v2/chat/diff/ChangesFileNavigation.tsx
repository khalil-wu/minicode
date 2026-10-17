import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import { animate, motion, useMotionValue, useReducedMotion, useTransform } from "motion/react";
import { ChevronRight, MessageCircle, Search } from "../../lib/icons";
import { fileIcon, folderIcon } from "../../lib/file-icons";
import { useI18n } from "../../i18n";
import { useAppStore } from "../../stores";
import { rankChangedFilePaths } from "./changes-file-matcher";
import "./ChangesFileNavigation.css";

export interface ChangesFileEntry {
  path: string;
  additions?: number | null;
  deletions?: number | null;
  commentCount?: number;
}

export interface ChangesFileJumpProps {
  anchor: HTMLElement;
  entries: readonly ChangesFileEntry[];
  activePath?: string | null;
  onSelect: (path: string) => void;
  onClose: () => void;
}

export interface ChangedFilesTreeProps {
  open: boolean;
  entries: readonly ChangesFileEntry[];
  activePath?: string | null;
  parentWidth: number;
  onSelect: (path: string) => void;
  onClose: () => void;
}

const pathParts = (path: string) => {
  const normalized = path.replaceAll("\\", "/");
  const index = normalized.lastIndexOf("/");
  return { fileName: normalized.slice(index + 1), parent: index < 0 ? "" : normalized.slice(0, index) };
};

export function ChangesFileJump({ anchor, entries, onSelect, onClose }: ChangesFileJumpProps) {
  const { t } = useI18n();
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const closing = useRef(false);
  const [query, setQuery] = useState("");
  const [highlightedPath, setHighlightedPath] = useState<string | null>(null);
  const [placement, setPlacement] = useState<CSSProperties>({});
  const paths = useMemo(() => rankChangedFilePaths(entries.map(entry => entry.path), query), [entries, query]);
  const selected = paths.includes(highlightedPath ?? "") ? highlightedPath : paths[0];
  const label = t("跳转到文件");
  const dismiss = (restoreFocus = false) => {
    if (closing.current) return;
    closing.current = true;
    onClose();
    if (restoreFocus) anchor.focus({ preventScroll: true });
  };

  useLayoutEffect(() => {
    const place = () => {
      const rect = anchor.getBoundingClientRect();
      const menu = popup.current!;
      const width = Math.min(menu.getBoundingClientRect().width, window.innerWidth - 16);
      const below = window.innerHeight - rect.bottom - 7;
      const above = rect.top - 7;
      const flip = below < menu.scrollHeight && above > below;
      setPlacement({ left: Math.max(6, Math.min(rect.right - width, window.innerWidth - width - 6)),
        top: flip ? undefined : rect.bottom + 1,
        bottom: flip ? window.innerHeight - rect.top + 1 : undefined,
        maxHeight: Math.max(0, Math.min(250, flip ? above : below, window.innerHeight - 16)) });
    };
    place();
    const observer = new ResizeObserver(place);
    observer.observe(anchor);
    observer.observe(popup.current!);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => { observer.disconnect(); window.removeEventListener("resize", place); window.removeEventListener("scroll", place, true); };
  }, [anchor]);

  useEffect(() => {
    input.current!.focus({ preventScroll: true });
    const closeOutside = (event: PointerEvent) => {
      const path = event.composedPath();
      if (!path.includes(popup.current!) && !path.includes(anchor)) dismiss();
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
    };
  }, [anchor, onClose]);

  useEffect(() => {
    if (selected) popup.current?.querySelector<HTMLElement>(`[data-selected="true"]`)?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  const choose = (path: string) => {
    onSelect(path);
    dismiss(true);
  };
  const keyDown = (event: KeyboardEvent) => {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation(); dismiss(true); return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      if (selected) choose(selected);
      return;
    }
    const down = event.key === "ArrowDown" || event.ctrlKey && ["j", "n"].includes(event.key);
    const up = event.key === "ArrowUp" || event.ctrlKey && ["k", "p"].includes(event.key);
    if (!(down || up || event.key === "Home" || event.key === "End") || paths.length === 0) return;
    event.preventDefault();
    const index = paths.indexOf(selected!);
    const next = event.key === "Home" || event.metaKey && up ? 0 : event.key === "End" || event.metaKey && down ? paths.length - 1
      : Math.max(0, Math.min(paths.length - 1, index + (down ? 1 : -1)));
    setHighlightedPath(paths[next]);
  };

  return createPortal(
    <div ref={popup} className="mc-changes-file-jump" role="dialog" aria-label={label} style={placement} onKeyDown={keyDown}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) dismiss(); }}>
      <div className="mc-changes-file-search">
        <Search size={12} aria-hidden="true" />
        <input ref={input} aria-label={label} placeholder={label} role="combobox" aria-expanded="true"
          aria-autocomplete="list"
          aria-controls={id} aria-activedescendant={selected ? `${id}-${encodeURIComponent(selected)}` : undefined}
          autoComplete="off" spellCheck={false} value={query} onChange={event => {
            setQuery(event.target.value); setHighlightedPath(null);
          }} />
      </div>
      <div id={id} className="mc-changes-file-jump-list" role="listbox" aria-label={label}>
        {paths.length === 0 ? <div className="mc-changes-file-empty">{t("无匹配文件")}</div>
          : paths.map(path => {
            const { fileName, parent } = pathParts(path);
            return <button key={path} id={`${id}-${encodeURIComponent(path)}`} type="button" role="option" tabIndex={-1}
              aria-label={path} aria-selected={selected === path} data-selected={selected === path}
              onMouseMove={() => setHighlightedPath(path)} onMouseDown={event => event.preventDefault()} onClick={() => choose(path)}>
              <span className="mc-changes-file-jump-name">{fileName}</span>
              {parent && <span className="mc-changes-file-jump-parent">{parent}</span>}
            </button>;
          })}
      </div>
    </div>, document.body,
  );
}

type FileNode = { kind: "file"; key: string; name: string; entry: ChangesFileEntry };
type DirectoryNode = { kind: "directory"; key: string; name: string; children: TreeNode[] };
type TreeNode = FileNode | DirectoryNode;
type VisibleRow = { node: TreeNode; depth: number; parent?: string };

function buildTree(entries: readonly ChangesFileEntry[], rankedPaths: readonly string[]): TreeNode[] {
  const root: DirectoryNode = { kind: "directory", key: "", name: "", children: [] };
  const byPath = new Map(entries.map(entry => [entry.path, entry]));
  const rank = new Map(rankedPaths.map((path, index) => [path, index]));
  for (const path of rankedPaths) {
    const segments = path.replaceAll("\\", "/").split("/").filter(Boolean);
    let current = root;
    for (let index = 0; index < segments.length - 1; index++) {
      const key = segments.slice(0, index + 1).join("/") + "/";
      let directory = current.children.find((node): node is DirectoryNode => node.kind === "directory" && node.key === key);
      if (!directory) {
        directory = { kind: "directory", key, name: segments[index], children: [] };
        current.children.push(directory);
      }
      current = directory;
    }
    current.children.push({ kind: "file", key: path, name: segments.at(-1)!, entry: byPath.get(path)! });
  }
  const flatten = (nodes: TreeNode[]): TreeNode[] => nodes.map(node => {
    if (node.kind === "file") return node;
    let merged = node;
    while (merged.children.length === 1 && merged.children[0].kind === "directory") {
      const child = merged.children[0];
      merged = { ...child, name: `${merged.name}/${child.name}` };
    }
    return { ...merged, children: flatten(merged.children) };
  }).sort((left, right) => {
    if (left.kind !== right.kind) return left.kind === "directory" ? -1 : 1;
    return left.kind === "directory" ? left.name.localeCompare(right.name) : rank.get(left.key)! - rank.get(right.key)!;
  });
  return flatten(root.children);
}

function visibleTreeRows(nodes: TreeNode[], collapsed: ReadonlySet<string>, depth = 1, parent?: string): VisibleRow[] {
  return nodes.flatMap(node => [{ node, depth, parent }, ...(node.kind === "directory" && !collapsed.has(node.key)
    ? visibleTreeRows(node.children, collapsed, depth + 1, node.key) : [])]);
}

function FileCounts({ entry }: { entry: ChangesFileEntry }) {
  return <span className="mc-changes-file-counts">
    {entry.commentCount !== undefined && entry.commentCount > 0 && <span className="mc-changes-file-comments"
      aria-label={`${entry.commentCount} comments`}><MessageCircle size={12} aria-hidden="true" /><span>{entry.commentCount}</span></span>}
    {entry.additions != null && entry.additions > 0 && <span className="mc-changes-file-added">+{entry.additions}</span>}
    {entry.deletions != null && entry.deletions > 0 && <span className="mc-changes-file-deleted">-{entry.deletions}</span>}
  </span>;
}

const MIN_TREE_WIDTH = 200;
const DEFAULT_TREE_WIDTH = 250;
const MAX_TREE_RATIO = .6;
const TREE_CLOSE_THRESHOLD = MIN_TREE_WIDTH * .5;
const TREE_TRANSITION = { type: "spring" as const, duration: .5, bounce: .1 };

export function ChangedFilesTree({ open, entries, activePath, parentWidth, onSelect, onClose }: ChangedFilesTreeProps) {
  const { t } = useI18n();
  const pane = useRef<HTMLElement>(null);
  const tree = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  const [width, setWidth] = useState(DEFAULT_TREE_WIDTH);
  const [resizing, setResizing] = useState(false);
  const [mounted, setMounted] = useState(open);
  const systemReducedMotion = useReducedMotion();
  const applicationReducedMotion = useAppStore(state => state.reducedMotion);
  const reducedMotion = systemReducedMotion || applicationReducedMotion;
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [focusedKey, setFocusedKey] = useState<string | null>(null);
  const resize = useRef<{ x: number; width: number; stop: () => void } | null>(null);
  const rankedPaths = useMemo(() => rankChangedFilePaths(entries.map(entry => entry.path), query), [entries, query]);
  const matchingPaths = useMemo(() => new Set(rankedPaths), [rankedPaths]);
  const nodes = useMemo(() => buildTree(entries, rankedPaths), [entries, rankedPaths]);
  const rows = useMemo(() => visibleTreeRows(nodes, collapsed), [nodes, collapsed]);
  const selectedPath = matchingPaths.has(activePath ?? "") ? activePath : entries.find(entry => matchingPaths.has(entry.path))?.path;
  const focusKey = rows.some(row => row.node.key === focusedKey) ? focusedKey
    : rows.some(row => row.node.key === selectedPath) ? selectedPath : rows[0]?.node.key;
  const boundedWidth = Math.min(Math.max(width, MIN_TREE_WIDTH), Math.max(MIN_TREE_WIDTH, parentWidth * MAX_TREE_RATIO));
  const progress = useMotionValue(open ? 1 : 0);
  const size = useMotionValue(boundedWidth);
  const animatedWidth = useTransform([progress, size], ([amount, pixels]) => Math.max(0, Math.min(1, amount as number)) * (pixels as number));

  useLayoutEffect(() => { size.set(boundedWidth); }, [boundedWidth, size]);
  useLayoutEffect(() => { if (mounted || open) pane.current!.inert = !open; }, [mounted, open]);
  useEffect(() => {
    if (reducedMotion) { progress.set(open ? 1 : 0); setMounted(open); return; }
    if (open) setMounted(true);
    const animation = animate(progress, open ? 1 : 0, { ...TREE_TRANSITION,
      onComplete: () => { if (progress.get() === 0) setMounted(false); } });
    return () => animation.stop();
  }, [open, progress, reducedMotion]);
  useEffect(() => () => resize.current?.stop(), []);
  useEffect(() => { if (!open) resize.current?.stop(); }, [open]);

  const toggle = (key: string, close?: boolean) => setCollapsed(current => {
    const next = new Set(current);
    if (close ?? !next.has(key)) next.add(key); else next.delete(key);
    return next;
  });
  const focusRow = (key: string) => {
    setFocusedKey(key);
    Array.from(tree.current!.querySelectorAll<HTMLButtonElement>("[role=treeitem]"))
      .find(row => row.dataset.key === key)?.focus({ preventScroll: true });
  };
  const rowKeyDown = (event: KeyboardEvent<HTMLButtonElement>, row: VisibleRow) => {
    const index = rows.indexOf(row);
    if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? rows.length - 1
        : Math.max(0, Math.min(rows.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)));
      focusRow(rows[next].node.key);
    } else if (event.key === "ArrowRight" && row.node.kind === "directory") {
      event.preventDefault();
      if (collapsed.has(row.node.key)) toggle(row.node.key, false);
      else if (rows[index + 1]?.parent === row.node.key) focusRow(rows[index + 1].node.key);
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      if (row.node.kind === "directory" && !collapsed.has(row.node.key)) toggle(row.node.key, true);
      else if (row.parent) focusRow(row.parent);
    }
  };
  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    resize.current?.stop();
    const cursor = document.body.style.cursor, userSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none";
    setResizing(true);
    const move = (next: PointerEvent) => {
      const desired = boundedWidth + event.clientX - next.clientX;
      if (desired < TREE_CLOSE_THRESHOLD) { onClose(); return; }
      setWidth(Math.min(Math.max(desired, MIN_TREE_WIDTH), Math.max(MIN_TREE_WIDTH, parentWidth * MAX_TREE_RATIO)));
    };
    const stop = () => {
      window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
      document.body.style.cursor = cursor; document.body.style.userSelect = userSelect;
      resize.current = null; setResizing(false);
    };
    resize.current = { x: event.clientX, width: boundedWidth, stop };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
  };

  if (!mounted && !open) return null;
  return <motion.aside ref={pane} className="mc-changed-files-tree" aria-label={t("已更改文件")}
    aria-hidden={!open} data-open={open} data-resizing={resizing}
    style={{ width: animatedWidth, opacity: progress }}>
    <div className="mc-changed-files-tree-resize" role="separator" aria-label={t("调整文件树宽度")}
      aria-orientation="vertical" aria-valuemin={MIN_TREE_WIDTH} aria-valuemax={Math.max(MIN_TREE_WIDTH, parentWidth * MAX_TREE_RATIO)}
      aria-valuenow={boundedWidth} onPointerDown={startResize} />
    <div className="mc-changed-files-tree-content">
      <div className="mc-changes-file-search">
        <Search size={14} aria-hidden="true" />
        <input aria-label={t("筛选文件")} placeholder={t("筛选文件")}
          autoComplete="off" spellCheck={false} value={query} onChange={event => setQuery(event.target.value)} />
      </div>
      <div ref={tree} className="mc-changed-files-tree-rows" role="tree" aria-label={t("已更改文件")}>
        {rows.length === 0 ? <div className="mc-changes-file-empty">{t("无匹配文件")}</div>
          : rows.map(row => <button type="button" key={row.node.key} data-key={row.node.key} role="treeitem"
            aria-label={row.node.kind === "file" ? row.node.entry.path : row.node.name}
            aria-level={row.depth} aria-expanded={row.node.kind === "directory" ? !collapsed.has(row.node.key) : undefined}
            aria-selected={row.node.kind === "file" && row.node.entry.path === selectedPath}
            tabIndex={row.node.key === focusKey ? 0 : -1} style={{ paddingInlineStart: (row.depth - 1) * 14 + 4 }}
            onFocus={() => setFocusedKey(row.node.key)} onKeyDown={event => rowKeyDown(event, row)}
            onClick={() => row.node.kind === "directory" ? toggle(row.node.key) : onSelect(row.node.entry.path)}>
            {row.node.kind === "directory"
              ? <><ChevronRight size={12} className="mc-changes-directory-chevron" data-expanded={!collapsed.has(row.node.key)} aria-hidden="true" />
                {folderIcon(!collapsed.has(row.node.key), 16, row.node.name)}</>
              : <><span className="mc-changes-file-indent" />{fileIcon(row.node.name, { size: 16 })}</>}
            <span className="mc-changes-tree-name">{row.node.name}</span>
            {row.node.kind === "file" && <FileCounts entry={row.node.entry} />}
          </button>)}
      </div>
    </div>
  </motion.aside>;
}
