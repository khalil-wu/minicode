import { useEffect, useMemo, useRef, useState } from "react";
import { Check, CheckCircle, ChevronDown, Code2, Columns2, ExternalLink, FileDiff, GitBranch, MessageCircle, Minus, Plus, RefreshCw, RotateCcw, Rows3, X, XCircle } from "lucide-react";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { buildApprovalResponseCommand } from "../protocol/prompt-responses";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { useColorizedLines, extractFilePathFromDiff, guessLanguageFromPath } from "../lib/monaco-colorize";
import { MonacoDiffView } from "../components/MonacoDiffView";

type DiffViewMode = "unified" | "split" | "monaco";
type ChangeScope = "review" | "history" | "git";
const HISTORY_PREVIEW_LINE_LIMIT = 180;
const INLINE_COLORIZE_LINE_LIMIT = 900;
const REVIEW_PREVIEW_LINE_LIMIT = 900;
const REVIEW_FILE_INITIAL_LIMIT = 96;
const REVIEW_FILE_INCREMENT = 96;
const GIT_PREVIEW_LINE_LIMIT = 600;
const GIT_FILE_INITIAL_LIMIT = 48;
const GIT_FILE_INCREMENT = 48;

interface DiffLine {
  kind: "context" | "add" | "del" | "hunk" | "meta";
  text: string;
}

const parseUnifiedDiff = (raw: string): DiffLine[] => {
  if (!raw) return [];
  const lines = raw.split(/\r?\n/);
  const out: DiffLine[] = [];
  for (const line of lines) {
    if (line.startsWith("@@")) out.push({ kind: "hunk", text: line });
    else if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index "))
      out.push({ kind: "meta", text: line });
    else if (line.startsWith("+")) out.push({ kind: "add", text: line.slice(1) });
    else if (line.startsWith("-")) out.push({ kind: "del", text: line.slice(1) });
    else out.push({ kind: "context", text: line.startsWith(" ") ? line.slice(1) : line });
  }
  return out;
};

const colorForKind = (kind: DiffLine["kind"]): string => {
  switch (kind) {
    case "add":
      return "var(--state-success)";
    case "del":
      return "var(--state-danger)";
    case "hunk":
      return "var(--accent-primary)";
    case "meta":
      return "var(--text-muted)";
    default:
      return "var(--text-secondary)";
  }
};

const bgForKind = (kind: DiffLine["kind"]): string => {
  if (kind === "add") return "color-mix(in oklch, var(--state-success) 12%, transparent)";
  if (kind === "del") return "color-mix(in oklch, var(--state-danger) 12%, transparent)";
  if (kind === "hunk") return "var(--surface-soft)";
  return "transparent";
};

const respond = (requestId: string, approved: boolean) => {
  const protocol = useAppStore.getState().diffReview?.protocol;
  const sent = sendClientCommand(buildApprovalResponseCommand(requestId, approved ? "approve" : "reject", protocol));
  const store = useAppStore.getState();
  store.setDiffReviewState({
    ...(store.diffReview ?? { requestId, diff: "", files: [], fileDecisions: {}, lineComments: [] }),
    status: sent ? "submitted" : "error",
    error: sent ? undefined : "Connection is offline",
  });
};

export const DiffPanel = () => {
  const messages = useAppStore((s) => s.messages);
  const diffReview = useAppStore((s) => s.diffReview);
  const gitChanges = useAppStore((s) => s.gitChanges);
  const [activeScope, setActiveScope] = useState<ChangeScope>(diffReview ? "review" : "git");
  const [scopeMenuOpen, setScopeMenuOpen] = useState(false);
  const scopeMenuRef = useRef<HTMLDivElement | null>(null);
  const [diffViewMode, setDiffViewMode] = useState<DiffViewMode>("unified");

  useEffect(() => {
    if (diffReview) setActiveScope("review");
    else setActiveScope((scope) => scope === "review" ? "git" : scope);
  }, [diffReview]);

  const historySources = useMemo(() => {
    const items: { id: string; name: string; diff: string }[] = [];
    for (const m of messages) {
      for (const tc of getToolCallsFromMessage(m)) {
        const candidate =
          (tc.args as { diff?: string; patch?: string }).diff ??
          (tc.args as { patch?: string }).patch ??
          tc.diff?.patch;
        if (typeof candidate === "string" && candidate.includes("\n")) {
          items.push({ id: tc.id, name: tc.name, diff: candidate });
        } else if (
          tc.summary &&
          (tc.summary.includes("\n+") || tc.summary.includes("\n-")) &&
          /[+-]\s*\d+/.test(tc.summary)
        ) {
          items.push({ id: tc.id, name: tc.name, diff: tc.summary });
        }
      }
    }
    return items.reverse();
  }, [messages]);
  const gitChangeCount = gitChanges.workingTree.length + gitChanges.staged.length + gitChanges.untracked.length;
  const visibleScope = activeScope === "review" && !diffReview ? "git" : activeScope;
  const scopeOptions = [
    ...(diffReview ? [{ value: "review" as const, label: "Pending review", count: 1 }] : []),
    { value: "history" as const, label: "Last turn", count: historySources.length },
    { value: "git" as const, label: "Uncommitted", count: gitChangeCount },
  ];
  const visibleScopeOption = scopeOptions.find((option) => option.value === visibleScope) ?? scopeOptions[0];

  useEffect(() => {
    if (!scopeMenuOpen) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!scopeMenuRef.current?.contains(event.target as Node)) setScopeMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setScopeMenuOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [scopeMenuOpen]);

  return (
    <div className="h-full flex flex-col min-h-0">
      <div className="flex items-center gap-2 px-2 shrink-0" style={diffToolbarStyle}>
        <div ref={scopeMenuRef} className="relative inline-flex items-center gap-1.5 min-w-0" style={{ color: "var(--text-muted)" }}>
          <FileDiff size={14} />
          <button
            type="button"
            aria-label={`Diff source: ${visibleScopeOption.label}${visibleScopeOption.count ? ` ${visibleScopeOption.count}` : ""}`}
            aria-haspopup="listbox"
            aria-expanded={scopeMenuOpen}
            onClick={() => setScopeMenuOpen((open) => !open)}
            style={scopeTriggerStyle}
          >
            <span className="truncate">{visibleScopeOption.label}</span>
            {visibleScopeOption.count > 0 && (
              <span style={scopeCountStyle}>{visibleScopeOption.count}</span>
            )}
            <ChevronDown size={13} style={{ color: "var(--text-muted)", flexShrink: 0 }} />
          </button>
          {scopeMenuOpen && (
            <div role="listbox" aria-label="Diff source" style={scopeMenuStyle}>
              {scopeOptions.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="option"
                  aria-selected={visibleScope === option.value}
                  onClick={() => {
                    setActiveScope(option.value);
                    setScopeMenuOpen(false);
                  }}
                  style={scopeOptionStyle(visibleScope === option.value)}
                >
                  <span className="truncate">{option.label}</span>
                  {option.count > 0 && <span style={scopeCountStyle}>{option.count}</span>}
                </button>
              ))}
            </div>
          )}
        </div>
        <span className="flex-1" />
        {diffViewMode !== "monaco" && (
          <button
            onClick={() => setDiffViewMode(diffViewMode === "unified" ? "split" : "unified")}
            title={diffViewMode === "unified" ? "Switch to split view" : "Switch to unified view"}
            aria-label={diffViewMode === "unified" ? "Switch to split view" : "Switch to unified view"}
            className="w-6 h-[22px]"
            style={{ ...iconButtonStyle }}
          >
            {diffViewMode === "unified" ? <Columns2 size={12} /> : <Rows3 size={12} />}
          </button>
        )}
        <button
          onClick={() => setDiffViewMode(diffViewMode === "monaco" ? "unified" : "monaco")}
          title={diffViewMode === "monaco" ? "Switch to unified view" : "Switch to Monaco diff view"}
          aria-label={diffViewMode === "monaco" ? "Switch to unified view" : "Switch to Monaco diff view"}
          className="w-6 h-[22px]"
          style={{
            ...iconButtonStyle,
            background: diffViewMode === "monaco" ? "var(--surface-active)" : undefined,
            color: diffViewMode === "monaco" ? "var(--accent-primary)" : undefined,
          }}
        >
          <Code2 size={12} />
        </button>
      </div>
      <div className="flex-1 min-h-0 overflow-hidden">
        {visibleScope === "review" && <ReviewTab diffReview={diffReview} viewMode={diffViewMode} />}
        {visibleScope === "history" && <HistoryTab sources={historySources} viewMode={diffViewMode} />}
        {visibleScope === "git" && <GitChangesTab viewMode={diffViewMode} />}
      </div>
    </div>
  );
};

interface DiffLineCommentData {
  filePath: string;
  lineIndex: number;
  content: string;
}

const DiffBody = ({ lines, language, viewMode = "unified", comments, onLineClick, filePath, activeCommentLine, onCommentSubmit, onCommentCancel, rawPatch, previewLineLimit }: { lines: DiffLine[]; language?: string; viewMode?: DiffViewMode; comments?: DiffLineCommentData[]; onLineClick?: (lineIndex: number) => void; filePath?: string; activeCommentLine?: number | null; onCommentSubmit?: (lineIndex: number, text: string) => void; onCommentCancel?: () => void; rawPatch?: string; previewLineLimit?: number }) => {
  const [showFullPreview, setShowFullPreview] = useState(false);
  useEffect(() => {
    setShowFullPreview(false);
  }, [filePath, rawPatch, previewLineLimit]);
  const shouldPreview = Boolean(previewLineLimit && lines.length > previewLineLimit && !showFullPreview);
  const visibleLines = shouldPreview && previewLineLimit
    ? lines.slice(0, previewLineLimit)
    : lines;
  const hiddenLineCount = lines.length - visibleLines.length;
  if (hiddenLineCount > 0 && viewMode === "monaco") {
    return <UnifiedDiffBody lines={visibleLines} language={language} comments={comments} onLineClick={onLineClick} filePath={filePath} activeCommentLine={activeCommentLine} onCommentSubmit={onCommentSubmit} onCommentCancel={onCommentCancel} hiddenLineCount={hiddenLineCount} onShowFull={() => setShowFullPreview(true)} />;
  }
  if (viewMode === "monaco" && rawPatch) {
    const lang = language ?? guessLanguageFromPath(filePath ?? extractFilePathFromDiff(visibleLines));
    return (
      <div className="flex-1 min-h-0 overflow-hidden">
        <MonacoDiffView patch={rawPatch} language={lang} filePath={filePath} height="100%" />
      </div>
    );
  }
  if (viewMode === "monaco") {
    // Fallback: reconstruct a patch-like string from parsed lines for monaco
    const reconstructed = visibleLines
      .map((l) => {
        if (l.kind === "add") return `+${l.text}`;
        if (l.kind === "del") return `-${l.text}`;
        if (l.kind === "hunk" || l.kind === "meta") return l.text;
        return ` ${l.text}`;
      })
      .join("\n");
    const lang = language ?? guessLanguageFromPath(filePath ?? extractFilePathFromDiff(visibleLines));
    return (
      <div className="flex-1 min-h-0 overflow-hidden">
        <MonacoDiffView patch={reconstructed} language={lang} filePath={filePath} height="100%" />
      </div>
    );
  }
  if (viewMode === "split") return <SplitDiffBody lines={visibleLines} language={language} comments={comments} onLineClick={onLineClick} filePath={filePath} activeCommentLine={activeCommentLine} onCommentSubmit={onCommentSubmit} onCommentCancel={onCommentCancel} hiddenLineCount={hiddenLineCount} onShowFull={() => setShowFullPreview(true)} />;
  return <UnifiedDiffBody lines={visibleLines} language={language} comments={comments} onLineClick={onLineClick} filePath={filePath} activeCommentLine={activeCommentLine} onCommentSubmit={onCommentSubmit} onCommentCancel={onCommentCancel} hiddenLineCount={hiddenLineCount} onShowFull={() => setShowFullPreview(true)} />;
};

const UnifiedDiffBody = ({ lines, language, comments, onLineClick, filePath, activeCommentLine, onCommentSubmit, onCommentCancel, hiddenLineCount = 0, onShowFull }: { lines: DiffLine[]; language?: string; comments?: DiffLineCommentData[]; onLineClick?: (lineIndex: number) => void; filePath?: string; activeCommentLine?: number | null; onCommentSubmit?: (lineIndex: number, text: string) => void; onCommentCancel?: () => void; hiddenLineCount?: number; onShowFull?: () => void }) => {
  const lang = language ?? guessLanguageFromPath(extractFilePathFromDiff(lines));
  const colorized = useColorizedLines(lines.length <= INLINE_COLORIZE_LINE_LIMIT ? lines : [], lang);
  const commentMap = useMemo(() => {
    if (!comments) return new Map<number, DiffLineCommentData>();
    const map = new Map<number, DiffLineCommentData>();
    for (const c of comments) {
      if (!filePath || c.filePath === filePath) map.set(c.lineIndex, c);
    }
    return map;
  }, [comments, filePath]);

  return (
    <div className="flex-1 min-h-0 overflow-auto leading-[1.55]" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>
      {lines.map((line, i) => (
        <div key={i}>
          <div
            className="px-2.5 whitespace-pre-wrap break-words relative"
            style={{
              background: bgForKind(line.kind),
              borderLeft:
                line.kind === "add"
                  ? "2px solid var(--state-success)"
                  : line.kind === "del"
                    ? "2px solid var(--state-danger)"
                    : "2px solid transparent",
              color: (line.kind === "hunk" || line.kind === "meta" || !colorized?.[i]) ? colorForKind(line.kind) : undefined,
              cursor: (line.kind === "add" || line.kind === "del" || line.kind === "context") && onLineClick ? "pointer" : undefined,
            }}
            onClick={() => {
              if (onLineClick && (line.kind === "add" || line.kind === "del" || line.kind === "context")) {
                onLineClick(i);
              }
            }}
          >
            <span className="select-none" style={{ color: colorForKind(line.kind) }}>
              {line.kind === "add" ? "+" : line.kind === "del" ? "-" : " "}
            </span>
            {line.kind === "context" && colorized?.[i] ? (
              <span dangerouslySetInnerHTML={{ __html: colorized[i] }} />
            ) : (
              line.text
            )}
            {commentMap.has(i) && (
              <span className="ml-2 inline align-middle" style={{ color: "var(--accent-primary)", fontSize: "var(--text-xs)" }} title={commentMap.get(i)!.content}>
                <MessageCircle size={12} style={{ display: "inline", verticalAlign: "middle" }} />
              </span>
            )}
          </div>
          {commentMap.has(i) && (
            <div className="py-1 px-2.5 pl-3.5" style={{ background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-base))", borderLeft: "3px solid var(--accent-primary)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
              {commentMap.get(i)!.content}
            </div>
          )}
          {activeCommentLine === i && onCommentSubmit && (
            <InlineCommentInput lineIndex={i} onSubmit={onCommentSubmit} onCancel={onCommentCancel} />
          )}
        </div>
      ))}
      {hiddenLineCount > 0 && <DiffTruncationNotice hiddenLineCount={hiddenLineCount} onShowFull={onShowFull} />}
    </div>
  );
};

interface SplitRow {
  left: DiffLine | null;
  right: DiffLine | null;
  leftIndex?: number;
  rightIndex?: number;
}

const InlineCommentInput = ({ lineIndex, onSubmit, onCancel }: { lineIndex: number; onSubmit: (lineIndex: number, text: string) => void; onCancel?: () => void }) => {
  const [text, setText] = useState("");
  return (
    <div className="flex items-center gap-1.5 py-1.5 px-2.5 pl-3.5" style={{ borderLeft: "3px solid var(--accent-primary)", background: "color-mix(in oklch, var(--accent-primary) 5%, var(--surface-base))" }}>
      <input
        type="text"
        placeholder={`Comment on line ${lineIndex + 1}...`}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && text.trim()) { e.preventDefault(); onSubmit(lineIndex, text.trim()); } if (e.key === "Escape") onCancel?.(); }}
        className="flex-1 px-2 py-1 outline-none"
        style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", fontSize: "var(--text-xs)", background: "var(--surface-base)", color: "var(--text-primary)" }}
        autoFocus
      />
      <button onClick={() => { if (text.trim()) onSubmit(lineIndex, text.trim()); }} className="px-2 py-0.5 cursor-pointer" style={{ border: "1px solid var(--accent-primary)", background: "var(--accent-primary)", color: "var(--text-on-accent)", borderRadius: "var(--radius-sm, 4px)", fontSize: "var(--text-xs)" }}>Add</button>
      <button onClick={onCancel} className="px-2 py-0.5 cursor-pointer" style={{ border: "1px solid var(--border-subtle)", background: "transparent", color: "var(--text-muted)", borderRadius: "var(--radius-sm, 4px)", fontSize: "var(--text-xs)" }}>Cancel</button>
    </div>
  );
};

const SplitDiffBody = ({ lines, language, comments, onLineClick, filePath, activeCommentLine, onCommentSubmit, onCommentCancel, hiddenLineCount = 0, onShowFull }: { lines: DiffLine[]; language?: string; comments?: DiffLineCommentData[]; onLineClick?: (lineIndex: number) => void; filePath?: string; activeCommentLine?: number | null; onCommentSubmit?: (lineIndex: number, text: string) => void; onCommentCancel?: () => void; hiddenLineCount?: number; onShowFull?: () => void }) => {
  const lang = language ?? guessLanguageFromPath(extractFilePathFromDiff(lines));
  const colorized = useColorizedLines(lines.length <= INLINE_COLORIZE_LINE_LIMIT ? lines : [], lang);
  const rows = useMemo(() => {
    const result: SplitRow[] = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (line.kind === "meta" || line.kind === "hunk") {
        result.push({ left: line, right: line, leftIndex: i, rightIndex: i });
        i++;
      } else if (line.kind === "context") {
        result.push({ left: line, right: line, leftIndex: i, rightIndex: i });
        i++;
      } else if (line.kind === "del") {
        const delStart = i;
        while (i < lines.length && lines[i].kind === "del") i++;
        const addStart = i;
        while (i < lines.length && lines[i].kind === "add") i++;
        const dels = lines.slice(delStart, addStart);
        const adds = lines.slice(addStart, i);
        const maxLen = Math.max(dels.length, adds.length);
        for (let j = 0; j < maxLen; j++) {
          result.push({
            left: dels[j] ?? null,
            right: adds[j] ?? null,
            leftIndex: dels[j] ? delStart + j : undefined,
            rightIndex: adds[j] ? addStart + j : undefined,
          });
        }
      } else if (line.kind === "add") {
        result.push({ left: null, right: line, rightIndex: i });
        i++;
      } else {
        i++;
      }
    }
    return result;
  }, [lines]);

  const commentMap = useMemo(() => {
    if (!comments) return new Map<number, DiffLineCommentData>();
    const map = new Map<number, DiffLineCommentData>();
    for (const c of comments) {
      if (!filePath || c.filePath === filePath) map.set(c.lineIndex, c);
    }
    return map;
  }, [comments, filePath]);

  const colorizedMap = useMemo(() => {
    if (!colorized) return null;
    const map = new Map<DiffLine, string>();
    lines.forEach((line, i) => {
      if (colorized[i]) map.set(line, colorized[i]);
    });
    return map;
  }, [colorized, lines]);

  return (
    <div className="flex-1 min-h-0 overflow-auto leading-[1.55]" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>
      {rows.map((row, i) => {
        const lineIdx = row.rightIndex ?? row.leftIndex;
        const hasComment = lineIdx != null && commentMap.has(lineIdx);
        const isActiveComment = lineIdx != null && activeCommentLine === lineIdx;
        return (
          <div key={i}>
            <div className="grid grid-cols-2">
              <SplitRowPair row={row} colorizedMap={colorizedMap} onLineClick={onLineClick} />
            </div>
            {hasComment && (
              <div className="py-1 px-2.5 pl-3.5" style={{ background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-base))", borderLeft: "3px solid var(--accent-primary)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
                {commentMap.get(lineIdx!)!.content}
              </div>
            )}
            {isActiveComment && onCommentSubmit && (
              <InlineCommentInput lineIndex={lineIdx!} onSubmit={onCommentSubmit} onCancel={onCommentCancel} />
            )}
          </div>
        );
      })}
      {hiddenLineCount > 0 && <DiffTruncationNotice hiddenLineCount={hiddenLineCount} onShowFull={onShowFull} />}
    </div>
  );
};

const DiffTruncationNotice = ({ hiddenLineCount, onShowFull }: { hiddenLineCount: number; onShowFull?: () => void }) => (
  <div
    className="px-2.5 py-2 flex items-center gap-2"
    style={{
      borderTop: "1px solid var(--border-subtle)",
      color: "var(--text-muted)",
      fontFamily: "var(--font-ui)",
      fontSize: "var(--text-xs)",
      background: "var(--surface-page)",
    }}
  >
    <span className="flex-1">Showing preview. {hiddenLineCount.toLocaleString()} more diff lines hidden.</span>
    {onShowFull && (
      <button type="button" onClick={onShowFull} style={smallActionButtonStyle}>
        Show full diff
      </button>
    )}
  </div>
);

const SplitRowPair = ({ row, colorizedMap, onLineClick }: { row: SplitRow; colorizedMap: Map<DiffLine, string> | null; onLineClick?: (lineIndex: number) => void }) => {
  const renderCell = (line: DiffLine | null, side: "left" | "right", lineIndex?: number) => {
    if (!line) {
      return <div className="px-2 min-h-[1.55em]" style={{ background: "var(--surface-soft)" }} />;
    }
    if (line.kind === "hunk" || line.kind === "meta") {
      return (
        <div className="px-2 whitespace-pre-wrap break-words" style={{ background: bgForKind(line.kind), color: colorForKind(line.kind) }}>
          {line.text}
        </div>
      );
    }
    const bg = side === "left" && line.kind === "del"
      ? "color-mix(in oklch, var(--state-danger) 12%, transparent)"
      : side === "right" && line.kind === "add"
        ? "color-mix(in oklch, var(--state-success) 12%, transparent)"
        : "transparent";
    const html = colorizedMap?.get(line);
    const clickable = onLineClick && lineIndex != null && (line.kind === "add" || line.kind === "del" || line.kind === "context");
    return (
      <div
        className="px-2 whitespace-pre-wrap break-words"
        style={{ background: bg, cursor: clickable ? "pointer" : undefined }}
        onClick={clickable ? () => onLineClick(lineIndex) : undefined}
      >
        {line.kind === "context" && html ? (
          <span dangerouslySetInnerHTML={{ __html: html }} />
        ) : (
          <span style={{ color: colorForKind(line.kind) }}>{line.text}</span>
        )}
      </div>
    );
  };

  return (
    <>
      {renderCell(row.left, "left", row.leftIndex)}
      {renderCell(row.right, "right", row.rightIndex)}
    </>
  );
};

const iconButtonStyle: React.CSSProperties = {
  width: 26,
  height: 24,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "transparent",
  color: "var(--text-muted)",
  cursor: "pointer",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 0,
};

const rejectButtonStyle: React.CSSProperties = {
  background: "var(--surface-soft)",
  color: "var(--text-primary)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  padding: "3px 9px",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
};

const acceptButtonStyle: React.CSSProperties = {
  ...rejectButtonStyle,
  background: "var(--state-success)",
  border: "1px solid var(--state-success)",
  color: "var(--text-on-accent)",
};

const smallActionButtonStyle: React.CSSProperties = {
  ...rejectButtonStyle,
  padding: "3px 7px",
  gap: 4,
  height: 22,
  whiteSpace: "nowrap",
};

const showMoreButtonStyle: React.CSSProperties = {
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-soft)",
  color: "var(--text-muted)",
  cursor: "pointer",
  fontSize: "var(--text-xs)",
};

const fileDecisionBtnStyle: React.CSSProperties = {
  width: 22,
  height: 22,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  cursor: "pointer",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 0,
  flexShrink: 0,
};

const diffToolbarStyle: React.CSSProperties = {
  minHeight: 38,
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--surface-page)",
};

const scopeTriggerStyle: React.CSSProperties = {
  minHeight: 28,
  maxWidth: 210,
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
  background: "var(--surface-base)",
  color: "var(--text-primary)",
  fontSize: "var(--text-sm)",
  fontWeight: 700,
  padding: "0 8px",
  cursor: "pointer",
  boxShadow: "0 1px 1px color-mix(in oklch, var(--text-primary) 6%, transparent)",
};

const scopeMenuStyle: React.CSSProperties = {
  position: "absolute",
  top: "calc(100% + 5px)",
  left: 20,
  zIndex: 20,
  width: 210,
  padding: 4,
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
  background: "var(--surface-raised)",
  boxShadow: "var(--shadow-strong, var(--shadow-md))",
};

const scopeOptionStyle = (active: boolean): React.CSSProperties => ({
  width: "100%",
  minHeight: 28,
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "0 8px",
  border: 0,
  borderRadius: "var(--radius-sm, 5px)",
  background: active ? "var(--surface-active)" : "transparent",
  color: active ? "var(--text-primary)" : "var(--text-secondary)",
  cursor: "pointer",
  fontSize: "var(--text-sm)",
  fontWeight: active ? 700 : 500,
  textAlign: "left",
});

const scopeCountStyle: React.CSSProperties = {
  minWidth: 18,
  height: 18,
  padding: "0 5px",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  borderRadius: 999,
  background: "color-mix(in oklch, var(--accent-primary) 12%, transparent)",
  border: "1px solid color-mix(in oklch, var(--accent-primary) 28%, transparent)",
  color: "var(--accent-primary)",
  fontFamily: "var(--font-mono)",
  fontSize: 10,
  flexShrink: 0,
};

// ── Review Tab ───────────────────────────────────────────────────

import type { DiffReviewState } from "../stores/types";

const ReviewTab = ({ diffReview, viewMode }: { diffReview: DiffReviewState | null; viewMode: DiffViewMode }) => {
  if (!diffReview) {
    return (
      <div className="flex-1 grid place-items-center p-4" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
        No pending review. Diffs requiring approval will appear here.
      </div>
    );
  }

  return <ActiveReviewTab diffReview={diffReview} viewMode={viewMode} />;
};

const ActiveReviewTab = ({ diffReview, viewMode }: { diffReview: DiffReviewState; viewMode: DiffViewMode }) => {
  const [commentLineIndex, setCommentLineIndex] = useState<number | null>(null);
  const [visibleFileLimit, setVisibleFileLimit] = useState(REVIEW_FILE_INITIAL_LIMIT);

  useEffect(() => {
    setVisibleFileLimit(REVIEW_FILE_INITIAL_LIMIT);
    setCommentLineIndex(null);
  }, [diffReview.requestId]);

  const selectedFile = useMemo(
    () => diffReview.files.find((file) => file.path === diffReview.selectedPath),
    [diffReview.files, diffReview.selectedPath],
  );
  const diff = selectedFile?.patch || diffReview.diff;
  const parsed = useMemo(() => parseUnifiedDiff(diff), [diff]);
  const { plus, minus } = useMemo(
    () => parsed.reduce(
      (acc, line) => {
        if (line.kind === "add") acc.plus += 1;
        if (line.kind === "del") acc.minus += 1;
        return acc;
      },
      { plus: 0, minus: 0 },
    ),
    [parsed],
  );
  const needsFetch = selectedFile && !selectedFile.patch;
  const comments = diffReview.lineComments ?? [];
  const isReadOnly = diffReview.mode === "view" || diffReview.status === "viewing";
  const isSubmitted = diffReview.status === "submitted";
  const allFilesDecided = Object.keys(diffReview.fileDecisions ?? {}).length >= diffReview.files.length;
  const visibleFiles = useMemo(
    () => diffReview.files.slice(0, visibleFileLimit),
    [diffReview.files, visibleFileLimit],
  );
  const hiddenFileCount = Math.max(0, diffReview.files.length - visibleFiles.length);
  const handleLineClick = (lineIndex: number) => {
    setCommentLineIndex(commentLineIndex === lineIndex ? null : lineIndex);
  };

  return (
    <div className="h-full grid min-h-0 overflow-hidden" style={{ gridTemplateColumns: diffReview.files.length ? "minmax(220px, 320px) 1fr" : "1fr" }}>
      {diffReview.files.length > 0 && (
        <aside className="min-h-0 overflow-hidden p-2.5 flex flex-col" style={{ borderRight: "1px solid var(--border-subtle)" }}>
          <div className="flex items-center gap-2 mb-2.5" style={{ fontSize: "var(--text-xs)" }}>
            <FileDiff size={14} color="var(--accent-primary)" />
            <span className="flex-1 font-bold" style={{ color: "var(--text-primary)" }}>{isReadOnly ? "Diff" : "Approval Diff"}</span>
            <span style={{ color: "var(--text-muted)" }}>{diffReview.files.length}</span>
          </div>
          <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden">
            {visibleFiles.map((file) => {
              const decision = diffReview.fileDecisions?.[file.path];
              return (
                <div key={file.path} className="flex items-center gap-1 mb-0.5">
                  <button
                    onClick={() => {
                      useAppStore.getState().setDiffReviewSelectedPath(file.path);
                      if (!file.patch) {
                        sendClientCommand({ type: "approval.file_diff", tool_call_id: diffReview.requestId, path: file.path });
                      }
                    }}
                    title={file.path}
                    className="flex-1 min-w-0 border-0 cursor-pointer text-left px-1.5 py-1.5"
                    style={{
                      borderRadius: "var(--radius-sm, 4px)",
                      background: file.path === diffReview.selectedPath ? "var(--surface-active)" : "transparent",
                      color: "var(--text-secondary)",
                      fontSize: "var(--text-xs)",
                    }}
                  >
                    <div className="overflow-hidden text-ellipsis whitespace-nowrap" style={{ fontFamily: "var(--font-mono)" }}>{file.path}</div>
                    <div className="flex gap-2 mt-0.5" style={{ color: "var(--text-muted)" }}>
                      {file.additions != null && <span style={{ color: "var(--state-success)" }}>+{file.additions}</span>}
                      {file.deletions != null && <span style={{ color: "var(--state-danger)" }}>-{file.deletions}</span>}
                      {file.isLarge && <span>大文件</span>}
                    </div>
                  </button>
                  {!isReadOnly && (
                    <>
                      <button
                        title="接受文件"
                        aria-label={`接受文件 ${file.path}`}
                        onClick={(e) => { e.stopPropagation(); useAppStore.getState().setDiffFileDecision(file.path, "approved"); }}
                        style={{ ...fileDecisionBtnStyle, color: decision === "approved" ? "var(--text-on-accent)" : "var(--state-success)", background: decision === "approved" ? "var(--state-success)" : "transparent" }}
                      >
                        <CheckCircle size={13} />
                      </button>
                      <button
                        title="拒绝文件"
                        aria-label={`拒绝文件 ${file.path}`}
                        onClick={(e) => { e.stopPropagation(); useAppStore.getState().setDiffFileDecision(file.path, "rejected"); }}
                        style={{ ...fileDecisionBtnStyle, color: decision === "rejected" ? "var(--text-on-accent)" : "var(--state-danger)", background: decision === "rejected" ? "var(--state-danger)" : "transparent" }}
                      >
                        <XCircle size={13} />
                      </button>
                    </>
                  )}
                </div>
              );
            })}
            {hiddenFileCount > 0 && (
              <button
                type="button"
                onClick={() => setVisibleFileLimit((limit) => limit + REVIEW_FILE_INCREMENT)}
                className="w-full mt-1 px-2 py-1.5"
                style={{
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "var(--radius-sm, 4px)",
                  background: "var(--surface-soft)",
                  color: "var(--text-muted)",
                  cursor: "pointer",
                  fontSize: "var(--text-xs)",
                }}
              >
                再显示 {Math.min(REVIEW_FILE_INCREMENT, hiddenFileCount)} 个文件
              </button>
            )}
          </div>
          {!isReadOnly && Object.keys(diffReview.fileDecisions ?? {}).length > 0 && (
            <button
              onClick={() => useAppStore.getState().submitPartialApproval()}
              disabled={!allFilesDecided || isSubmitted}
              className="mt-2.5 w-full px-2.5 py-1.5"
              style={{
                border: "1px solid var(--accent-primary)",
                borderRadius: "var(--radius-sm, 4px)",
                background: allFilesDecided ? "var(--accent-primary)" : "transparent",
                color: allFilesDecided ? "var(--text-on-accent)" : "var(--accent-primary)",
                fontSize: "var(--text-xs)",
                cursor: allFilesDecided && !isSubmitted ? "pointer" : "not-allowed",
                opacity: allFilesDecided && !isSubmitted ? 1 : 0.5,
              }}
            >
              {isSubmitted ? "提交中..." : `提交审查 (${Object.keys(diffReview.fileDecisions ?? {}).length}/${diffReview.files.length})`}
            </button>
          )}
        </aside>
      )}

      <main className="min-w-0 min-h-0 overflow-hidden flex flex-col">
        <div className="flex items-center gap-2 px-2.5 py-[7px] shrink-0" style={{ borderBottom: "1px solid var(--border-subtle)", background: "var(--surface-page)", fontSize: "var(--text-xs)" }}>
          <span className="font-bold" style={{ color: "var(--text-primary)" }}>{isReadOnly ? (diffReview.toolName || "Tool") : `${diffReview.toolName || "Tool"} approval`}</span>
          <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{diffReview.requestId.slice(0, 8)}</span>
          {plus > 0 && <span style={{ color: "var(--state-success)" }}>+{plus}</span>}
          {minus > 0 && <span style={{ color: "var(--state-danger)" }}>-{minus}</span>}
          <span className="flex-1" />
          {selectedFile && (
            <button
              onClick={() => useAppStore.getState().openEditorFile(selectedFile.path, selectedFile.path.split(/[/\\]/).pop())}
              title="Open file in editor" aria-label="Open file in editor" style={iconButtonStyle}
            >
              <ExternalLink size={13} />
            </button>
          )}
          {diffReview.status === "error" && diffReview.error && (
            <span style={{ color: "var(--state-danger)" }}>{diffReview.error}</span>
          )}
          {!isReadOnly && isSubmitted && <span style={{ color: "var(--text-muted)" }}>已提交</span>}
          {!isReadOnly && <button disabled={isSubmitted} onClick={() => respond(diffReview.requestId, false)} style={{ ...rejectButtonStyle, opacity: isSubmitted ? 0.6 : 1 }}><X size={13} /> 全部拒绝</button>}
          {!isReadOnly && <button disabled={isSubmitted} onClick={() => respond(diffReview.requestId, true)} style={{ ...acceptButtonStyle, opacity: isSubmitted ? 0.6 : 1 }}><Check size={13} /> 全部接受</button>}
          {!isReadOnly && comments.length > 0 && (
            <button disabled={isSubmitted} onClick={() => useAppStore.getState().submitDiffReviewWithComments()} style={{ ...acceptButtonStyle, background: "var(--accent-primary)", borderColor: "var(--accent-primary)", marginLeft: 4, opacity: isSubmitted ? 0.6 : 1 }}>
              <MessageCircle size={13} /> 提交意见 ({comments.length})
            </button>
          )}
        </div>
        {needsFetch ? (
          <div className="flex-1 grid place-items-center" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>正在加载文件差异...</div>
        ) : (
          <DiffBody
            lines={parsed}
            viewMode={viewMode}
            rawPatch={diff}
            comments={comments}
            onLineClick={handleLineClick}
            filePath={diffReview.selectedPath}
            activeCommentLine={commentLineIndex}
            onCommentSubmit={isReadOnly ? undefined : (lineIndex, text) => {
              useAppStore.getState().addDiffLineComment({
                filePath: diffReview.selectedPath ?? diffReview.files[0]?.path ?? "diff",
                lineIndex,
                content: text,
              });
              setCommentLineIndex(null);
            }}
            onCommentCancel={isReadOnly ? undefined : () => setCommentLineIndex(null)}
            previewLineLimit={isReadOnly ? REVIEW_PREVIEW_LINE_LIMIT : undefined}
          />
        )}
      </main>
    </div>
  );
};

// ── History Tab ──────────────────────────────────────────────────

const HistoryTab = ({ sources, viewMode }: { sources: { id: string; name: string; diff: string }[]; viewMode: DiffViewMode }) => {
  const [expandedId, setExpandedId] = useState<string | null>(sources[0]?.id ?? null);
  if (sources.length === 0) {
    return (
      <div className="flex-1 grid place-items-center p-4" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
        No diffs yet. When the agent edits files, their unified diffs will show up here.
      </div>
    );
  }

  return (
    <div className="h-full min-h-0 overflow-y-auto p-3 flex flex-col gap-4">
      {sources.map((d) => {
        const parsed = parseUnifiedDiff(d.diff);
        const plus = parsed.filter((l) => l.kind === "add").length;
        const minus = parsed.filter((l) => l.kind === "del").length;
        const expanded = expandedId === d.id;
        return (
          <div key={d.id} className="overflow-hidden shrink-0" style={{ background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 6px)" }}>
            <div className="flex items-center gap-2.5 px-2.5 py-1.5" style={{ background: "var(--surface-page)", borderBottom: "1px solid var(--border-subtle)", fontSize: "var(--text-xs)" }}>
              <span style={{ fontFamily: "var(--font-mono)", color: "var(--accent-primary)" }}>{d.name}</span>
              <span style={{ color: "var(--text-muted)" }}>{d.id.slice(0, 8)}</span>
              <span style={{ color: "var(--text-muted)" }}>{parsed.length.toLocaleString()} lines</span>
              <span className="flex-1" />
              {plus > 0 && <span style={{ color: "var(--state-success)" }}>+{plus}</span>}
              {minus > 0 && <span style={{ color: "var(--state-danger)" }}>-{minus}</span>}
              <button
                type="button"
                onClick={() => setExpandedId(expanded ? null : d.id)}
                style={smallActionButtonStyle}
              >
                {expanded ? "Collapse" : "Preview"}
              </button>
            </div>
            {expanded && (
              <div style={{ maxHeight: "min(58vh, 760px)", display: "flex", minHeight: 0 }}>
                <DiffBody
                  lines={parsed}
                  viewMode={viewMode}
                  rawPatch={d.diff}
                  previewLineLimit={HISTORY_PREVIEW_LINE_LIMIT}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
};

// ── Working Tree Tab ─────────────────────────────────────────────

const GitChangesTab = ({ viewMode }: { viewMode: DiffViewMode }) => {
  const gitChanges = useAppStore((s) => s.gitChanges);
  const requestGitChanges = useAppStore((s) => s.requestGitChanges);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [visibleGitLimits, setVisibleGitLimits] = useState({
    staged: GIT_FILE_INITIAL_LIMIT,
    working: GIT_FILE_INITIAL_LIMIT,
    untracked: GIT_FILE_INITIAL_LIMIT,
  });

  useEffect(() => {
    requestGitChanges();
  }, [requestGitChanges]);

  useEffect(() => {
    setVisibleGitLimits({
      staged: GIT_FILE_INITIAL_LIMIT,
      working: GIT_FILE_INITIAL_LIMIT,
      untracked: GIT_FILE_INITIAL_LIMIT,
    });
  }, [gitChanges.staged.length, gitChanges.workingTree.length, gitChanges.untracked.length]);

  const allFiles = useMemo(() => {
    const staged = gitChanges.staged.map((f) => ({ ...f, section: "staged" as const }));
    const working = gitChanges.workingTree.map((f) => ({ ...f, section: "working" as const }));
    return [...staged, ...working];
  }, [gitChanges.staged, gitChanges.workingTree]);

  const selectedPatch = useMemo(() => {
    if (!selectedFile) return null;
    const file = allFiles.find((f) => f.path === selectedFile);
    return file?.patch ?? null;
  }, [selectedFile, allFiles]);
  const selectedPatchLines = useMemo(
    () => selectedPatch ? parseUnifiedDiff(selectedPatch) : [],
    [selectedPatch],
  );
  const visibleStaged = useMemo(
    () => gitChanges.staged.slice(0, visibleGitLimits.staged),
    [gitChanges.staged, visibleGitLimits.staged],
  );
  const visibleWorking = useMemo(
    () => gitChanges.workingTree.slice(0, visibleGitLimits.working),
    [gitChanges.workingTree, visibleGitLimits.working],
  );
  const visibleUntracked = useMemo(
    () => gitChanges.untracked.slice(0, visibleGitLimits.untracked),
    [gitChanges.untracked, visibleGitLimits.untracked],
  );
  const hiddenStagedCount = Math.max(0, gitChanges.staged.length - visibleStaged.length);
  const hiddenWorkingCount = Math.max(0, gitChanges.workingTree.length - visibleWorking.length);
  const hiddenUntrackedCount = Math.max(0, gitChanges.untracked.length - visibleUntracked.length);

  const handleStage = (path: string) => {
    sendClientCommand({ type: "diff.git_stage_file", path });
  };

  const handleUnstage = (path: string) => {
    sendClientCommand({ type: "diff.git_unstage_file", path });
  };

  const handleStageAll = () => {
    sendClientCommand({ type: "diff.git_stage_all" });
  };

  const handleUnstageAll = () => {
    sendClientCommand({ type: "diff.git_unstage_all" });
  };

  const handleRevert = async (path: string) => {
    const { showConfirm } = await import("../overlays/DialogService");
    const confirmed = await showConfirm({
      title: "Discard file changes",
      message: `Discard local changes in ${path}? This cannot be undone.`,
      confirmLabel: "Discard",
      cancelLabel: "Cancel",
      danger: true,
    });
    if (confirmed) {
      sendClientCommand({ type: "diff.git_revert_file", path });
    }
  };

  if (gitChanges.loading && allFiles.length === 0) {
    return (
      <div className="flex-1 grid place-items-center" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
        Loading git changes...
      </div>
    );
  }

  if (allFiles.length === 0 && gitChanges.untracked.length === 0) {
    return (
      <div className="flex-1 grid place-items-center p-4" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
        No uncommitted changes.
      </div>
    );
  }

  const hasWorkingChanges = gitChanges.workingTree.length + gitChanges.untracked.length > 0;
  const hasStagedChanges = gitChanges.staged.length > 0;
  const showMoreGitFiles = (section: keyof typeof visibleGitLimits, hiddenCount: number, label: string) => (
    hiddenCount > 0 ? (
      <button
        type="button"
        onClick={() => setVisibleGitLimits((limits) => ({
          ...limits,
          [section]: limits[section] + GIT_FILE_INCREMENT,
        }))}
        className="w-full mt-1 px-2 py-1.5"
        style={showMoreButtonStyle}
      >
        Show {Math.min(GIT_FILE_INCREMENT, hiddenCount)} more {label}
      </button>
    ) : null
  );

  return (
    <div className="h-full grid min-h-0 overflow-hidden" style={{ gridTemplateColumns: "minmax(200px, 280px) 1fr" }}>
      <aside className="min-h-0 overflow-y-auto overflow-x-hidden p-2.5 flex flex-col gap-2" style={{ borderRight: "1px solid var(--border-subtle)" }}>
        <div className="flex flex-col gap-1.5" style={{ fontSize: "var(--text-xs)" }}>
          <div className="flex items-center gap-1.5">
            <GitBranch size={13} color="var(--accent-primary)" />
            <span className="flex-1 font-bold" style={{ color: "var(--text-primary)" }}>未提交更改</span>
            <button onClick={requestGitChanges} title="Refresh" aria-label="Refresh git changes" className="w-[22px] h-5" style={iconButtonStyle}>
              <RefreshCw size={11} className={gitChanges.loading ? "spin" : ""} />
            </button>
          </div>
          {(hasWorkingChanges || hasStagedChanges) && (
            <div className="flex items-center gap-1.5 flex-wrap">
              {hasWorkingChanges && (
                <button
                  onClick={handleStageAll}
                  title="Stage all"
                  aria-label="Stage all"
                  style={{ ...smallActionButtonStyle, color: "var(--state-success)" }}
                >
                  <Plus size={11} />
                  全部暂存
                </button>
              )}
              {hasStagedChanges && (
                <button
                  onClick={handleUnstageAll}
                  title="Unstage all"
                  aria-label="Unstage all"
                  style={{ ...smallActionButtonStyle, color: "var(--text-muted)" }}
                >
                  <Minus size={11} />
                  全部取消暂存
                </button>
              )}
            </div>
          )}
        </div>

        {gitChanges.staged.length > 0 && (
          <div>
            <div className="uppercase mb-1 tracking-wide" style={{ fontSize: 10, color: "var(--text-muted)", letterSpacing: "0.5px" }}>
              已暂存 ({gitChanges.staged.length})
            </div>
            {visibleStaged.map((f) => (
              <div key={`staged-${f.path}`} className="flex items-center gap-1 mb-0.5">
                <button
                  onClick={() => setSelectedFile(f.path)}
                  className="flex-1 min-w-0 border-0 cursor-pointer text-left px-1.5 py-1"
                  style={{
                    borderRadius: "var(--radius-sm, 4px)",
                    background: selectedFile === f.path ? "var(--surface-active)" : "transparent",
                    color: "var(--text-secondary)",
                    fontSize: "var(--text-xs)",
                  }}
                >
                  <div className="overflow-hidden text-ellipsis whitespace-nowrap" style={{ fontFamily: "var(--font-mono)" }}>{f.path}</div>
                  <div className="flex gap-1.5 mt-px">
                    <span style={{ color: "var(--state-success)", fontSize: 10 }}>+{f.additions}</span>
                    <span style={{ color: "var(--state-danger)", fontSize: 10 }}>-{f.deletions}</span>
                  </div>
                </button>
                <button onClick={() => handleUnstage(f.path)} title="Unstage" aria-label={`Unstage ${f.path}`} style={{ ...fileDecisionBtnStyle, color: "var(--text-muted)" }}>
                  <Minus size={11} />
                </button>
              </div>
            ))}
            {showMoreGitFiles("staged", hiddenStagedCount, "staged files")}
          </div>
        )}

        {gitChanges.workingTree.length > 0 && (
          <div>
            <div className="uppercase mb-1 tracking-wide" style={{ fontSize: 10, color: "var(--text-muted)", letterSpacing: "0.5px" }}>
              已修改 ({gitChanges.workingTree.length})
            </div>
            {visibleWorking.map((f) => (
              <div key={`wt-${f.path}`} className="flex items-center gap-1 mb-0.5">
                <button
                  onClick={() => setSelectedFile(f.path)}
                  className="flex-1 min-w-0 border-0 cursor-pointer text-left px-1.5 py-1"
                  style={{
                    borderRadius: "var(--radius-sm, 4px)",
                    background: selectedFile === f.path ? "var(--surface-active)" : "transparent",
                    color: "var(--text-secondary)",
                    fontSize: "var(--text-xs)",
                  }}
                >
                  <div className="overflow-hidden text-ellipsis whitespace-nowrap" style={{ fontFamily: "var(--font-mono)" }}>{f.path}</div>
                  <div className="flex gap-1.5 mt-px">
                    <span style={{ color: "var(--state-success)", fontSize: 10 }}>+{f.additions}</span>
                    <span style={{ color: "var(--state-danger)", fontSize: 10 }}>-{f.deletions}</span>
                  </div>
                </button>
                <button onClick={() => handleRevert(f.path)} title="Discard changes" aria-label={`Discard changes in ${f.path}`} style={{ ...fileDecisionBtnStyle, color: "var(--state-danger)" }}>
                  <RotateCcw size={11} />
                </button>
                <button onClick={() => handleStage(f.path)} title="Stage" aria-label={`Stage ${f.path}`} style={{ ...fileDecisionBtnStyle, color: "var(--state-success)" }}>
                  <Plus size={11} />
                </button>
              </div>
            ))}
            {showMoreGitFiles("working", hiddenWorkingCount, "modified files")}
          </div>
        )}

        {gitChanges.untracked.length > 0 && (
          <div>
            <div className="uppercase mb-1 tracking-wide" style={{ fontSize: 10, color: "var(--text-muted)", letterSpacing: "0.5px" }}>
              未跟踪 ({gitChanges.untracked.length})
            </div>
            {visibleUntracked.map((path) => (
              <div key={`ut-${path}`} className="flex items-center gap-1 mb-0.5">
                <button
                  onClick={() => useAppStore.getState().openEditorFile(path, path.split(/[/\\]/).pop())}
                  className="flex-1 min-w-0 border-0 cursor-pointer text-left px-1.5 py-1"
                  style={{
                    borderRadius: "var(--radius-sm, 4px)",
                    background: "transparent",
                    color: "var(--text-muted)",
                    fontSize: "var(--text-xs)",
                  }}
                >
                  <div className="overflow-hidden text-ellipsis whitespace-nowrap" style={{ fontFamily: "var(--font-mono)" }}>{path}</div>
                </button>
                <button onClick={() => handleStage(path)} title="Stage" aria-label={`Stage ${path}`} style={{ ...fileDecisionBtnStyle, color: "var(--state-success)" }}>
                  <Plus size={11} />
                </button>
              </div>
            ))}
            {showMoreGitFiles("untracked", hiddenUntrackedCount, "untracked files")}
          </div>
        )}
      </aside>

      <main className="min-w-0 min-h-0 overflow-hidden flex flex-col">
        {selectedPatch ? (
          <DiffBody
            lines={selectedPatchLines}
            viewMode={viewMode}
            rawPatch={selectedPatch}
            previewLineLimit={GIT_PREVIEW_LINE_LIMIT}
          />
        ) : (
          <div className="flex-1 grid place-items-center" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
            Select a file to view its diff
          </div>
        )}
      </main>
    </div>
  );
};
