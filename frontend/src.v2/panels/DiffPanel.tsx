import { useEffect, useMemo, useState } from "react";
import { Check, CheckCircle, Columns2, ExternalLink, FileDiff, GitBranch, MessageCircle, Minus, Plus, RefreshCw, Rows3, X, XCircle } from "lucide-react";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { getToolCallsFromMessage } from "../lib/content-blocks";
import { useColorizedLines, extractFilePathFromDiff, guessLanguageFromPath } from "../lib/monaco-colorize";

type DiffViewMode = "unified" | "split";

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
  const sent = sendClientCommand({
    type: "approval",
    tool_call_id: requestId,
    action: approved ? "approve" : "reject",
  });
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
  const [activeTab, setActiveTab] = useState<"review" | "history" | "git">(diffReview ? "review" : "history");
  const [diffViewMode, setDiffViewMode] = useState<DiffViewMode>("unified");

  useEffect(() => {
    if (diffReview) setActiveTab("review");
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

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", minHeight: 0 }}>
      <div style={{ display: "flex", alignItems: "center", borderBottom: "1px solid var(--border-subtle)", background: "var(--surface-page)", padding: "0 8px", gap: 2, flexShrink: 0 }}>
        <TabButton active={activeTab === "review"} onClick={() => setActiveTab("review")} badge={diffReview ? 1 : 0}>
          Review
        </TabButton>
        <TabButton active={activeTab === "history"} onClick={() => setActiveTab("history")} badge={historySources.length}>
          History
        </TabButton>
        <TabButton active={activeTab === "git"} onClick={() => setActiveTab("git")} badge={gitChanges.workingTree.length + gitChanges.staged.length}>
          Git Changes
        </TabButton>
        <span style={{ flex: 1 }} />
        <button
          onClick={() => setDiffViewMode(diffViewMode === "unified" ? "split" : "unified")}
          title={diffViewMode === "unified" ? "Switch to split view" : "Switch to unified view"}
          style={{ ...iconButtonStyle, width: 24, height: 22 }}
        >
          {diffViewMode === "unified" ? <Columns2 size={12} /> : <Rows3 size={12} />}
        </button>
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: "hidden" }}>
        {activeTab === "review" && <ReviewTab diffReview={diffReview} viewMode={diffViewMode} />}
        {activeTab === "history" && <HistoryTab sources={historySources} viewMode={diffViewMode} />}
        {activeTab === "git" && <GitChangesTab viewMode={diffViewMode} />}
      </div>
    </div>
  );
};

interface DiffLineCommentData {
  filePath: string;
  lineIndex: number;
  content: string;
}

const DiffBody = ({ lines, language, viewMode = "unified", comments, onLineClick, filePath, activeCommentLine, onCommentSubmit, onCommentCancel }: { lines: DiffLine[]; language?: string; viewMode?: "unified" | "split"; comments?: DiffLineCommentData[]; onLineClick?: (lineIndex: number) => void; filePath?: string; activeCommentLine?: number | null; onCommentSubmit?: (lineIndex: number, text: string) => void; onCommentCancel?: () => void }) => {
  if (viewMode === "split") return <SplitDiffBody lines={lines} language={language} comments={comments} onLineClick={onLineClick} filePath={filePath} activeCommentLine={activeCommentLine} onCommentSubmit={onCommentSubmit} onCommentCancel={onCommentCancel} />;
  return <UnifiedDiffBody lines={lines} language={language} comments={comments} onLineClick={onLineClick} filePath={filePath} activeCommentLine={activeCommentLine} onCommentSubmit={onCommentSubmit} onCommentCancel={onCommentCancel} />;
};

const UnifiedDiffBody = ({ lines, language, comments, onLineClick, filePath, activeCommentLine, onCommentSubmit, onCommentCancel }: { lines: DiffLine[]; language?: string; comments?: DiffLineCommentData[]; onLineClick?: (lineIndex: number) => void; filePath?: string; activeCommentLine?: number | null; onCommentSubmit?: (lineIndex: number, text: string) => void; onCommentCancel?: () => void }) => {
  const lang = language ?? guessLanguageFromPath(extractFilePathFromDiff(lines));
  const colorized = useColorizedLines(lines, lang);
  const commentMap = useMemo(() => {
    if (!comments) return new Map<number, DiffLineCommentData>();
    const map = new Map<number, DiffLineCommentData>();
    for (const c of comments) {
      if (!filePath || c.filePath === filePath) map.set(c.lineIndex, c);
    }
    return map;
  }, [comments, filePath]);

  return (
    <div style={{ flex: 1, minHeight: 0, overflow: "auto", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", lineHeight: 1.55 }}>
      {lines.map((line, i) => (
        <div key={i}>
          <div
            style={{
              background: bgForKind(line.kind),
              padding: "0 10px",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              borderLeft:
                line.kind === "add"
                  ? "2px solid var(--state-success)"
                  : line.kind === "del"
                    ? "2px solid var(--state-danger)"
                    : "2px solid transparent",
              color: (line.kind === "hunk" || line.kind === "meta" || !colorized?.[i]) ? colorForKind(line.kind) : undefined,
              cursor: (line.kind === "add" || line.kind === "del" || line.kind === "context") && onLineClick ? "pointer" : undefined,
              position: "relative",
            }}
            onClick={() => {
              if (onLineClick && (line.kind === "add" || line.kind === "del" || line.kind === "context")) {
                onLineClick(i);
              }
            }}
          >
            <span style={{ color: colorForKind(line.kind), userSelect: "none" }}>
              {line.kind === "add" ? "+" : line.kind === "del" ? "-" : " "}
            </span>
            {colorized?.[i] ? (
              <span dangerouslySetInnerHTML={{ __html: colorized[i] }} />
            ) : (
              line.text
            )}
            {commentMap.has(i) && (
              <span style={{ marginLeft: 8, color: "var(--accent-primary)", fontSize: "var(--text-xs)" }} title={commentMap.get(i)!.content}>
                <MessageCircle size={12} style={{ display: "inline", verticalAlign: "middle" }} />
              </span>
            )}
          </div>
          {commentMap.has(i) && (
            <div style={{ background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-base))", borderLeft: "3px solid var(--accent-primary)", padding: "4px 10px 4px 14px", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
              {commentMap.get(i)!.content}
            </div>
          )}
          {activeCommentLine === i && onCommentSubmit && (
            <InlineCommentInput lineIndex={i} onSubmit={onCommentSubmit} onCancel={onCommentCancel} />
          )}
        </div>
      ))}
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
    <div style={{ borderLeft: "3px solid var(--accent-primary)", background: "color-mix(in oklch, var(--accent-primary) 5%, var(--surface-base))", padding: "6px 10px 6px 14px", display: "flex", gap: 6, alignItems: "center" }}>
      <input
        type="text"
        placeholder={`Comment on line ${lineIndex + 1}...`}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && text.trim()) { e.preventDefault(); onSubmit(lineIndex, text.trim()); } if (e.key === "Escape") onCancel?.(); }}
        style={{ flex: 1, border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 4px)", padding: "4px 8px", fontSize: "var(--text-xs)", background: "var(--surface-base)", color: "var(--text-primary)", outline: "none" }}
        autoFocus
      />
      <button onClick={() => { if (text.trim()) onSubmit(lineIndex, text.trim()); }} style={{ border: "1px solid var(--accent-primary)", background: "var(--accent-primary)", color: "var(--text-on-accent)", borderRadius: "var(--radius-sm, 4px)", padding: "3px 8px", fontSize: "var(--text-xs)", cursor: "pointer" }}>Add</button>
      <button onClick={onCancel} style={{ border: "1px solid var(--border-subtle)", background: "transparent", color: "var(--text-muted)", borderRadius: "var(--radius-sm, 4px)", padding: "3px 8px", fontSize: "var(--text-xs)", cursor: "pointer" }}>Cancel</button>
    </div>
  );
};

const SplitDiffBody = ({ lines, language, comments, onLineClick, filePath, activeCommentLine, onCommentSubmit, onCommentCancel }: { lines: DiffLine[]; language?: string; comments?: DiffLineCommentData[]; onLineClick?: (lineIndex: number) => void; filePath?: string; activeCommentLine?: number | null; onCommentSubmit?: (lineIndex: number, text: string) => void; onCommentCancel?: () => void }) => {
  const lang = language ?? guessLanguageFromPath(extractFilePathFromDiff(lines));
  const colorized = useColorizedLines(lines, lang);
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
    <div style={{ flex: 1, minHeight: 0, overflow: "auto", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", lineHeight: 1.55 }}>
      {rows.map((row, i) => {
        const lineIdx = row.rightIndex ?? row.leftIndex;
        const hasComment = lineIdx != null && commentMap.has(lineIdx);
        const isActiveComment = lineIdx != null && activeCommentLine === lineIdx;
        return (
          <div key={i}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr" }}>
              <SplitRowPair row={row} colorizedMap={colorizedMap} onLineClick={onLineClick} />
            </div>
            {hasComment && (
              <div style={{ background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-base))", borderLeft: "3px solid var(--accent-primary)", padding: "4px 10px 4px 14px", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
                {commentMap.get(lineIdx!)!.content}
              </div>
            )}
            {isActiveComment && onCommentSubmit && (
              <InlineCommentInput lineIndex={lineIdx!} onSubmit={onCommentSubmit} onCancel={onCommentCancel} />
            )}
          </div>
        );
      })}
    </div>
  );
};

const SplitRowPair = ({ row, colorizedMap, onLineClick }: { row: SplitRow; colorizedMap: Map<DiffLine, string> | null; onLineClick?: (lineIndex: number) => void }) => {
  const renderCell = (line: DiffLine | null, side: "left" | "right", lineIndex?: number) => {
    if (!line) {
      return <div style={{ padding: "0 8px", background: "var(--surface-soft)", minHeight: "1.55em" }} />;
    }
    if (line.kind === "hunk" || line.kind === "meta") {
      return (
        <div style={{ padding: "0 8px", background: bgForKind(line.kind), color: colorForKind(line.kind), whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
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
        style={{ padding: "0 8px", background: bg, whiteSpace: "pre-wrap", wordBreak: "break-word", cursor: clickable ? "pointer" : undefined }}
        onClick={clickable ? () => onLineClick(lineIndex) : undefined}
      >
        {html ? <span dangerouslySetInnerHTML={{ __html: html }} /> : <span style={{ color: colorForKind(line.kind) }}>{line.text}</span>}
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

// ── Tab Button ───────────────────────────────────────────────────

const TabButton = ({ active, onClick, badge, children }: { active: boolean; onClick: () => void; badge?: number; children: React.ReactNode }) => (
  <button
    onClick={onClick}
    style={{
      border: 0,
      borderBottom: active ? "2px solid var(--accent-primary)" : "2px solid transparent",
      background: "transparent",
      color: active ? "var(--text-primary)" : "var(--text-muted)",
      fontSize: "var(--text-xs)",
      padding: "7px 10px 5px",
      cursor: "pointer",
      display: "inline-flex",
      alignItems: "center",
      gap: 5,
    }}
  >
    {children}
    {badge != null && badge > 0 && (
      <span style={{ background: "var(--accent-primary)", color: "var(--text-on-accent)", borderRadius: 8, padding: "0 5px", fontSize: 10, lineHeight: "16px" }}>
        {badge}
      </span>
    )}
  </button>
);

// ── Review Tab ───────────────────────────────────────────────────

import type { DiffReviewState } from "../stores/types";

const ReviewTab = ({ diffReview, viewMode }: { diffReview: DiffReviewState | null; viewMode: DiffViewMode }) => {
  const [commentLineIndex, setCommentLineIndex] = useState<number | null>(null);

  if (!diffReview) {
    return (
      <div style={{ flex: 1, display: "grid", placeItems: "center", color: "var(--text-muted)", fontSize: "var(--text-sm)", padding: 16 }}>
        No pending review. Diffs requiring approval will appear here.
      </div>
    );
  }

  const selectedFile = diffReview.files.find((file) => file.path === diffReview.selectedPath);
  const diff = selectedFile?.patch || diffReview.diff;
  const parsed = parseUnifiedDiff(diff);
  const plus = parsed.filter((line) => line.kind === "add").length;
  const minus = parsed.filter((line) => line.kind === "del").length;
  const needsFetch = selectedFile && !selectedFile.patch;
  const comments = diffReview.lineComments ?? [];
  const isSubmitted = diffReview.status === "submitted";
  const allFilesDecided = Object.keys(diffReview.fileDecisions ?? {}).length >= diffReview.files.length;
  const handleLineClick = (lineIndex: number) => {
    setCommentLineIndex(commentLineIndex === lineIndex ? null : lineIndex);
  };

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateColumns: diffReview.files.length ? "minmax(220px, 320px) 1fr" : "1fr", minHeight: 0 }}>
      {diffReview.files.length > 0 && (
        <aside style={{ borderRight: "1px solid var(--border-subtle)", overflow: "auto", padding: 10, display: "flex", flexDirection: "column" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10, fontSize: "var(--text-xs)" }}>
            <FileDiff size={14} color="var(--accent-primary)" />
            <span style={{ flex: 1, color: "var(--text-primary)", fontWeight: 700 }}>Approval Diff</span>
            <span style={{ color: "var(--text-muted)" }}>{diffReview.files.length}</span>
          </div>
          <div style={{ flex: 1, overflow: "auto" }}>
            {diffReview.files.map((file) => {
              const decision = diffReview.fileDecisions?.[file.path];
              return (
                <div key={file.path} style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 2 }}>
                  <button
                    onClick={() => {
                      useAppStore.getState().setDiffReviewSelectedPath(file.path);
                      if (!file.patch) {
                        sendClientCommand({ type: "approval.file_diff", tool_call_id: diffReview.requestId, path: file.path });
                      }
                    }}
                    title={file.path}
                    style={{
                      flex: 1, minWidth: 0, border: 0, borderRadius: "var(--radius-sm, 4px)",
                      background: file.path === diffReview.selectedPath ? "var(--surface-active)" : "transparent",
                      color: "var(--text-secondary)", cursor: "pointer", fontSize: "var(--text-xs)", padding: "5px 6px", textAlign: "left",
                    }}
                  >
                    <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "var(--font-mono)" }}>{file.path}</div>
                    <div style={{ display: "flex", gap: 8, color: "var(--text-muted)", marginTop: 2 }}>
                      {file.additions != null && <span style={{ color: "var(--state-success)" }}>+{file.additions}</span>}
                      {file.deletions != null && <span style={{ color: "var(--state-danger)" }}>-{file.deletions}</span>}
                      {file.isLarge && <span>large</span>}
                    </div>
                  </button>
                  <button
                    title="Approve file"
                    onClick={(e) => { e.stopPropagation(); useAppStore.getState().setDiffFileDecision(file.path, "approved"); }}
                    style={{ ...fileDecisionBtnStyle, color: decision === "approved" ? "var(--text-on-accent)" : "var(--state-success)", background: decision === "approved" ? "var(--state-success)" : "transparent" }}
                  >
                    <CheckCircle size={13} />
                  </button>
                  <button
                    title="Reject file"
                    onClick={(e) => { e.stopPropagation(); useAppStore.getState().setDiffFileDecision(file.path, "rejected"); }}
                    style={{ ...fileDecisionBtnStyle, color: decision === "rejected" ? "var(--text-on-accent)" : "var(--state-danger)", background: decision === "rejected" ? "var(--state-danger)" : "transparent" }}
                  >
                    <XCircle size={13} />
                  </button>
                </div>
              );
            })}
          </div>
          {Object.keys(diffReview.fileDecisions ?? {}).length > 0 && (
            <button
              onClick={() => useAppStore.getState().submitPartialApproval()}
              disabled={!allFilesDecided || isSubmitted}
              style={{
                marginTop: 10, width: "100%", padding: "6px 10px", border: "1px solid var(--accent-primary)", borderRadius: "var(--radius-sm, 4px)",
                background: allFilesDecided ? "var(--accent-primary)" : "transparent",
                color: allFilesDecided ? "var(--text-on-accent)" : "var(--accent-primary)",
                fontSize: "var(--text-xs)",
                cursor: allFilesDecided && !isSubmitted ? "pointer" : "not-allowed",
                opacity: allFilesDecided && !isSubmitted ? 1 : 0.5,
              }}
            >
              {isSubmitted ? "Submitting..." : `Submit Review (${Object.keys(diffReview.fileDecisions ?? {}).length}/${diffReview.files.length})`}
            </button>
          )}
        </aside>
      )}

      <main style={{ minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "7px 10px", borderBottom: "1px solid var(--border-subtle)", background: "var(--surface-page)", fontSize: "var(--text-xs)" }}>
          <span style={{ color: "var(--text-primary)", fontWeight: 700 }}>{diffReview.toolName || "Tool"} approval</span>
          <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>{diffReview.requestId.slice(0, 8)}</span>
          {plus > 0 && <span style={{ color: "var(--state-success)" }}>+{plus}</span>}
          {minus > 0 && <span style={{ color: "var(--state-danger)" }}>-{minus}</span>}
          <span style={{ flex: 1 }} />
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
          {isSubmitted && <span style={{ color: "var(--text-muted)" }}>Submitted</span>}
          <button disabled={isSubmitted} onClick={() => respond(diffReview.requestId, false)} style={{ ...rejectButtonStyle, opacity: isSubmitted ? 0.6 : 1 }}><X size={13} /> Reject all</button>
          <button disabled={isSubmitted} onClick={() => respond(diffReview.requestId, true)} style={{ ...acceptButtonStyle, opacity: isSubmitted ? 0.6 : 1 }}><Check size={13} /> Approve all</button>
          {comments.length > 0 && (
            <button disabled={isSubmitted} onClick={() => useAppStore.getState().submitDiffReviewWithComments()} style={{ ...acceptButtonStyle, background: "var(--accent-primary)", borderColor: "var(--accent-primary)", marginLeft: 4, opacity: isSubmitted ? 0.6 : 1 }}>
              <MessageCircle size={13} /> Review Code ({comments.length})
            </button>
          )}
        </div>
        {needsFetch ? (
          <div style={{ flex: 1, display: "grid", placeItems: "center", color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>Loading file diff...</div>
        ) : (
          <DiffBody
            lines={parsed}
            viewMode={viewMode}
            comments={comments}
            onLineClick={handleLineClick}
            filePath={diffReview.selectedPath}
            activeCommentLine={commentLineIndex}
            onCommentSubmit={(lineIndex, text) => {
              useAppStore.getState().addDiffLineComment({
                filePath: diffReview.selectedPath ?? diffReview.files[0]?.path ?? "diff",
                lineIndex,
                content: text,
              });
              setCommentLineIndex(null);
            }}
            onCommentCancel={() => setCommentLineIndex(null)}
          />
        )}
      </main>
    </div>
  );
};

// ── History Tab ──────────────────────────────────────────────────

const HistoryTab = ({ sources, viewMode }: { sources: { id: string; name: string; diff: string }[]; viewMode: DiffViewMode }) => {
  if (sources.length === 0) {
    return (
      <div style={{ flex: 1, display: "grid", placeItems: "center", color: "var(--text-muted)", fontSize: "var(--text-sm)", padding: 16 }}>
        No diffs yet. When the agent edits files, their unified diffs will show up here.
      </div>
    );
  }

  return (
    <div style={{ height: "100%", overflowY: "auto", padding: 12, display: "flex", flexDirection: "column", gap: 16 }}>
      {sources.map((d) => {
        const parsed = parseUnifiedDiff(d.diff);
        const plus = parsed.filter((l) => l.kind === "add").length;
        const minus = parsed.filter((l) => l.kind === "del").length;
        return (
          <div key={d.id} style={{ background: "var(--surface-soft)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-sm, 6px)", overflow: "hidden" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 10px", background: "var(--surface-page)", borderBottom: "1px solid var(--border-subtle)", fontSize: "var(--text-xs)" }}>
              <span style={{ fontFamily: "var(--font-mono)", color: "var(--accent-primary)" }}>{d.name}</span>
              <span style={{ color: "var(--text-muted)" }}>{d.id.slice(0, 8)}</span>
              <span style={{ flex: 1 }} />
              {plus > 0 && <span style={{ color: "var(--state-success)" }}>+{plus}</span>}
              {minus > 0 && <span style={{ color: "var(--state-danger)" }}>-{minus}</span>}
            </div>
            <DiffBody lines={parsed} viewMode={viewMode} />
          </div>
        );
      })}
    </div>
  );
};

// ── Git Changes Tab ──────────────────────────────────────────────

const GitChangesTab = ({ viewMode }: { viewMode: DiffViewMode }) => {
  const gitChanges = useAppStore((s) => s.gitChanges);
  const requestGitChanges = useAppStore((s) => s.requestGitChanges);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);

  useEffect(() => {
    requestGitChanges();
  }, [requestGitChanges]);

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

  const handleStage = (path: string) => {
    sendClientCommand({ type: "diff.git_stage_file", path });
    setTimeout(() => requestGitChanges(), 300);
  };

  const handleUnstage = (path: string) => {
    sendClientCommand({ type: "diff.git_unstage_file", path });
    setTimeout(() => requestGitChanges(), 300);
  };

  if (gitChanges.loading && allFiles.length === 0) {
    return (
      <div style={{ flex: 1, display: "grid", placeItems: "center", color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
        Loading git changes...
      </div>
    );
  }

  if (allFiles.length === 0 && gitChanges.untracked.length === 0) {
    return (
      <div style={{ flex: 1, display: "grid", placeItems: "center", color: "var(--text-muted)", fontSize: "var(--text-sm)", padding: 16 }}>
        Working tree clean. No uncommitted changes.
      </div>
    );
  }

  return (
    <div style={{ height: "100%", display: "grid", gridTemplateColumns: "minmax(200px, 280px) 1fr", minHeight: 0 }}>
      <aside style={{ borderRight: "1px solid var(--border-subtle)", overflow: "auto", padding: 10, display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "var(--text-xs)" }}>
          <GitBranch size={13} color="var(--accent-primary)" />
          <span style={{ color: "var(--text-primary)", fontWeight: 700, flex: 1 }}>Git Changes</span>
          <button onClick={requestGitChanges} title="Refresh" style={{ ...iconButtonStyle, width: 22, height: 20 }}>
            <RefreshCw size={11} className={gitChanges.loading ? "spin" : ""} />
          </button>
        </div>

        {gitChanges.staged.length > 0 && (
          <div>
            <div style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", marginBottom: 4, letterSpacing: "0.5px" }}>
              Staged ({gitChanges.staged.length})
            </div>
            {gitChanges.staged.map((f) => (
              <div key={`staged-${f.path}`} style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 2 }}>
                <button
                  onClick={() => setSelectedFile(f.path)}
                  style={{
                    flex: 1, minWidth: 0, border: 0, borderRadius: "var(--radius-sm, 4px)",
                    background: selectedFile === f.path ? "var(--surface-active)" : "transparent",
                    color: "var(--text-secondary)", cursor: "pointer", fontSize: "var(--text-xs)", padding: "4px 6px", textAlign: "left",
                  }}
                >
                  <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "var(--font-mono)" }}>{f.path}</div>
                  <div style={{ display: "flex", gap: 6, marginTop: 1 }}>
                    <span style={{ color: "var(--state-success)", fontSize: 10 }}>+{f.additions}</span>
                    <span style={{ color: "var(--state-danger)", fontSize: 10 }}>-{f.deletions}</span>
                  </div>
                </button>
                <button onClick={() => handleUnstage(f.path)} title="Unstage" style={{ ...fileDecisionBtnStyle, color: "var(--text-muted)" }}>
                  <Minus size={11} />
                </button>
              </div>
            ))}
          </div>
        )}

        {gitChanges.workingTree.length > 0 && (
          <div>
            <div style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", marginBottom: 4, letterSpacing: "0.5px" }}>
              Modified ({gitChanges.workingTree.length})
            </div>
            {gitChanges.workingTree.map((f) => (
              <div key={`wt-${f.path}`} style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 2 }}>
                <button
                  onClick={() => setSelectedFile(f.path)}
                  style={{
                    flex: 1, minWidth: 0, border: 0, borderRadius: "var(--radius-sm, 4px)",
                    background: selectedFile === f.path ? "var(--surface-active)" : "transparent",
                    color: "var(--text-secondary)", cursor: "pointer", fontSize: "var(--text-xs)", padding: "4px 6px", textAlign: "left",
                  }}
                >
                  <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "var(--font-mono)" }}>{f.path}</div>
                  <div style={{ display: "flex", gap: 6, marginTop: 1 }}>
                    <span style={{ color: "var(--state-success)", fontSize: 10 }}>+{f.additions}</span>
                    <span style={{ color: "var(--state-danger)", fontSize: 10 }}>-{f.deletions}</span>
                  </div>
                </button>
                <button onClick={() => handleStage(f.path)} title="Stage" style={{ ...fileDecisionBtnStyle, color: "var(--state-success)" }}>
                  <Plus size={11} />
                </button>
              </div>
            ))}
          </div>
        )}

        {gitChanges.untracked.length > 0 && (
          <div>
            <div style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", marginBottom: 4, letterSpacing: "0.5px" }}>
              Untracked ({gitChanges.untracked.length})
            </div>
            {gitChanges.untracked.map((path) => (
              <div key={`ut-${path}`} style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 2 }}>
                <button
                  onClick={() => useAppStore.getState().openEditorFile(path, path.split(/[/\\]/).pop())}
                  style={{
                    flex: 1, minWidth: 0, border: 0, borderRadius: "var(--radius-sm, 4px)",
                    background: "transparent", color: "var(--text-muted)", cursor: "pointer", fontSize: "var(--text-xs)", padding: "4px 6px", textAlign: "left",
                  }}
                >
                  <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "var(--font-mono)" }}>{path}</div>
                </button>
                <button onClick={() => handleStage(path)} title="Stage" style={{ ...fileDecisionBtnStyle, color: "var(--state-success)" }}>
                  <Plus size={11} />
                </button>
              </div>
            ))}
          </div>
        )}
      </aside>

      <main style={{ minWidth: 0, minHeight: 0, display: "flex", flexDirection: "column" }}>
        {selectedPatch ? (
          <DiffBody lines={parseUnifiedDiff(selectedPatch)} viewMode={viewMode} />
        ) : (
          <div style={{ flex: 1, display: "grid", placeItems: "center", color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
            Select a file to view its diff
          </div>
        )}
      </main>
    </div>
  );
};
