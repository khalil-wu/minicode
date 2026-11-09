import { AnimatePresence, motion } from "motion/react";
import {
  ChevronRight,
  Columns2,
  Copy,
  ExternalLink,
  FileCode2,
  FileDiff,
  FileText,
  Folder,
  FolderOpen,
  Globe,
  RotateCw,
  Rows2,
  Search,
  Undo2,
  X,
} from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { selectChat, useApp } from "../store";
import { fileContents, fileTree, type TreeNode } from "../mock";
import type { Chat, FileChange } from "../types";
import { Tokens, CodeBlock } from "../chat/Markdown";
import { langFromPath, tokenize } from "../lib/highlight";
import { basename, copyText, cx } from "../lib/util";
import { Segmented, Spinner } from "../ui/primitives";

interface ChangeEntry extends FileChange {
  turnId: string;
}

function collectChanges(chat: Chat | undefined): ChangeEntry[] {
  if (!chat) return [];
  const byPath = new Map<string, ChangeEntry>();
  for (const m of chat.messages) {
    if (m.role !== "assistant") continue;
    for (const f of m.files ?? []) byPath.set(f.path, { ...f, turnId: m.id });
  }
  return [...byPath.values()];
}

export function Panel() {
  const tab = useApp((s) => s.panelTab);
  const openPanel = useApp((s) => s.openPanel);
  const togglePanel = useApp((s) => s.togglePanel);
  const width = useApp((s) => s.panelWidth);
  const setWidth = useApp((s) => s.setPanelWidth);
  const chat = useApp(selectChat);
  const changes = collectChanges(chat).filter((c) => !c.reverted);
  const [dragging, setDragging] = useState(false);

  const onDrag = (e: React.PointerEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = width;
    setDragging(true);
    const move = (ev: PointerEvent) => setWidth(startW + (startX - ev.clientX));
    const up = () => {
      setDragging(false);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  return (
    <motion.aside
      className={cx("panel", dragging && "resizing")}
      aria-label="审阅面板"
      initial={{ width: 0, opacity: 0 }}
      animate={{ width, opacity: 1 }}
      exit={{ width: 0, opacity: 0 }}
      transition={dragging ? { duration: 0 } : { type: "spring", stiffness: 420, damping: 40 }}
    >
      <div className="panel-resize" onPointerDown={onDrag} onDoubleClick={() => setWidth(460)} aria-hidden />
      <div className="panel-inner" style={{ width }}>
        <div className="panel-tabs" role="tablist">
          {(
            [
              ["changes", "变更", <FileDiff size={14} key="i" />],
              ["files", "文件", <FileCode2 size={14} key="i" />],
              ["preview", "预览", <Globe size={14} key="i" />],
            ] as const
          ).map(([id, label, icon]) => (
            <button key={id} role="tab" aria-selected={tab === id} className={cx("panel-tab", tab === id && "on")} onClick={() => openPanel(id)}>
              {icon}
              {label}
              {id === "changes" && changes.length > 0 && <span className="count">{changes.length}</span>}
              {tab === id && <motion.span layoutId="panel-tab-underline" className="panel-tab-underline" />}
            </button>
          ))}
          <div className="grow" />
          <button className="icon-btn" aria-label="关闭面板" onClick={() => togglePanel(false)}>
            <X size={16} />
          </button>
        </div>
        <div className="panel-body">
          {tab === "changes" && <ChangesTab chat={chat} />}
          {tab === "files" && <FilesTab chat={chat} />}
          {tab === "preview" && <PreviewTab />}
        </div>
      </div>
    </motion.aside>
  );
}

function ChangesTab({ chat }: { chat: Chat | undefined }) {
  const diffPath = useApp((s) => s.diffPath);
  const setDiffPath = useApp((s) => s.setDiffPath);
  const revertFile = useApp((s) => s.revertFile);
  const openPanel = useApp((s) => s.openPanel);
  const [mode, setMode] = useState<"unified" | "split">("unified");
  const all = collectChanges(chat);
  const changes = all.filter((c) => !c.reverted);
  const selected = changes.find((c) => c.path === diffPath) ?? changes[0];
  const add = changes.reduce((n, c) => n + c.add, 0);
  const del = changes.reduce((n, c) => n + c.del, 0);

  if (!chat || changes.length === 0) {
    return (
      <div className="panel-empty">
        <FileDiff size={28} strokeWidth={1.4} />
        <div className="panel-empty-title">还没有改动</div>
        <div className="panel-empty-sub">MiniCode 修改文件后，差异会出现在这里，可以逐个查看或撤销。</div>
      </div>
    );
  }

  return (
    <div className="changes">
      <div className="changes-head">
        <span className="changes-title">本次聊天</span>
        <span className="diffstat">
          <span className="add">+{add}</span>
          <span className="del">−{del}</span>
        </span>
        <div className="grow" />
        <Segmented
          label="差异视图"
          value={mode}
          onChange={setMode}
          options={[
            { id: "unified", label: <Rows2 size={14} aria-label="统一视图" /> },
            { id: "split", label: <Columns2 size={14} aria-label="并排视图" /> },
          ]}
        />
      </div>
      <ul className="changes-list">
        {changes.map((c) => (
          <li key={c.path}>
            <button className={cx("changes-file", c.path === selected?.path && "on")} onClick={() => setDiffPath(c.path)}>
              <FileText size={14} className="muted" />
              <span className="file-name">{basename(c.path)}</span>
              <span className="file-dir">{c.path.slice(0, c.path.length - basename(c.path).length - 1)}</span>
              <span className="diffstat">
                <span className="add">+{c.add}</span>
                <span className="del">−{c.del}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
      {selected && (
        <div className="diff-file">
          <div className="diff-file-head">
            <span className="mono diff-file-path">{selected.path}</span>
            <div className="grow" />
            <button className="icon-btn xs" data-tip="复制路径" aria-label="复制路径" onClick={() => copyText(selected.path)}>
              <Copy size={13} />
            </button>
            <button className="icon-btn xs" data-tip="在文件中打开" aria-label="在文件中打开" onClick={() => openPanel("files", selected.path)}>
              <ExternalLink size={13} />
            </button>
            <button className="btn sm ghost" onClick={() => revertFile(chat.id, selected.turnId, selected.path)}>
              <Undo2 size={13} /> 撤销
            </button>
          </div>
          <DiffView diff={selected.diff} path={selected.path} mode={mode} />
        </div>
      )}
    </div>
  );
}

interface DiffLine {
  type: "hunk" | "add" | "del" | "ctx";
  text: string;
  oldNo?: number;
  newNo?: number;
}

function parseDiff(diff: string): DiffLine[] {
  const out: DiffLine[] = [];
  let oldNo = 0;
  let newNo = 0;
  for (const raw of diff.split("\n")) {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(raw);
    if (hunk) {
      oldNo = Number(hunk[1]);
      newNo = Number(hunk[2]);
      out.push({ type: "hunk", text: raw });
    } else if (raw.startsWith("+")) out.push({ type: "add", text: raw.slice(1), newNo: newNo++ });
    else if (raw.startsWith("-")) out.push({ type: "del", text: raw.slice(1), oldNo: oldNo++ });
    else out.push({ type: "ctx", text: raw.slice(1), oldNo: oldNo++, newNo: newNo++ });
  }
  return out;
}

function DiffView({ diff, path, mode }: { diff: string; path: string; mode: "unified" | "split" }) {
  const lines = useMemo(() => parseDiff(diff), [diff]);
  const tokens = useMemo(() => {
    const body = lines.filter((l) => l.type !== "hunk");
    const tk = tokenize(body.map((l) => l.text).join("\n"), langFromPath(path));
    const map = new Map<DiffLine, (typeof tk)[number]>();
    body.forEach((l, i) => map.set(l, tk[i] ?? []));
    return map;
  }, [lines, path]);

  if (mode === "split") {
    // 把连续的删除/新增配成一行，左旧右新
    const rows: { left?: DiffLine; right?: DiffLine; hunk?: DiffLine }[] = [];
    for (let i = 0; i < lines.length; ) {
      const l = lines[i];
      if (l.type === "hunk") {
        rows.push({ hunk: l });
        i++;
      } else if (l.type === "ctx") {
        rows.push({ left: l, right: l });
        i++;
      } else {
        const dels: DiffLine[] = [];
        const adds: DiffLine[] = [];
        while (lines[i]?.type === "del") dels.push(lines[i++]);
        while (lines[i]?.type === "add") adds.push(lines[i++]);
        for (let k = 0; k < Math.max(dels.length, adds.length); k++) rows.push({ left: dels[k], right: adds[k] });
      }
    }
    return (
      <div className="diff split" role="table" aria-label="并排差异">
        {rows.map((r, i) =>
          r.hunk ? (
            <div key={i} className="diff-hunk">{r.hunk.text}</div>
          ) : (
            <div key={i} className="diff-split-row">
              {[r.left, r.right].map((l, side) => (
                <div key={side} className={cx("diff-line", l?.type, !l && "empty")}>
                  <span className="ln">{l ? (side === 0 ? l.oldNo : l.newNo) : ""}</span>
                  <span className="diff-code">{l && <Tokens tokens={tokens.get(l) ?? []} />}</span>
                </div>
              ))}
            </div>
          ),
        )}
      </div>
    );
  }

  return (
    <div className="diff" role="table" aria-label="差异">
      {lines.map((l, i) =>
        l.type === "hunk" ? (
          <div key={i} className="diff-hunk">{l.text}</div>
        ) : (
          <div key={i} className={cx("diff-line", l.type)}>
            <span className="ln">{l.oldNo ?? ""}</span>
            <span className="ln">{l.newNo ?? ""}</span>
            <span className="diff-sign">{l.type === "add" ? "+" : l.type === "del" ? "−" : ""}</span>
            <span className="diff-code">
              <Tokens tokens={tokens.get(l) ?? []} />
            </span>
          </div>
        ),
      )}
    </div>
  );
}

function FilesTab({ chat }: { chat: Chat | undefined }) {
  const viewerPath = useApp((s) => s.viewerPath);
  const setViewerPath = useApp((s) => s.setViewerPath);
  const [query, setQuery] = useState("");
  const modified = new Set(collectChanges(chat).filter((c) => !c.reverted).map((c) => c.path));

  if (viewerPath) {
    const content =
      fileContents[viewerPath] ??
      `// ${viewerPath}\n// 演示数据：这里会显示文件的完整内容。\n\nexport {};\n`;
    return (
      <div className="viewer">
        <div className="viewer-head">
          <button className="btn sm ghost" onClick={() => setViewerPath(null)}>
            <ChevronRight size={13} style={{ transform: "rotate(180deg)" }} /> 全部文件
          </button>
          <span className="mono viewer-path">{viewerPath}</span>
          {modified.has(viewerPath) && <span className="badge accent">已修改</span>}
        </div>
        <div className="viewer-body">
          <CodeBlock code={content} lang={langFromPath(viewerPath)} lineNumbers />
        </div>
      </div>
    );
  }

  return (
    <div className="tree-wrap">
      <label className="search-field">
        <Search size={14} className="muted" />
        <input placeholder="筛选文件" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="筛选文件" />
        {query && (
          <button className="icon-btn xs" aria-label="清除" onClick={() => setQuery("")}>
            <X size={12} />
          </button>
        )}
      </label>
      <div className="tree" role="tree">
        {fileTree.map((n) => (
          <TreeItem key={n.path} node={n} depth={0} query={query.toLowerCase()} modified={modified} onOpen={setViewerPath} />
        ))}
      </div>
    </div>
  );
}

function matches(node: TreeNode, q: string): boolean {
  if (!q) return true;
  if (node.name.toLowerCase().includes(q)) return true;
  return (node.children ?? []).some((c) => matches(c, q));
}

function TreeItem({
  node,
  depth,
  query,
  modified,
  onOpen,
}: {
  node: TreeNode;
  depth: number;
  query: string;
  modified: Set<string>;
  onOpen: (p: string) => void;
}) {
  const [open, setOpen] = useState(depth < 1);
  if (!matches(node, query)) return null;
  const expanded = open || !!query;
  const dirty = node.dir ? [...modified].some((m) => m.startsWith(node.path + "/")) : modified.has(node.path);
  return (
    <>
      <button
        role="treeitem"
        aria-expanded={node.dir ? expanded : undefined}
        className={cx("tree-row", dirty && "dirty")}
        style={{ paddingLeft: 10 + depth * 14 }}
        onClick={() => (node.dir ? setOpen(!open) : onOpen(node.path))}
      >
        {node.dir ? (
          <>
            <ChevronRight size={13} className={cx("chev", expanded && "open")} />
            {expanded ? <FolderOpen size={14} className="tree-icon folder" /> : <Folder size={14} className="tree-icon folder" />}
          </>
        ) : (
          <>
            <span className="tree-chev-space" />
            <FileText size={14} className="tree-icon" />
          </>
        )}
        <span className="tree-name">{node.name}</span>
        {dirty && <span className="tree-dirty">{node.dir ? "•" : "M"}</span>}
      </button>
      {node.dir && expanded && node.children?.map((c) => <TreeItem key={c.path} node={c} depth={depth + 1} query={query} modified={modified} onOpen={onOpen} />)}
    </>
  );
}

function PreviewTab() {
  const [url, setUrl] = useState("http://localhost:5173/");
  const [loading, setLoading] = useState(false);
  const [count, setCount] = useState(3);
  const inputRef = useRef<HTMLInputElement>(null);
  const reload = () => {
    setLoading(true);
    setTimeout(() => setLoading(false), 700);
  };
  return (
    <div className="preview">
      <div className="preview-bar">
        <button className="icon-btn xs" aria-label="刷新" onClick={reload}>
          {loading ? <Spinner size={13} /> : <RotateCw size={13} />}
        </button>
        <input
          ref={inputRef}
          className="preview-url mono"
          value={url}
          aria-label="地址"
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && reload()}
        />
        <button className="icon-btn xs" aria-label="在浏览器中打开" data-tip="在浏览器中打开">
          <ExternalLink size={13} />
        </button>
      </div>
      <div className="preview-frame">
        <AnimatePresence mode="wait">
          {loading ? (
            <motion.div key="l" className="preview-loading" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
              <Spinner size={18} />
            </motion.div>
          ) : (
            <motion.div key="p" className="preview-page" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
              <div className="pv-nav">
                <span className="pv-logo">Acme</span>
                <span>产品</span>
                <span>价格</span>
                <span>文档</span>
              </div>
              <div className="pv-hero">
                <h2>更快地交付你的想法</h2>
                <p>这是一个本地开发服务器的预览。改动保存后会自动刷新。</p>
                <button className="pv-btn" onClick={() => setCount((c) => c + 1)}>
                  点赞 {count}
                </button>
              </div>
              <div className="pv-cards">
                {["组件", "主题", "部署"].map((t) => (
                  <div key={t} className="pv-card">
                    <div className="pv-card-title">{t}</div>
                    <div className="pv-card-line" />
                    <div className="pv-card-line short" />
                  </div>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
