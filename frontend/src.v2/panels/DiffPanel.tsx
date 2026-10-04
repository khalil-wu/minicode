import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Check, CheckCircle, CheckCircle2, ChevronDown, Columns2, ExternalLink, FileDiff, GitBranch, GitCompare, MessageCircle, Minus, Plus, RefreshCw, RotateCcw, Rows3, X, XCircle } from "lucide-react";
import {
  commandResultSucceeded,
  sendClientCommand,
  sendClientCommandAwaitResult,
  sendPromptResponseCommand,
} from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { fileIcon } from "../lib/file-icons";
import { buildApprovalResponseCommand } from "../protocol/prompt-responses";
import { useColorizedLines, extractFilePathFromDiff, guessLanguageFromPath } from "../lib/monaco-colorize";
import { buildReviewHistory, type HistoryDiffTurn } from "../lib/review-history";
import { EmptyState } from "../components/EmptyState";
import { Button } from "../components/Button";
import { diffFileDecisionForPath, diffFilePathsEqual } from "../chat/diffReviewState";
import { returnToBrowserPage } from "../chat/openWebInBrowser";
import { workspaceFilePathsEqual, workspaceRootsEqual } from "../lib/workspace-path";
import { parseUnifiedDiffLines } from "../lib/unified-diff";
import { pushToast } from "../overlays/ToastContainer";
import "./DiffPanel.css";

type DiffViewMode = "unified" | "split";
type ChangeScope = "review" | "history" | "git";
const HISTORY_PREVIEW_LINE_LIMIT = 180;
const INLINE_COLORIZE_LINE_LIMIT = 900;
const REVIEW_FILE_INITIAL_LIMIT = 96;
const REVIEW_FILE_INCREMENT = 96;
const GIT_PREVIEW_LINE_LIMIT = 600;
const GIT_FILE_INITIAL_LIMIT = 48;
const GIT_FILE_INCREMENT = 48;

interface DiffLine {
  kind: "context" | "add" | "del" | "hunk" | "meta";
  text: string;
  oldLine?: number;
  newLine?: number;
  lineIndex?: number;
  oldGap?: number;
  newGap?: number;
}

const parseUnifiedDiff = (raw: string): DiffLine[] => {
  let oldLine: number | undefined;
  let newLine: number | undefined;
  let lineIndex = 0;
  return parseUnifiedDiffLines(raw).map(({ kind, text }) => {
    let oldGap: number | undefined;
    let newGap: number | undefined;
    if (kind === "meta") {
      oldLine = undefined;
      newLine = undefined;
    } else if (kind === "hunk") {
      const range = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
      oldGap = range && oldLine !== undefined ? Number(range[1]) - oldLine : undefined;
      newGap = range && newLine !== undefined ? Number(range[2]) - newLine : undefined;
      oldLine = range ? Number(range[1]) : undefined;
      newLine = range ? Number(range[2]) : undefined;
    }
    const line: DiffLine = {
      kind: kind === "marker" ? "meta" : kind,
      text: kind === "add" || kind === "del" || (kind === "context" && text.startsWith(" "))
        ? text.slice(1) : text,
    };
    if (kind === "hunk") {
      line.oldLine = oldLine;
      line.newLine = newLine;
      line.oldGap = oldGap;
      line.newGap = newGap;
    }
    if (kind === "context" || kind === "add" || kind === "del") line.lineIndex = lineIndex++;
    if (kind === "context" || kind === "del") {
      line.oldLine = oldLine;
      if (oldLine !== undefined) oldLine++;
    }
    if (kind === "context" || kind === "add") {
      line.newLine = newLine;
      if (newLine !== undefined) newLine++;
    }
    return line;
  });
};

const visibleDiffLines = (lines: DiffLine[]): DiffLine[] =>
  lines.filter((line) => line.kind !== "meta");

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
      return "var(--editor-foreground)";
  }
};

const bgForKind = (kind: DiffLine["kind"]): string => {
  if (kind === "add") return "color-mix(in oklch, var(--state-success) 12%, transparent)";
  if (kind === "del") return "color-mix(in oklch, var(--state-danger) 12%, transparent)";
  if (kind === "hunk") return "var(--surface-soft)";
  return "transparent";
};

const respond = async (requestId: string, approved: boolean) => {
  const store = useAppStore.getState();
  const review = store.diffReview;
  store.setDiffReviewState({
    ...(review ?? { requestId, diff: "", files: [], fileDecisions: {}, lineComments: [] }),
    status: "submitted",
    error: undefined,
  });
  try {
    const command = buildApprovalResponseCommand(
      requestId,
      approved ? "approve" : "reject",
      { owner: { conversationId: review?.conversationId, turnId: review?.turnId, messageId: review?.messageId } },
    );
    const result = await sendPromptResponseCommand(command);
    if (result && !commandResultSucceeded(result)) throw new Error(result.message || "审批未被后端接受");
    useAppStore.getState().clearDiffReview(requestId);
  } catch (error) {
    const current = useAppStore.getState().diffReview;
    if (current?.requestId === requestId) {
      useAppStore.getState().setDiffReviewState({
        ...current,
        status: "error",
        error: error instanceof Error ? error.message : "审批提交失败",
      });
    }
  }
};

export const DiffPanel = () => {
  const messages = useAppStore((s) => s.messages);
  const diffReview = useAppStore((s) => s.diffReview);
  const gitChanges = useAppStore((s) => s.gitChanges);
  const conversationId = useAppStore((s) => s.conversationId);
  const turnDiff = useAppStore((s) => s.conversationId ? s.turnDiffs[s.conversationId] : undefined);
  const gitReviewRequest = useAppStore((s) => s.gitReviewRequest);
  const [activeScope, setActiveScope] = useState<ChangeScope>(diffReview ? "review" : "git");
  const [scopeMenuOpen, setScopeMenuOpen] = useState(false);
  const scopeMenuRef = useRef<HTMLDivElement | null>(null);
  const [diffViewMode, setDiffViewMode] = useState<DiffViewMode>("unified");

  useEffect(() => {
    if (diffReview) setActiveScope("review");
    else setActiveScope((scope) => scope === "review" ? "git" : scope);
  }, [diffReview]);

  useEffect(() => {
    if (gitReviewRequest && workspaceRootsEqual(gitReviewRequest.workspaceRoot, useAppStore.getState().workingDirectory)
      && gitReviewRequest.conversationId === conversationId) setActiveScope("git");
  }, [gitReviewRequest]);

  const historySources = useMemo(() => {
    return buildReviewHistory(messages, turnDiff, conversationId ?? undefined);
  }, [messages, turnDiff, conversationId]);
  const gitChangeCount = gitChanges.workingTree.length + gitChanges.staged.length + gitChanges.untracked.length;
  const visibleScope = activeScope === "review" && !diffReview ? "git" : activeScope;
  const scopeOptions = [
    ...(diffReview ? [{ value: "review" as const, label: diffReview.mode === "view" || diffReview.status === "viewing" ? "当前修改" : "待审阅", count: diffReview.files.length || 1 }] : []),
    { value: "history" as const, label: "轮次记录", count: historySources.length },
    { value: "git" as const, label: "未提交", count: gitChangeCount },
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
    <div className="mc-diff-panel h-full flex flex-col min-h-0">
      <div className="mc-diff-toolbar flex items-center gap-2 px-2 shrink-0" style={diffToolbarStyle}>
        {diffReview?.previewReturnTarget
          && diffReview.conversationId === conversationId
          && diffReview.previewReturnTarget.conversationId === conversationId && (
          <button
            type="button"
            className="mc-diff-return-preview"
            title={diffReview.previewReturnTarget.url}
            onClick={() => {
              const target = diffReview.previewReturnTarget!;
              if (target.tab === "browser" && target.targetId && target.url) returnToBrowserPage({ conversationId: target.conversationId, targetId: target.targetId, url: target.url });
              else useAppStore.getState().setRightStackTab(target.tab);
            }}
          >
            <ArrowLeft size={14} /> 返回预览
          </button>
        )}
        <div ref={scopeMenuRef} className="relative inline-flex items-center gap-1.5 min-w-0" style={{ color: "var(--text-muted)" }}>
          <FileDiff size={14} />
          <button
            type="button"
            aria-label={`Diff 来源：${visibleScopeOption.label}${visibleScopeOption.count ? ` ${visibleScopeOption.count}` : ""}`}
            aria-haspopup="listbox"
            aria-expanded={scopeMenuOpen}
            className="mc-diff-scope-trigger"
            onClick={() => setScopeMenuOpen((open) => !open)}
            style={scopeTriggerStyle}
          >
            <span className="truncate">{visibleScopeOption.label}</span>
            {visibleScopeOption.count > 0 && (
              <span style={scopeCountStyle}>{visibleScopeOption.count}</span>
            )}
            <ChevronDown size={14} style={{ color: "var(--text-muted)", flexShrink: 0 }} />
          </button>
          {scopeMenuOpen && (
            <div className="mc-dropdown-menu" role="listbox" aria-label="Diff 来源" style={scopeMenuStyle}>
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
          <button
            onClick={() => setDiffViewMode(diffViewMode === "unified" ? "split" : "unified")}
            title={diffViewMode === "unified" ? "切换为分栏视图" : "切换为行内视图"}
            aria-label={diffViewMode === "unified" ? "切换为分栏视图" : "切换为行内视图"}
            className="mc-diff-icon-action w-6 h-[22px]"
            style={{ ...iconButtonStyle }}
          >
            {diffViewMode === "unified" ? <Columns2 size={14} /> : <Rows3 size={14} />}
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

interface DiffBodyProps {
  lines: DiffLine[];
  language?: string;
  viewMode?: DiffViewMode;
  comments?: DiffLineCommentData[];
  onLineClick?: (lineIndex: number) => void;
  onQuoteLine?: (line: DiffLine, side?: "old" | "new") => void;
  filePath?: string;
  activeCommentLine?: number | null;
  onCommentSubmit?: (lineIndex: number, text: string) => void;
  onCommentCancel?: () => void;
  rawPatch?: string;
  previewLineLimit?: number;
}

interface DiffReadingProps extends DiffBodyProps {
  commentDraft: string;
  onCommentDraftChange: (text: string) => void;
  hiddenLineCount?: number;
  onShowFull?: () => void;
}

const DiffBody = ({ lines, language, viewMode = "unified", comments, onLineClick, onQuoteLine, filePath, activeCommentLine, onCommentSubmit, onCommentCancel, rawPatch, previewLineLimit }: DiffBodyProps) => {
  const [showFullPreview, setShowFullPreview] = useState(false);
  const [commentDraft, setCommentDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const readingAnchor = useRef<{ index: string; offset: number } | null>(null);
  const selectedRange = useRef<{ start: string; startOffset: number; end: string; endOffset: number } | null>(null);
  const projectedLines = useMemo(() => visibleDiffLines(lines), [lines]);
  useEffect(() => {
    setShowFullPreview(false);
    readingAnchor.current = null;
    selectedRange.current = null;
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [filePath, rawPatch, previewLineLimit]);
  useEffect(() => { setCommentDraft(""); }, [filePath, activeCommentLine]);
  useEffect(() => {
    const rememberSelection = () => {
      const selection = window.getSelection();
      if (!selection?.rangeCount) return;
      if (selection.isCollapsed) {
        if (selection.anchorNode && scrollRef.current?.contains(selection.anchorNode)) selectedRange.current = null;
        return;
      }
      const range = selection.getRangeAt(0);
      const start = range.startContainer.parentElement?.closest<HTMLElement>("[data-diff-text]");
      const end = range.endContainer.parentElement?.closest<HTMLElement>("[data-diff-text]");
      if (!start || !end || !scrollRef.current?.contains(start) || !scrollRef.current.contains(end)) return;
      const offsetIn = (element: HTMLElement, node: Node, offset: number) => {
        const prefix = document.createRange();
        prefix.selectNodeContents(element);
        prefix.setEnd(node, offset);
        return prefix.toString().length;
      };
      selectedRange.current = { start: start.dataset.diffText!, startOffset: offsetIn(start, range.startContainer, range.startOffset), end: end.dataset.diffText!, endOffset: offsetIn(end, range.endContainer, range.endOffset) };
    };
    document.addEventListener("selectionchange", rememberSelection);
    return () => document.removeEventListener("selectionchange", rememberSelection);
  }, []);
  useLayoutEffect(() => {
    const surface = scrollRef.current!;
    const anchor = readingAnchor.current;
    if (anchor) {
      const row = surface.querySelector<HTMLElement>(`[data-diff-index="${anchor.index}"]`);
      if (row) surface.scrollTop += row.getBoundingClientRect().top - surface.getBoundingClientRect().top - anchor.offset;
    }
    const selection = selectedRange.current;
    if (!selection || activeCommentLine != null) return;
    const endpoint = (key: string, offset: number) => {
      const alternateKey = key.replace(/:(old|new)$/, (_, side: string) => side === "old" ? ":new" : ":old");
      const element = surface.querySelector<HTMLElement>(`[data-diff-text="${key}"]`) ?? surface.querySelector<HTMLElement>(`[data-diff-text="${alternateKey}"]`);
      if (!element) return null;
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      let node = walker.nextNode();
      while (node) {
        if (offset <= node.textContent!.length) return { node, offset };
        offset -= node.textContent!.length;
        node = walker.nextNode();
      }
      return { node: element, offset: element.childNodes.length };
    };
    const start = endpoint(selection.start, selection.startOffset);
    const end = endpoint(selection.end, selection.endOffset);
    if (start && end) {
      const range = document.createRange();
      range.setStart(start.node, start.offset);
      range.setEnd(end.node, end.offset);
      window.getSelection()?.removeAllRanges();
      window.getSelection()?.addRange(range);
    }
  }, [viewMode]);
  const shouldPreview = Boolean(previewLineLimit && projectedLines.length > previewLineLimit && !showFullPreview);
  const visibleLines = shouldPreview && previewLineLimit
    ? projectedLines.slice(0, previewLineLimit)
    : projectedLines;
  const hiddenLineCount = projectedLines.length - visibleLines.length;
  const props: DiffReadingProps = { lines: visibleLines, language, comments, onLineClick, onQuoteLine, filePath, activeCommentLine, onCommentSubmit, onCommentCancel, commentDraft, onCommentDraftChange: setCommentDraft, hiddenLineCount, onShowFull: () => setShowFullPreview(true) };
  return <div ref={scrollRef} className="mc-diff-reading-surface flex-1 min-h-0 overflow-auto" onScroll={() => {
    const surface = scrollRef.current!;
    const top = surface.getBoundingClientRect().top;
    const row = [...surface.querySelectorAll<HTMLElement>("[data-diff-index]")].find((candidate) => candidate.getBoundingClientRect().bottom > top);
    if (row) readingAnchor.current = { index: row.dataset.diffIndex!, offset: row.getBoundingClientRect().top - top };
  }}>
    {viewMode === "split" ? <SplitDiffBody {...props} /> : <UnifiedDiffBody {...props} />}
  </div>;
};

const UnifiedDiffBody = ({ lines, language, comments, onLineClick, onQuoteLine, filePath, activeCommentLine, onCommentSubmit, onCommentCancel, commentDraft, onCommentDraftChange, hiddenLineCount = 0, onShowFull }: DiffReadingProps) => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const lang = language ?? guessLanguageFromPath(filePath ?? extractFilePathFromDiff(lines));
  const colorized = useColorizedLines(lines.length <= INLINE_COLORIZE_LINE_LIMIT ? lines : [], lang);
  const commentMap = useMemo(() => {
    if (!comments) return new Map<number, DiffLineCommentData>();
    const map = new Map<number, DiffLineCommentData>();
    for (const c of comments) {
      if (!filePath || workspaceFilePathsEqual(c.filePath, filePath, workingDirectory)) map.set(c.lineIndex, c);
    }
    return map;
  }, [comments, filePath, workingDirectory]);

  return (
    <div className="mc-diff-code-body">
      {lines.map((line, colorIndex) => {
        if (line.kind === "hunk") return <DiffHunkDivider key={`hunk-${colorIndex}`} line={line} />;
        const i = line.lineIndex!;
        return (
        <div key={i} data-diff-index={i}>
          <div className="mc-diff-code-row" style={{ background: bgForKind(line.kind) }}>
          <DiffLineGutter line={line} filePath={filePath} onQuoteLine={onQuoteLine} />
          <div
            className="mc-diff-code-content px-2.5 whitespace-pre-wrap break-words relative"
            role={onLineClick ? "button" : undefined}
            tabIndex={onLineClick ? 0 : undefined}
            aria-label={onLineClick ? `评论 Diff 第 ${i + 1} 行` : undefined}
            onKeyDown={onLineClick ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onLineClick(i);
              }
            } : undefined}
            style={{
              background: bgForKind(line.kind),
              borderLeft:
                line.kind === "add"
                  ? "2px solid var(--state-success)"
                  : line.kind === "del"
                    ? "2px solid var(--state-danger)"
                    : "2px solid transparent",
              color: !colorized?.[colorIndex] ? colorForKind(line.kind) : undefined,
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
            {colorized?.[colorIndex] ? (
              <span data-diff-text={`${i}:${line.newLine === undefined ? "old" : "new"}`} dangerouslySetInnerHTML={{ __html: colorized[colorIndex] }} />
            ) : (
              <span data-diff-text={`${i}:${line.newLine === undefined ? "old" : "new"}`}>{line.text}</span>
            )}
            {commentMap.has(i) && (
              <span className="ml-2 inline align-middle" style={{ color: "var(--accent-primary)", fontSize: "var(--text-xs)" }} title={commentMap.get(i)!.content}>
                <MessageCircle size={14} style={{ display: "inline", verticalAlign: "middle" }} />
              </span>
            )}
          </div>
          </div>
          {commentMap.has(i) && (
            <div className="py-1 px-2.5 pl-3.5" style={{ background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-base))", borderLeft: "3px solid var(--accent-primary)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
              {commentMap.get(i)!.content}
            </div>
          )}
          {activeCommentLine === i && onCommentSubmit && (
            <InlineCommentInput lineIndex={i} onSubmit={onCommentSubmit} onCancel={onCommentCancel} text={commentDraft} onTextChange={onCommentDraftChange} />
          )}
        </div>
      ); })}
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

const DiffHunkDivider = ({ line }: { line: DiffLine }) => {
  const omitted = Math.max(line.oldGap ?? 0, line.newGap ?? 0);
  return <div className="mc-diff-hunk-divider">
    {omitted > 0 && <span>中间省略 {omitted.toLocaleString()} 行</span>}
    <span>{line.oldLine === 0
      ? `新增内容 · 第 ${line.newLine} 行`
      : line.newLine === 0
        ? `删除内容 · 原文件第 ${line.oldLine} 行`
        : `变更前第 ${line.oldLine} 行 · 变更后第 ${line.newLine} 行`}</span>
    <span className="truncate">{line.text.replace(/^@@.*?@@\s*/, "")}</span>
  </div>;
};

const DiffLineGutter = ({ line, filePath, side, onQuoteLine }: { line: DiffLine; filePath?: string; side?: "old" | "new"; onQuoteLine?: (line: DiffLine, side?: "old" | "new") => void }) => {
  if (line.oldLine === undefined && line.newLine === undefined) return null;
  const original = side === "old" || line.newLine === undefined;
  const quoteLabel = `把 ${filePath} 变更${original ? "前" : "后"}第 ${original ? line.oldLine : line.newLine} 行加入对话`;
  return (
    <span className="mc-diff-line-gutter" data-side={side ?? "both"}>
      {onQuoteLine && (
        <button type="button" className="mc-diff-line-quote" title={quoteLabel} aria-label={quoteLabel} onClick={() => onQuoteLine(line, side)}>
          <MessageCircle size={12} />
        </button>
      )}
      {side !== "new" && <span title="原文件行号">{line.oldLine ?? ""}</span>}
      {side !== "old" && (filePath && line.newLine !== undefined ? (
        <button
          type="button"
          title="跳到当前代码"
          aria-label={`在编辑器中打开 ${filePath} 第 ${line.newLine} 行`}
          onClick={() => useAppStore.getState().openEditorFile(filePath, filePath.split(/[/\\]/).pop(), { line: line.newLine, exact: true })}
        >
          {line.newLine}
        </button>
      ) : <span title="当前文件行号">{line.newLine ?? ""}</span>)}
    </span>
  );
};

const InlineCommentInput = ({ lineIndex, onSubmit, onCancel, text, onTextChange }: { lineIndex: number; onSubmit: (lineIndex: number, text: string) => void; onCancel?: () => void; text: string; onTextChange: (text: string) => void }) => {
  return (
    <div className="flex items-center gap-1.5 py-1.5 px-2.5 pl-3.5" style={{ borderLeft: "3px solid var(--accent-primary)", background: "color-mix(in oklch, var(--accent-primary) 5%, var(--surface-base))" }}>
      <input
        type="text"
        aria-label={`评论 Diff 第 ${lineIndex + 1} 行`}
        placeholder={`评论第 ${lineIndex + 1} 行...`}
        value={text}
        onChange={(e) => onTextChange(e.target.value)}
        onKeyDown={(e) => { if (e.nativeEvent.isComposing || e.keyCode === 229) return; if (e.key === "Enter" && text.trim()) { e.preventDefault(); onSubmit(lineIndex, text.trim()); } if (e.key === "Escape") onCancel?.(); }}
        className="flex-1 px-2 py-1 outline-none"
        style={{ border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", fontSize: "var(--text-xs)", background: "var(--surface-base)", color: "var(--text-primary)" }}
        autoFocus
      />
      <Button variant="primary" size="sm" onClick={() => { if (text.trim()) onSubmit(lineIndex, text.trim()); }}>添加</Button>
      <Button variant="ghost" size="sm" onClick={onCancel}>取消</Button>
    </div>
  );
};

const SplitDiffBody = ({ lines, language, comments, onLineClick, onQuoteLine, filePath, activeCommentLine, onCommentSubmit, onCommentCancel, commentDraft, onCommentDraftChange, hiddenLineCount = 0, onShowFull }: DiffReadingProps) => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const lang = language ?? guessLanguageFromPath(filePath ?? extractFilePathFromDiff(lines));
  const colorized = useColorizedLines(lines.length <= INLINE_COLORIZE_LINE_LIMIT ? lines : [], lang);
  const rows = useMemo(() => {
    const result: SplitRow[] = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (line.kind === "meta" || line.kind === "hunk") {
        result.push({ left: line, right: line });
        i++;
      } else if (line.kind === "context") {
        result.push({ left: line, right: line, leftIndex: line.lineIndex, rightIndex: line.lineIndex });
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
            leftIndex: dels[j]?.lineIndex,
            rightIndex: adds[j]?.lineIndex,
          });
        }
      } else if (line.kind === "add") {
        result.push({ left: null, right: line, rightIndex: line.lineIndex });
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
      if (!filePath || workspaceFilePathsEqual(c.filePath, filePath, workingDirectory)) map.set(c.lineIndex, c);
    }
    return map;
  }, [comments, filePath, workingDirectory]);

  const colorizedMap = useMemo(() => {
    if (!colorized) return null;
    const map = new Map<DiffLine, string>();
    lines.forEach((line, i) => {
      if (colorized[i]) map.set(line, colorized[i]);
    });
    return map;
  }, [colorized, lines]);

  return (
    <div className="mc-diff-code-body">
      {rows.map((row, i) => {
        if (row.left?.kind === "hunk") return <DiffHunkDivider key={i} line={row.left} />;
        const commentLineIndices = row.leftIndex === row.rightIndex
          ? [row.leftIndex]
          : [row.leftIndex, row.rightIndex];
        return (
          <div key={i}>
            <div className="grid grid-cols-2">
              <SplitRowPair row={row} colorizedMap={colorizedMap} onLineClick={onLineClick} onQuoteLine={onQuoteLine} filePath={filePath} />
            </div>
            {commentLineIndices.map((lineIdx) => lineIdx != null && commentMap.has(lineIdx) && (
              <div key={lineIdx} className="py-1 px-2.5 pl-3.5" style={{ background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-base))", borderLeft: "3px solid var(--accent-primary)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
                {commentMap.get(lineIdx)!.content}
              </div>
            ))}
            {activeCommentLine != null && (activeCommentLine === row.leftIndex || activeCommentLine === row.rightIndex) && onCommentSubmit && (
              <InlineCommentInput key={activeCommentLine} lineIndex={activeCommentLine} onSubmit={onCommentSubmit} onCancel={onCommentCancel} text={commentDraft} onTextChange={onCommentDraftChange} />
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
    <span className="flex-1">当前为预览，另有 {hiddenLineCount.toLocaleString()} 行 Diff 已隐藏。</span>
    {onShowFull && (
      <button type="button" onClick={onShowFull} style={smallActionButtonStyle}>
        显示完整 Diff
      </button>
    )}
  </div>
);

const SplitRowPair = ({ row, colorizedMap, onLineClick, onQuoteLine, filePath }: { row: SplitRow; colorizedMap: Map<DiffLine, string> | null; onLineClick?: (lineIndex: number) => void; onQuoteLine?: (line: DiffLine, side?: "old" | "new") => void; filePath?: string }) => {
  const renderCell = (line: DiffLine | null, side: "left" | "right", lineIndex?: number) => {
    if (!line) {
      return <div className="mc-diff-empty-code-cell px-2" style={{ background: "var(--surface-soft)" }} />;
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
      <div className="mc-diff-code-row" data-diff-index={lineIndex} style={{ background: bg }}>
      <DiffLineGutter line={line} filePath={filePath} side={side === "left" ? "old" : "new"} onQuoteLine={onQuoteLine} />
      <div
        className="mc-diff-code-content px-2 whitespace-pre-wrap break-words"
        role={clickable ? "button" : undefined}
        tabIndex={clickable ? 0 : undefined}
        aria-label={clickable ? `评论 Diff 第 ${lineIndex + 1} 行` : undefined}
        onKeyDown={clickable ? (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onLineClick(lineIndex);
          }
        } : undefined}
        style={{ background: bg, cursor: clickable ? "pointer" : undefined }}
        onClick={clickable ? () => onLineClick(lineIndex) : undefined}
      >
        {html ? (
          <span data-diff-text={`${lineIndex}:${side === "left" ? "old" : "new"}`} dangerouslySetInnerHTML={{ __html: html }} />
        ) : (
          <span data-diff-text={`${lineIndex}:${side === "left" ? "old" : "new"}`} style={{ color: colorForKind(line.kind) }}>{line.text}</span>
        )}
      </div>
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
  border: 0,
  borderRadius: 4,
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
  borderRadius: 4,
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
  border: 0,
  borderRadius: 4,
  cursor: "pointer",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 0,
  flexShrink: 0,
};

const diffToolbarStyle: React.CSSProperties = {
  minHeight: 34,
  borderBottom: "1px solid var(--border-subtle)",
  background: "var(--editor-background)",
};

const scopeTriggerStyle: React.CSSProperties = {
  minHeight: 28,
  maxWidth: 210,
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  border: 0,
  borderRadius: 4,
  color: "var(--text-primary)",
  fontSize: "var(--text-xs)",
  fontWeight: "var(--fw-semibold)",
  padding: "0 6px",
  cursor: "pointer",
};

const scopeMenuStyle: React.CSSProperties = {
  position: "absolute",
  top: "calc(100% + 5px)",
  left: 20,
  zIndex: "var(--z-composer)",
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
  minWidth: 16,
  height: 16,
  padding: "0 4px",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  borderRadius: 4,
  background: "var(--surface-soft)",
  color: "var(--text-muted)",
  fontSize: "var(--text-xxs)",
  flexShrink: 0,
};

// ── Review Tab ───────────────────────────────────────────────────

import type { DiffReviewState } from "../stores/types";

const ReviewTab = ({ diffReview, viewMode }: { diffReview: DiffReviewState | null; viewMode: DiffViewMode }) => {
  if (!diffReview) {
    return (
      <div className="flex-1 grid place-items-center p-4" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
        暂无待审阅的改动。需要审批的 Diff 会显示在这里。
      </div>
    );
  }

  return <ActiveReviewTab diffReview={diffReview} viewMode={viewMode} />;
};

const ActiveReviewTab = ({ diffReview, viewMode }: { diffReview: DiffReviewState; viewMode: DiffViewMode }) => {
  const [commentLineIndex, setCommentLineIndex] = useState<number | null>(null);
  const [visibleFileLimit, setVisibleFileLimit] = useState(REVIEW_FILE_INITIAL_LIMIT);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const conversationId = useAppStore((s) => s.conversationId);

  useEffect(() => {
    setVisibleFileLimit(REVIEW_FILE_INITIAL_LIMIT);
    setCommentLineIndex(null);
  }, [diffReview.requestId]);

  useEffect(() => {
    setCommentLineIndex(null);
  }, [diffReview.selectedPath]);

  const selectedFile = useMemo(
    () => diffReview.files.find((file) =>
      diffFilePathsEqual(file.path, diffReview.selectedPath, workingDirectory),
    ),
    [diffReview.files, diffReview.selectedPath, workingDirectory],
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
  const canDiscuss = isReadOnly && Boolean(conversationId) && diffReview.conversationId === conversationId && Boolean(diffReview.selectedPath);
  const isSubmitted = diffReview.status === "submitted";
  const decidedFileCount = diffReview.files.filter((file) =>
    diffFileDecisionForPath(diffReview.fileDecisions, file.path, workingDirectory),
  ).length;
  const allFilesDecided = decidedFileCount >= diffReview.files.length;
  const visibleFiles = useMemo(
    () => diffReview.files.slice(0, visibleFileLimit),
    [diffReview.files, visibleFileLimit],
  );
  const hiddenFileCount = Math.max(0, diffReview.files.length - visibleFiles.length);
  const handleLineClick = (lineIndex: number) => {
    setCommentLineIndex(commentLineIndex === lineIndex ? null : lineIndex);
  };
  const discussChange = (line?: DiffLine, side?: "old" | "new") => {
    const store = useAppStore.getState();
    if (store.conversationId !== diffReview.conversationId) return;
    const path = diffReview.selectedPath!;
    if (line) {
      const original = side === "old" || line.newLine === undefined;
      const location = `变更${original ? "前" : "后"}第 ${original ? line.oldLine : line.newLine} 行`;
      const context = `关于 ${path}（${location}）：\n\n    ${line.text}`;
      store.setDraft(store.draft ? `${store.draft}\n\n${context}` : context);
    } else {
      store.addSelectedMention({ kind: "file", path, name: path.split(/[/\\]/).pop()! });
    }
    const chatPanel = store.panelSlots.find((slot) => slot.kind === "chat");
    if (chatPanel) store.focusPanel(chatPanel.id);
    else store.addPanel({ id: "main-chat", kind: "chat", label: "Chat" });
    window.dispatchEvent(new Event("composer:focus"));
    requestAnimationFrame(() => {
      const current = useAppStore.getState();
      if (current.conversationId !== conversationId || !workspaceRootsEqual(current.workingDirectory, workingDirectory)) return;
      document.querySelector<HTMLTextAreaElement>("[data-composer-input]")?.focus();
    });
  };

  return (
    <div className="mc-diff-review-layout h-full grid min-h-0 overflow-hidden" data-has-files={diffReview.files.length > 0 ? "true" : "false"}>
      {diffReview.files.length > 0 && (
        <aside className="mc-diff-review-files min-h-0 overflow-hidden flex flex-col">
          <div className="mc-diff-files-heading">
            <FileDiff size={14} color="var(--text-muted)" />
            <span className="flex-1 font-medium" style={{ color: "var(--text-secondary)" }}>{isReadOnly ? "Diff" : "Diff 审阅"}</span>
            <span style={{ color: "var(--text-muted)" }}>{diffReview.files.length}</span>
          </div>
          <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden">
            {visibleFiles.map((file) => {
              const decision = diffFileDecisionForPath(diffReview.fileDecisions, file.path, workingDirectory);
              return (
                <div key={file.path} className="mc-diff-file-row">
                  <button
                    onClick={() => {
                      useAppStore.getState().setDiffReviewSelectedPath(file.path);
                      if (!file.patch) {
                        sendClientCommand({
                          type: "approval.file_diff",
                          tool_call_id: diffReview.requestId,
                          path: file.path,
                          conversation_id: diffReview.conversationId,
                          turn_id: diffReview.turnId,
                        });
                      }
                    }}
                    title={file.path}
                    className="mc-diff-file-item"
                    data-active={workspaceFilePathsEqual(file.path, diffReview.selectedPath, workingDirectory) || undefined}
                  >
                    <div className="mc-diff-file-path">
                      {fileIcon(file.path, { size: 15, className: "diff-file-icon" })}
                      <span className="overflow-hidden text-ellipsis whitespace-nowrap">{file.path}</span>
                    </div>
                    <div className="mc-diff-file-counts">
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
                        <CheckCircle size={14} />
                      </button>
                      <button
                        title="拒绝文件"
                        aria-label={`拒绝文件 ${file.path}`}
                        onClick={(e) => { e.stopPropagation(); useAppStore.getState().setDiffFileDecision(file.path, "rejected"); }}
                        style={{ ...fileDecisionBtnStyle, color: decision === "rejected" ? "var(--text-on-accent)" : "var(--state-danger)", background: decision === "rejected" ? "var(--state-danger)" : "transparent" }}
                      >
                        <XCircle size={14} />
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
          {!isReadOnly && decidedFileCount > 0 && (
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
              {isSubmitted ? "提交中..." : `提交审查 (${decidedFileCount}/${diffReview.files.length})`}
            </button>
          )}
        </aside>
      )}

      <main className="min-w-0 min-h-0 overflow-hidden flex flex-col">
        <div className="mc-diff-review-header">
          <span className="font-medium" style={{ color: "var(--text-secondary)" }}>{isReadOnly ? (diffReview.toolName || "工具") : `${diffReview.toolName || "工具"}审批`}</span>
          {plus > 0 && <span style={{ color: "var(--state-success)" }}>+{plus}</span>}
          {minus > 0 && <span style={{ color: "var(--state-danger)" }}>-{minus}</span>}
          <span className="flex-1" />
          {canDiscuss && (
            <button type="button" onClick={() => discussChange()} title="把文件加入对话" aria-label="把文件加入对话" className="mc-diff-icon-action" style={iconButtonStyle}>
              <MessageCircle size={14} />
            </button>
          )}
          {selectedFile && (
            <button
              onClick={() => useAppStore.getState().openEditorFile(selectedFile.path, selectedFile.path.split(/[/\\]/).pop(), { exact: true })}
              title="在编辑器中打开文件" aria-label="在编辑器中打开文件" className="mc-diff-icon-action" style={iconButtonStyle}
            >
              <ExternalLink size={14} />
            </button>
          )}
          {diffReview.status === "error" && diffReview.error && (
            <span className="mc-diff-review-error" role="alert" style={{ color: "var(--state-danger)" }}>{diffReview.error}</span>
          )}
          {!isReadOnly && isSubmitted && <span style={{ color: "var(--text-muted)" }}>已提交</span>}
          {!isReadOnly && (
            <div className="mc-diff-review-actions">
              <button disabled={isSubmitted} onClick={() => respond(diffReview.requestId, false)} style={{ ...rejectButtonStyle, opacity: isSubmitted ? 0.6 : 1 }}><X size={14} /> 全部拒绝</button>
              <button disabled={isSubmitted} onClick={() => respond(diffReview.requestId, true)} style={{ ...acceptButtonStyle, opacity: isSubmitted ? 0.6 : 1 }}><Check size={14} /> 全部接受</button>
              {comments.length > 0 && (
                <button disabled={isSubmitted} onClick={() => useAppStore.getState().submitDiffReviewWithComments()} style={{ ...acceptButtonStyle, background: "var(--accent-primary)", borderColor: "var(--accent-primary)", opacity: isSubmitted ? 0.6 : 1 }}>
                  <MessageCircle size={14} /> 提交意见 ({comments.length})
                </button>
              )}
            </div>
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
            onLineClick={isReadOnly ? undefined : handleLineClick}
            onQuoteLine={canDiscuss ? discussChange : undefined}
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
          />
        )}
      </main>
    </div>
  );
};

// ── History Tab ──────────────────────────────────────────────────

const addDiffLineToConversation = (path: string, line: DiffLine, side?: "old" | "new", source?: string) => {
  const store = useAppStore.getState();
  const original = side === "old" || line.newLine === undefined;
  const location = `变更${original ? "前" : "后"}第 ${original ? line.oldLine : line.newLine} 行`;
  const context = `关于 ${path}（${source ? `${source}，` : ""}${location}）：\n\n    ${line.text}`;
  store.setDraft(store.draft ? `${store.draft}\n\n${context}` : context);
  const chatPanel = store.panelSlots.find((slot) => slot.kind === "chat");
  if (chatPanel) store.focusPanel(chatPanel.id);
  else store.addPanel({ id: "main-chat", kind: "chat", label: "Chat" });
  window.dispatchEvent(new Event("composer:focus"));
  requestAnimationFrame(() => {
    const current = useAppStore.getState();
    if (current.conversationId !== store.conversationId || !workspaceRootsEqual(current.workingDirectory, store.workingDirectory)) return;
    document.querySelector<HTMLTextAreaElement>("[data-composer-input]")?.focus();
  });
};

const HistoryTab = ({ sources, viewMode }: { sources: HistoryDiffTurn[]; viewMode: DiffViewMode }) => {
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const turn = sources.find((source) => source.id === selectedTurnId) ?? sources[0];
  const file = turn?.files.find((source) => source.path === selectedPath) ?? turn?.files[0];
  if (sources.length === 0) {
    return (
      <div className="flex-1 grid place-items-center p-4">
        <EmptyState compact icon={<GitCompare size={20} />} title="暂无轮次记录" hint="完成对话后，可以按轮次查看修改。" />
      </div>
    );
  }

  return (
    <div className="h-full min-h-0 flex flex-col">
      <div className="mc-diff-history-navigation">
        <label>轮次 <select aria-label="审阅轮次" value={turn.id} onChange={(event) => { setSelectedTurnId(event.target.value); setSelectedPath(null); }}>
          {sources.map((source, index) => <option key={source.id} value={source.id}>
            {index === 0 ? "上一轮" : `第 ${sources.length - index} 轮`}{source.label ? ` · ${source.label}` : ""} · {source.files.length} 个文件
          </option>)}
        </select></label>
        {turn.files.length > 0 && <label>文件 <select aria-label="轮次修改文件" value={file!.path} onChange={(event) => setSelectedPath(event.target.value)}>
          {turn.files.map((source) => <option key={source.path} value={source.path}>{source.path || "未标注文件路径"}{source.revisions.length > 1 ? ` · ${source.revisions.length} 次编辑` : ""}</option>)}
        </select></label>}
      </div>
      {!file ? <div className="flex-1 grid place-items-center p-4"><EmptyState compact icon={<GitCompare size={20} />} title="这一轮没有文件修改" hint="可以选择其他轮次查看。" /></div> : <div className="mc-diff-history-list flex-1 min-h-0 overflow-y-auto flex flex-col">
      {file.revisions.map((d, index) => {
        const parsed = parseUnifiedDiff(d.diff);
        const plus = parsed.filter((l) => l.kind === "add").length;
        const minus = parsed.filter((l) => l.kind === "del").length;
        return (
          <div key={d.id} className="mc-diff-history-card overflow-hidden shrink-0">
            <div className="mc-diff-history-file-header">
              <span className="mc-diff-history-file-path" title={file.path}>{file.path || "未标注文件路径"}</span>
              {file.revisions.length > 1 && <span>第 {index + 1} 次编辑</span>}
              <span style={{ color: "var(--text-muted)" }}>{parsed.length.toLocaleString()} 行</span>
              <span className="flex-1" />
              {plus > 0 && <span style={{ color: "var(--state-success)" }}>+{plus}</span>}
              {minus > 0 && <span style={{ color: "var(--state-danger)" }}>-{minus}</span>}
            </div>
              <div style={{ maxHeight: "min(58vh, 760px)", display: "flex", minHeight: 0 }}>
                <DiffBody
                  key={`${turn.id}:${file.path}:${d.id}`}
                  lines={parsed}
                  viewMode={viewMode}
                  rawPatch={d.diff}
                  previewLineLimit={HISTORY_PREVIEW_LINE_LIMIT}
                  filePath={file.path || undefined}
                  onQuoteLine={file.path ? (line, side) => addDiffLineToConversation(file.path, line, side, `第 ${sources.length - sources.indexOf(turn)} 轮`) : undefined}
                />
              </div>
          </div>
        );
      })}</div>}
    </div>
  );
};

// ── Working Tree Tab ─────────────────────────────────────────────

const GitChangesTab = ({ viewMode }: { viewMode: DiffViewMode }) => {
  const gitChanges = useAppStore((s) => s.gitChanges);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const conversationId = useAppStore((s) => s.conversationId);
  const requestGitChanges = useAppStore((s) => s.requestGitChanges);
  const gitReviewRequest = useAppStore((s) => s.gitReviewRequest);
  const [pendingActions, setPendingActions] = useState<Set<string>>(() => new Set());
  const pendingActionsRef = useRef(pendingActions);
  pendingActionsRef.current = pendingActions;
  const [selection, setSelection] = useState<{ path: string; section: "staged" | "working" | "untracked" } | null>(null);
  const [visibleGitLimits, setVisibleGitLimits] = useState({
    staged: GIT_FILE_INITIAL_LIMIT,
    working: GIT_FILE_INITIAL_LIMIT,
    untracked: GIT_FILE_INITIAL_LIMIT,
  });

  useEffect(() => {
    requestGitChanges();
  }, [requestGitChanges]);

  useEffect(() => {
    if (gitReviewRequest && workspaceRootsEqual(gitReviewRequest.workspaceRoot, workingDirectory)
      && gitReviewRequest.conversationId === conversationId) {
      setSelection({ path: gitReviewRequest.path, section: gitReviewRequest.section });
    }
  }, [gitReviewRequest, workingDirectory, conversationId]);

  const selectFile = (path: string, section: "staged" | "working" | "untracked") => {
    setSelection({ path, section });
    useAppStore.getState().openGitReview({ path, section, workspaceRoot: workingDirectory, conversationId });
  };

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
    if (!selection) return null;
    const file = allFiles.find((f) =>
      f.section === selection.section && workspaceFilePathsEqual(f.path, selection.path, workingDirectory),
    );
    return file?.patch ?? null;
  }, [selection, allFiles, workingDirectory]);
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

  const gitCommandScope = () => {
    return {
      conversation_id: String(conversationId || "").trim(),
      workspace: workingDirectory.trim(),
    };
  };

  const beginGitAction = async (key: string, command: Parameters<typeof sendClientCommand>[0]) => {
    const current = useAppStore.getState();
    if (current.conversationId !== conversationId || !workspaceRootsEqual(current.workingDirectory, workingDirectory)) return false;
    if (pendingActionsRef.current.size > 0) return false;
    pendingActionsRef.current = new Set([key]);
    setPendingActions(pendingActionsRef.current);
    try {
      const result = await sendClientCommandAwaitResult(command, command.type, { silent: true });
      if (!commandResultSucceeded(result)) throw new Error(result.message || "Git 操作失败");
      return true;
    } catch (error) {
      pushToast(error instanceof Error ? error.message : "Git 操作失败", "error", 5000);
      return false;
    } finally {
      pendingActionsRef.current = new Set();
      setPendingActions(pendingActionsRef.current);
    }
  };

  const handleStage = (path: string) => {
    beginGitAction(`stage:${path}`, { type: "diff.git_stage_file", path, ...gitCommandScope() });
  };

  const handleUnstage = (path: string) => {
    beginGitAction(`unstage:${path}`, { type: "diff.git_unstage_file", path, ...gitCommandScope() });
  };

  const handleStageAll = () => {
    beginGitAction("stage-all", { type: "diff.git_stage_all", ...gitCommandScope() });
  };

  const handleUnstageAll = () => {
    beginGitAction("unstage-all", { type: "diff.git_unstage_all", ...gitCommandScope() });
  };

  const handleRevert = async (path: string) => {
    const { showConfirm } = await import("../overlays/DialogService");
    const confirmed = await showConfirm({
      title: "放弃文件更改",
      message: `确定放弃 ${path} 的本地更改吗？此操作无法撤销。`,
      confirmLabel: "放弃",
      cancelLabel: "取消",
      danger: true,
    });
    if (confirmed) {
      beginGitAction(`revert:${path}`, { type: "diff.git_revert_file", path, confirmed: true, ...gitCommandScope() });
    }
  };

  if (gitChanges.loading && allFiles.length === 0) {
    return (
      <div className="flex-1 grid place-items-center" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
        正在加载 Git 更改...
      </div>
    );
  }

  if (gitChanges.error || gitChanges.isGitRepo === false) {
    return (
      <div className="flex-1 grid place-items-center p-4">
        <EmptyState
          compact
          icon={gitChanges.error ? <XCircle size={20} /> : <GitBranch size={20} />}
          title={gitChanges.error ? "无法加载 Git 更改" : "当前文件夹未启用 Git"}
          hint={gitChanges.error ? "暂时无法读取当前工作区的 Git 状态，可以刷新后重新检查。" : "可以继续使用文件编辑和聊天功能。"}
          action={<div>
            <button type="button" onClick={requestGitChanges} style={smallActionButtonStyle}>刷新 Git 状态</button>
            {gitChanges.error && <details className="mt-2 text-left"><summary style={{ cursor: "pointer", fontSize: "var(--text-xs)" }}>错误详情</summary><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: "var(--text-xxs)" }}>{gitChanges.error}</pre></details>}
          </div>}
        />
      </div>
    );
  }

  if (allFiles.length === 0 && gitChanges.untracked.length === 0) {
    return (
      <div className="flex-1 grid place-items-center p-4">
        <EmptyState compact icon={<CheckCircle2 size={20} />} title="没有未提交的更改" hint="工作区是干净的。" />
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
        再显示 {Math.min(GIT_FILE_INCREMENT, hiddenCount)} 个{label}
      </button>
    ) : null
  );

  return (
    <div className="mc-diff-git-layout h-full grid min-h-0 overflow-hidden">
      <aside className="mc-diff-git-files min-h-0 overflow-y-auto overflow-x-hidden flex flex-col gap-2">
        <div className="flex flex-col gap-1.5" style={{ fontSize: "var(--text-xs)" }}>
          <div className="mc-diff-files-heading">
            <GitBranch size={14} color="var(--text-muted)" />
            <span className="flex-1 font-medium" style={{ color: "var(--text-secondary)" }}>未提交更改</span>
            <button type="button" disabled={gitChanges.loading || pendingActions.size > 0} onClick={requestGitChanges} title="刷新" aria-label="刷新 Git 更改" className="mc-diff-icon-action w-[22px] h-5" style={iconButtonStyle}>
              <RefreshCw size={14} className={gitChanges.loading ? "spin" : ""} />
            </button>
          </div>
          {(hasWorkingChanges || hasStagedChanges) && (
            <div className="mc-diff-git-batch-actions flex items-center gap-1.5 flex-wrap">
              {hasWorkingChanges && (
                <button
                  type="button"
                  disabled={pendingActions.size > 0}
                  onClick={handleStageAll}
                  title="全部暂存"
                  aria-label="全部暂存"
                  style={{ ...smallActionButtonStyle, color: "var(--state-success)" }}
                >
                  <Plus size={14} />
                  全部暂存
                </button>
              )}
              {hasStagedChanges && (
                <button
                  type="button"
                  disabled={pendingActions.size > 0}
                  onClick={handleUnstageAll}
                  title="全部取消暂存"
                  aria-label="全部取消暂存"
                  style={{ ...smallActionButtonStyle, color: "var(--text-muted)" }}
                >
                  <Minus size={14} />
                  全部取消暂存
                </button>
              )}
            </div>
          )}
        </div>

        {gitChanges.staged.length > 0 && (
          <div>
            <div className="mc-diff-file-group-title">
              已暂存 ({gitChanges.staged.length})
            </div>
            {visibleStaged.map((f) => (
              <div key={`staged-${f.path}`} className="mc-diff-file-row">
                <button
                  onClick={() => selectFile(f.path, "staged")}
                  aria-label={`审阅已暂存 ${f.path}`}
                  title={f.path}
                  className="mc-diff-file-item"
                  data-active={(selection?.section === "staged" && workspaceFilePathsEqual(selection.path, f.path, workingDirectory)) || undefined}
                >
                  <div className="mc-diff-file-path">
                    {fileIcon(f.path, { size: 15, className: "diff-file-icon" })}
                    <span className="overflow-hidden text-ellipsis whitespace-nowrap">{f.path}</span>
                  </div>
                  <div className="mc-diff-file-counts">
                    <span style={{ color: "var(--state-success)" }}>+{f.additions}</span>
                    <span style={{ color: "var(--state-danger)" }}>-{f.deletions}</span>
                  </div>
                </button>
                <button type="button" disabled={pendingActions.size > 0} onClick={() => handleUnstage(f.path)} title="取消暂存" aria-label={`取消暂存 ${f.path}`} style={{ ...fileDecisionBtnStyle, color: "var(--text-muted)" }}>
                  <Minus size={14} />
                </button>
              </div>
            ))}
            {showMoreGitFiles("staged", hiddenStagedCount, "已暂存文件")}
          </div>
        )}

        {gitChanges.workingTree.length > 0 && (
          <div>
            <div className="mc-diff-file-group-title">
              已修改 ({gitChanges.workingTree.length})
            </div>
            {visibleWorking.map((f) => (
              <div key={`wt-${f.path}`} className="mc-diff-file-row">
                <button
                  onClick={() => selectFile(f.path, "working")}
                  aria-label={`审阅未暂存 ${f.path}`}
                  title={f.path}
                  className="mc-diff-file-item"
                  data-active={(selection?.section === "working" && workspaceFilePathsEqual(selection.path, f.path, workingDirectory)) || undefined}
                >
                  <div className="mc-diff-file-path">
                    {fileIcon(f.path, { size: 15, className: "diff-file-icon" })}
                    <span className="overflow-hidden text-ellipsis whitespace-nowrap">{f.path}</span>
                  </div>
                  <div className="mc-diff-file-counts">
                    <span style={{ color: "var(--state-success)" }}>+{f.additions}</span>
                    <span style={{ color: "var(--state-danger)" }}>-{f.deletions}</span>
                  </div>
                </button>
                <button type="button" disabled={pendingActions.size > 0} onClick={() => handleRevert(f.path)} title="放弃更改" aria-label={`放弃 ${f.path} 的更改`} style={{ ...fileDecisionBtnStyle, color: "var(--state-danger)" }}>
                  <RotateCcw size={14} />
                </button>
                <button type="button" disabled={pendingActions.size > 0} onClick={() => handleStage(f.path)} title="暂存" aria-label={`暂存 ${f.path}`} style={{ ...fileDecisionBtnStyle, color: "var(--state-success)" }}>
                  <Plus size={14} />
                </button>
              </div>
            ))}
            {showMoreGitFiles("working", hiddenWorkingCount, "已修改文件")}
          </div>
        )}

        {gitChanges.untracked.length > 0 && (
          <div>
            <div className="mc-diff-file-group-title">
              未跟踪 ({gitChanges.untracked.length})
            </div>
            {visibleUntracked.map((path) => (
              <div key={`ut-${path}`} className="mc-diff-file-row">
                <button
                  onClick={() => selectFile(path, "untracked")}
                  aria-label={`审阅未跟踪 ${path}`}
                  title={path}
                  className="mc-diff-file-item"
                  data-active={(selection?.section === "untracked" && workspaceFilePathsEqual(selection.path, path, workingDirectory)) || undefined}
                >
                  <div className="mc-diff-file-path">
                    {fileIcon(path, { size: 15, className: "diff-file-icon" })}
                    <span className="overflow-hidden text-ellipsis whitespace-nowrap">{path}</span>
                  </div>
                </button>
                <button type="button" disabled={pendingActions.size > 0} onClick={() => handleStage(path)} title="暂存" aria-label={`暂存 ${path}`} style={{ ...fileDecisionBtnStyle, color: "var(--state-success)" }}>
                  <Plus size={14} />
                </button>
              </div>
            ))}
            {showMoreGitFiles("untracked", hiddenUntrackedCount, "未跟踪文件")}
          </div>
        )}
      </aside>

      <main className="min-w-0 min-h-0 overflow-hidden flex flex-col">
        {selection && <div className="mc-diff-git-selection">
          <span>{selection.section === "staged" ? "已暂存" : selection.section === "working" ? "未暂存" : "未跟踪"}</span>
          <span className="flex-1 truncate" title={selection.path}>{selection.path}</span>
          <button type="button" aria-label="在编辑器中打开 Git 文件" title="在编辑器中打开文件" className="mc-diff-icon-action" style={iconButtonStyle} onClick={() => useAppStore.getState().openEditorFile(selection.path, selection.path.split(/[/\\]/).pop(), { exact: true })}><ExternalLink size={14} /></button>
        </div>}
        {selectedPatch ? (
          <DiffBody
            lines={selectedPatchLines}
            viewMode={viewMode}
            rawPatch={selectedPatch}
            previewLineLimit={GIT_PREVIEW_LINE_LIMIT}
            filePath={selection!.path}
            onQuoteLine={conversationId ? (line, side) => addDiffLineToConversation(selection!.path, line, side, selection!.section === "staged" ? "Git 已暂存" : "Git 未暂存") : undefined}
          />
        ) : (
          <div className="flex-1 grid place-items-center" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
            {selection?.section === "untracked" ? "此文件尚未加入 Git；可打开文件查看，或暂存后审阅。" : selection ? gitChanges.loading ? "正在加载所选差异…" : "所选暂存范围没有差异。" : "选择文件以查看其 Diff"}
          </div>
        )}
      </main>
    </div>
  );
};
