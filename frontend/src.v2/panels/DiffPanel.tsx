import { useI18n } from "../i18n";
import { createContext, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowLeft, ArrowUp, Check, CheckCircle, ChevronDown, ChevronLeft, ChevronRight, ExternalLink, FileDiff, GitCompare, Info, MessageCircle, Minus, Plus, RotateCcw, Rows3, X, XCircle } from "../lib/icons";
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
import { diffFileDecisionForPath, diffFilePathsEqual } from "../chat/diffReviewState";
import { returnToBrowserPage } from "../chat/openWebInBrowser";
import { workspaceFilePathsEqual, workspaceRootsEqual } from "../lib/workspace-path";
import { openWorkspaceSourceFile as openDiffSourceFile } from "../workspace/openWorkspaceFolder";
import { parsePositionedDiff as parseUnifiedDiff, type DiffLine, type DiffLineLocation } from "../lib/diff-comment-lines";
import { DiffReaderOwnerContext, useDiffAnnotations, type DiffAnnotations } from "./DiffAnnotations";
import { clearDiffReaderDrafts } from "../lib/diff-comment-storage";
import { buildDiffWordDecorations, type DiffWordSpan } from "../chat/diff/diff-word-spans";
import { decorateDiffLineHtml } from "../chat/diff/decorated-diff-syntax";
import { useWordDiffsPreference } from "../chat/diff/word-diff-preference";
import { useDiffViewPreference } from "../chat/diff/diff-view-preferences";
import { ChangesToolbarActions } from "../chat/diff/ChangesToolbarActions";
import { copyGitApplyCommand, gitApplyCommand } from "../chat/diff/copy-git-apply-command";
import { copyText } from "../lib/clipboard";
import { readFileWorkspaceApplyPatch, readFileWorkspaceBranches, readFileWorkspaceChanges, workspaceFileAbsolutePath, type FileWorkspaceComparison } from "../protocol/file-workspace";
import { ChangesFileJump, ChangedFilesTree, type ChangesFileEntry } from "../chat/diff/ChangesFileNavigation";
import { readGitReviewOptions, type GitReviewOptions } from "../chat/diff/git-review-options";
import { useGitReviewSelection } from "../chat/diff/git-review-selection";
import { compactCommitTime } from "../chat/diff/commit-relative-time";
import { reviewNeedsSingleFile } from "../chat/diff/review-file-layout";
import { useReviewExpansion } from "../chat/diff/review-expansion-state";
import { ChangesFileActions } from "../chat/diff/ChangesFileActions";
import { ChangesBranchComparison } from "../chat/diff/ChangesBranchComparison";
import { DiffFileContextMenu, useDiffRequestChanges } from "../chat/diff/DiffFileContextMenu";
import { useDesktopPreferences } from "../hooks/useDesktopPreferences";
import { openPreferredFile, preferredFileTargetForWorkspace } from "../lib/open-preferred-target";
import { runtime, type DesktopPreferences } from "../desktop/runtime";
import { buildFullDiffContext, type FullDiffContext } from "../chat/diff/full-content-metadata";
import { useGitDiffContents } from "../chat/diff/use-git-diff-contents";
import { diffPreviewKind, GitDiffMediaPreview } from "../chat/diff/GitDiffMediaPreview";
import type { GitChangeFile, GitComparisonState, GitDiffSource, GitDiffSnapshot, GitReviewRequest, GitReviewSelection, WorkspaceFileOwner } from "../stores/types";
import { filterImportOnlyDiffHunks, type DiffPresentationAnnotation } from "../chat/diff/file-diff-presentation";
import { useDiffPresentationParser } from "../chat/diff/diff-presentation-parser";
import { displayFullDiffContext, expandDiffContext, type ContextDirection, type ContextExpansion, type DiffContextGap } from "../chat/diff/full-context-display";
import { ContextMenu } from "../components/ContextMenu";
import "../chat/diff/diff-word-spans.css";
import { pushToast } from "../overlays/ToastContainer";
import { loadMessageTurnDiff } from "../chat/loadMessageTurnDiff";
import "./DiffPanel.css";
import { codeReviewComments, publishCodeReview } from "../chat/codeReview";
import { prepareCodeReview } from "../protocol/code-review";
import { MarkdownRenderer } from "../chat/messages/MarkdownRenderer";
import "./CodeReviewComments.css";
import type { CodeReviewComment } from "../lib/code-comment";
import { useDiffPalette } from "../lib/use-diff-palette";
import { diffPaletteStyle } from "../lib/diff-palette";

type DiffViewMode = "unified" | "split" | "auto";
type ChangeScope = "review" | "history" | "git";
const NO_COLORIZED_LINES: DiffLine[] = [];

const visibleDiffLines = (lines: DiffLine[]): DiffLine[] =>
  lines.filter((line) => line.kind !== "meta");

const colorForKind = (kind: DiffLine["kind"]): string => {
  switch (kind) {
    case "add":
      return "var(--diff-view-foreground)";
    case "del":
      return "var(--diff-view-foreground)";
    case "hunk":
      return "var(--accent-primary)";
    case "meta":
      return "var(--text-muted)";
    default:
      return "var(--diff-view-foreground)";
  }
};

const bgForKind = (kind: DiffLine["kind"]): string => {
  if (kind === "add") return "var(--diff-added-background)";
  if (kind === "del") return "var(--diff-removed-background)";
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

interface ChangesNavigationState {
  openPaths: Set<string>;
  setFileOpen: (path: string, open: boolean) => void;
  jump: { path: string; nonce: number } | null;
  onActivate: (path: string) => void;
  visiblePath?: string;
}
const ChangesNavigationContext = createContext<ChangesNavigationState | null>(null);
const DiffOpenPreferencesContext = createContext<DesktopPreferences | null>(null);
const FileWorkspaceReviewContext = createContext<{ owner: WorkspaceFileOwner; root: string } | null>(null);
const NO_CHANGE_FILES: GitChangeFile[] = [];
const sourceLabels = {
  branch: "此分支", uncommitted: "未提交", unstaged: "未暂存", staged: "已暂存", commit: "提交",
} as const;
const patchDeletesFile = (patch: string) => /^deleted file mode |^\+\+\+ \/dev\/null/m.test(patch);

export const DiffPanel = ({ readerOwnerId }: { readerOwnerId?: string } = {}) => {
  const request = useAppStore(state => state.gitReviewRequest);
  const conversationId = useAppStore(state => state.conversationId);
  const workspaceRoot = useAppStore(state => state.workingDirectory);
  if (request?.fileOwner && request.conversationId === conversationId) {
    const owner = request.fileOwner;
    if (!owner.conversationId || owner.conversationId !== conversationId || !workspaceRootsEqual(request.workspaceRoot, workspaceRoot)) {
      return <FileWorkspaceChanges key={JSON.stringify([owner.scopeId, owner.conversationId, owner.executionCwd, request.workspaceRoot])}
        owner={owner} request={request} readerOwnerId={readerOwnerId} />;
    }
  }
  return <ConversationDiffPanel readerOwnerId={readerOwnerId} />;
};

const ConversationDiffPanel = ({ readerOwnerId }: { readerOwnerId?: string } = {}) => {
  const localReaderId = useId();
  const readerId = readerOwnerId ?? localReaderId;
  useEffect(() => () => { if (readerOwnerId === undefined) clearDiffReaderDrafts(readerId); }, [readerOwnerId, readerId]);
  const { t: tr, locale } = useI18n();
  const { palette, variant } = useDiffPalette();
  const { preferences } = useDesktopPreferences();
  const messages = useAppStore(s => s.messages);
  const diffReview = useAppStore(s => s.diffReview);
  const gitChanges = useAppStore(s => s.gitChanges);
  const conversationId = useAppStore(s => s.conversationId);
  const workspaceRoot = useAppStore(s => s.workingDirectory);
  const pendingSwitch = useAppStore(s => s.pendingConversationSwitchId);
  const pendingCreate = useAppStore(s => s.pendingConversationCreateId);
  const turnDiff = useAppStore(s => s.conversationId ? s.turnDiffs[s.conversationId] : undefined);
  const gitReviewRequest = useAppStore(s => s.gitReviewRequest);
  const requestGitComparison = useAppStore(s => s.requestGitComparison);
  const latestReviewInput = [...messages].reverse().find(message => message.role === "user" && message.codeReview);
  const modelComments = useMemo(() => codeReviewComments(messages, diffReview?.workspaceRoot ?? ""), [messages, diffReview?.workspaceRoot]);
  const displayedReview = diffReview?.codeReview ? { ...diffReview, modelComments } : diffReview;
  const [reviewSelection, setReviewSelection] = useGitReviewSelection(conversationId ?? "", workspaceRoot);
  const selection: GitReviewSelection = { ...reviewSelection, source: reviewSelection.source === "last-turn" ? "branch" : reviewSelection.source };
  const setSelection = (value: GitReviewSelection | ((current: GitReviewSelection) => GitReviewSelection)) =>
    setReviewSelection(typeof value === "function" ? value(selection) : value);
  const ownerKey = JSON.stringify([conversationId, workspaceRoot]);
  const [scopeState, setScopeState] = useState<{ key: string; scope: ChangeScope }>();
  const activeScope = scopeState?.key === ownerKey ? scopeState.scope : diffReview ? "review" : reviewSelection.source === "last-turn" ? "history" : "git";
  const setActiveScope = (value: ChangeScope | ((scope: ChangeScope) => ChangeScope)) =>
    setScopeState({ key: ownerKey, scope: typeof value === "function" ? value(activeScope) : value });
  const [scopeMenuOpen, setScopeMenuOpen] = useState(false);
  const [menuNow, setMenuNow] = useState(() => new Date());
  useEffect(() => {
    if (!scopeMenuOpen) return;
    setMenuNow(new Date());
    const timer = window.setInterval(() => setMenuNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, [scopeMenuOpen]);
  const scopeButton = useRef<HTMLButtonElement>(null);
  const [diffViewMode, setDiffViewMode] = useState<DiffViewMode>(() => JSON.parse(localStorage.getItem("editorDiffViewMode") ?? '"auto"'));
  useEffect(() => { localStorage.setItem("editorDiffViewMode", JSON.stringify(diffViewMode)); }, [diffViewMode]);
  const [wrap, setWrap] = useDiffViewPreference("wrap");
  const [filesVisible, setFilesVisible] = useDiffViewPreference("filesVisible");
  const [hideWhitespace] = useDiffViewPreference("hideWhitespace");
  const root = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(root.current!); return () => observer.disconnect();
  }, []);
  const [optionsState, setOptionsState] = useState<{ key: string; owner: string; data?: GitReviewOptions; error?: string }>();
  const [refresh, setRefresh] = useState(0);
  const optionsKey = JSON.stringify([conversationId, workspaceRoot, selection.baseBranch, refresh]);
  const options = optionsState?.key === optionsKey ? optionsState.data : undefined;
  const branchOptions = optionsState?.owner === ownerKey ? optionsState.data : undefined;
  useEffect(() => {
    if (activeScope !== "git" && !scopeMenuOpen || !conversationId || !workspaceRoot || pendingSwitch || pendingCreate) return;
    let current = true;
    readGitReviewOptions(conversationId, workspaceRoot, selection.baseBranch).then(data => {
      if (current) setOptionsState({ key: optionsKey, owner: ownerKey, data });
    }, error => { if (current) setOptionsState({ key: optionsKey, owner: ownerKey, error: error.message }); });
    return () => { current = false; };
  }, [optionsKey, activeScope, scopeMenuOpen, pendingSwitch, pendingCreate]);
  useEffect(() => {
    if (activeScope !== "git" || pendingSwitch || pendingCreate || selection.source === "commit" && !selection.commitSha) return;
    requestGitComparison(selection);
  }, [activeScope, selection.source, selection.baseBranch, selection.commitSha, conversationId, workspaceRoot, pendingSwitch, pendingCreate, hideWhitespace]);
  useEffect(() => {
    if (reviewSelection.source === "commit" && (!reviewSelection.commitSha
      || options && !options.commits.some(commit => commit.sha === reviewSelection.commitSha))) {
      setReviewSelection(current => ({ ...current, source: "branch", commitSha: undefined }));
    }
  }, [reviewSelection.source, reviewSelection.commitSha, options]);
  useEffect(() => {
    if (diffReview) setActiveScope("review");
    else setActiveScope(scope => scope === "review" ? "git" : scope);
  }, [diffReview?.requestId]);
  useEffect(() => {
    if (diffReview || !conversationId || !latestReviewInput?.codeReview) return;
    const scope = latestReviewInput.codeReview;
    let active = true;
    prepareCodeReview(conversationId, scope.cwd, scope.mode, scope.baseBranch, scope.mergeBaseSha)
      .then(prepared => { if (active) publishCodeReview(prepared, conversationId, latestReviewInput.id); })
      .catch(error => { if (active) useAppStore.getState().setDiffReviewState({
        requestId: `code-review:${latestReviewInput.id}`, conversationId, workspaceRoot: scope.gitRoot, codeReview: scope,
        mode: "view", status: "error", error: error.message, diff: "", files: [], fileDecisions: {},
      }); });
    return () => { active = false; };
  }, [conversationId, latestReviewInput?.id, diffReview?.requestId]);
  const historySources = useMemo(() => buildReviewHistory(messages, turnDiff, conversationId ?? undefined), [messages, turnDiff, conversationId]);
  const historyTurn = historySources[0];
  const visibleScope = activeScope === "review" && !diffReview ? "git" : activeScope;
  const comparison = gitChanges.comparison?.source === selection.source
    && gitChanges.comparison.baseBranch === selection.baseBranch && gitChanges.comparison.commitSha === selection.commitSha
    ? gitChanges.comparison : undefined;
  const files: ChangesFileEntry[] = useMemo(() => {
    if (visibleScope === "review" && displayedReview) {
      const reviewFiles = displayedReview.files.length ? displayedReview.files : [{ path: extractFilePathFromDiff(parseUnifiedDiff(displayedReview.diff)) || "Diff", patch: displayedReview.diff }];
      const comments = displayedReview.modelComments ?? [];
      return [...reviewFiles, ...comments.filter((comment, index) => !reviewFiles.some(file => diffFilePathsEqual(file.path, comment.file, displayedReview.workspaceRoot))
        && comments.findIndex(other => diffFilePathsEqual(other.file, comment.file, displayedReview.workspaceRoot)) === index).map(comment => ({ path: comment.file }))]
        .map(file => ({ ...file, commentCount: comments.filter(comment => diffFilePathsEqual(comment.file, file.path, displayedReview.workspaceRoot)).length }));
    }
    if (visibleScope === "history") return historyTurn?.files ?? [];
    return comparison?.files ?? [];
  }, [visibleScope, displayedReview, historyTurn, comparison]);
  const totals = useMemo(() => {
    const entries: { patch?: string | null; additions?: number | null; deletions?: number | null }[] = visibleScope === "review"
      ? diffReview?.files.length ? diffReview.files : [{ patch: diffReview?.diff }]
      : visibleScope === "history" ? historyTurn?.files.flatMap<{ patch?: string; additions?: number; deletions?: number }>(file => file.revisions.length
        ? file.revisions.map(revision => ({ patch: revision.diff })) : [{ additions: file.additions, deletions: file.deletions }]) ?? []
        : comparison?.files ?? [];
    return entries.reduce((sum, file) => {
      const lines = parseUnifiedDiff(file.patch ?? "");
      sum.plus += file.additions ?? lines.filter(line => line.kind === "add").length;
      sum.minus += file.deletions ?? lines.filter(line => line.kind === "del").length;
      return sum;
    }, { plus: 0, minus: 0 });
  }, [visibleScope, diffReview, historyTurn, comparison]);
  const expansion = useReviewExpansion(conversationId ?? readerId);
  const deletedPaths = new Set(visibleScope === "git" ? comparison?.files.filter(file => file.status === "D").map(file => file.path)
    : visibleScope === "history" ? historyTurn?.files.filter(file => patchDeletesFile(file.revisions.at(-1)?.diff ?? "")).map(file => file.path)
      : diffReview?.files.filter(file => patchDeletesFile(file.patch ?? "")).map(file => file.path));
  const openPaths = new Set(files.filter(file => expansion.state.overrides.get(file.path)
    ?? (expansion.state.expanded && !deletedPaths.has(file.path))).map(file => file.path));
  const [jump, setJump] = useState<{ path: string; nonce: number } | null>(null);
  const activePath = expansion.state.activePath;
  const setActivePath = expansion.setActivePath;
  const activeFilePath = files.some(file => file.path === activePath) ? activePath : files[0]?.path;
  const diffBytes = useMemo(() => {
    const patches = visibleScope === "git" ? comparison?.files.map(file => file.patch ?? "") ?? []
      : visibleScope === "history" ? historyTurn?.files.flatMap(file => file.revisions.map(revision => revision.diff)) ?? []
        : diffReview?.files.length ? diffReview.files.map(file => file.patch ?? "") : [diffReview?.diff ?? ""];
    return patches.reduce((sum, patch) => sum + new TextEncoder().encode(patch).length, 0);
  }, [visibleScope, comparison, historyTurn, diffReview]);
  const singleFile = reviewNeedsSingleFile(files.length, totals.plus + totals.minus, diffBytes);
  const [jumpAnchor, setJumpAnchor] = useState<HTMLElement | null>(null);
  useEffect(() => { setJumpAnchor(null); setScopeMenuOpen(false); }, [ownerKey]);
  const scopeKey = JSON.stringify([conversationId, workspaceRoot, visibleScope,
    visibleScope === "git" ? selection : visibleScope === "review" ? diffReview?.requestId : historyTurn?.id]);
  useLayoutEffect(() => { setJump(null); }, [ownerKey]);
  const jumpToFile = (path: string) => {
    expansion.setFileOpen(path, true); setActivePath(path);
    setJump(value => ({ path, nonce: (value?.nonce ?? 0) + 1 }));
  };
  useEffect(() => {
    if (diffReview?.selectedPath && visibleScope === "review") jumpToFile(diffReview.selectedPath);
  }, [diffReview?.selectedPath, diffReview?.requestId, visibleScope]);
  useEffect(() => {
    if (!gitReviewRequest || !workspaceRootsEqual(gitReviewRequest.workspaceRoot, workspaceRoot) || gitReviewRequest.conversationId !== conversationId) return;
    setSelection({ source: gitReviewRequest.source ?? (gitReviewRequest.section === "staged" ? "staged" : "unstaged"), baseBranch: gitReviewRequest.baseBranch });
    setActiveScope("git");
    if (gitReviewRequest.path) {
      expansion.focusFile(gitReviewRequest.path);
      setJump(value => ({ path: gitReviewRequest.path, nonce: (value?.nonce ?? 0) + 1 }));
    }
  }, [gitReviewRequest, conversationId, workspaceRoot]);
  const chooseSource = (source: GitDiffSource | "last-turn", commitSha?: string) => {
    setActiveScope(source === "last-turn" ? "history" : "git");
    setReviewSelection(current => ({ source, baseBranch: current.baseBranch, commitSha: source === "commit" ? commitSha : current.commitSha }), true);
  };
  const label = visibleScope === "review" ? tr(diffReview?.mode === "view" ? "当前修改" : "待审阅")
    : visibleScope === "history" ? tr("上一轮") : selection.source === "commit"
      ? (locale === "zh-CN" ? "提交" : "Commit") : tr(sourceLabels[selection.source]);
  const selectedCommit = visibleScope === "git" && selection.source === "commit" ? options?.commits.find(commit => commit.sha === selection.commitSha) : undefined;
  const allExpanded = expansion.state.expanded;
  return <DiffReaderOwnerContext.Provider value={readerId}>
    <DiffOpenPreferencesContext.Provider value={preferences}>
    <ChangesNavigationContext.Provider value={{ openPaths, setFileOpen: expansion.setFileOpen, jump, onActivate: setActivePath, visiblePath: singleFile ? activeFilePath ?? undefined : undefined }}>
    <div ref={root} className="mc-diff-panel h-full flex flex-col min-h-0" data-diff-palette={palette ? "textmate" : "native"} style={diffPaletteStyle(palette, variant)}>
      <div className="mc-diff-toolbar flex items-center gap-2 px-2 shrink-0" style={diffToolbarStyle}>
        {diffReview?.previewReturnTarget && diffReview.conversationId === conversationId && diffReview.previewReturnTarget.conversationId === conversationId &&
          <button type="button" className="mc-diff-return-preview" title={diffReview.previewReturnTarget.url} onClick={() => {
            const target = diffReview.previewReturnTarget!;
            if (target.tab === "browser" && target.targetId && target.url) returnToBrowserPage({ conversationId: target.conversationId, targetId: target.targetId, url: target.url });
            else useAppStore.getState().setRightStackTab(target.tab);
          }}><ArrowLeft size={14} />{tr("返回预览")}</button>}
        <div className="mc-diff-leading-controls">
        <div className="mc-diff-scope-selector relative inline-flex items-center gap-1.5 min-w-0">
          <button ref={scopeButton} type="button" className="mc-diff-scope-trigger" style={scopeTriggerStyle}
            aria-label={tr("Diff 来源：{v0}{v1}", { v0: label, v1: files.length ? ` ${files.length}` : "" })}
            aria-haspopup="menu" aria-expanded={scopeMenuOpen} onClick={() => setScopeMenuOpen(open => !open)}>
            <span className="truncate">{label}</span><ChevronDown size={14} />
          </button>
          {scopeMenuOpen && <ContextMenu anchor={scopeButton.current!} returnFocusTo={scopeButton.current!} position={{ x: 0, y: 0 }}
            ariaLabel={tr("Diff 来源")} onClose={() => setScopeMenuOpen(false)} items={[
              ...diffReview ? [{ label: tr(diffReview.mode === "view" ? "当前修改" : "待审阅"), checked: visibleScope === "review", checkType: "radio" as const, onClick: () => setActiveScope("review") }] : [],
              { label: tr("上一轮"), checked: visibleScope === "history", checkType: "radio", onClick: () => chooseSource("last-turn") },
              ...(["uncommitted", "unstaged", "staged"] as const).map(source => ({ label: tr(sourceLabels[source]),
                checked: visibleScope === "git" && selection.source === source, checkType: "radio" as const, onClick: () => chooseSource(source) })),
              { label: locale === "zh-CN" ? "已提交" : "Commits", submenuMaxHeight: 320, submenu: options?.commits.length ? options.commits.map(commit => ({
                label: commit.subject, rightIcon: <span className="mc-diff-commit-age">{compactCommitTime(commit.committedAt, menuNow, locale)}<span>{locale === "zh-CN" ? "前" : "ago"}</span></span>, tooltip: commit.message,
                checked: visibleScope === "git" && selection.source === "commit" && selection.commitSha === commit.sha,
                checkType: "radio" as const, onClick: () => chooseSource("commit", commit.sha),
              })) : optionsState?.key === optionsKey && optionsState.error ? [
                { label: tr("无法加载提交"), disabled: true }, { label: tr("重试"), onClick: () => setRefresh(value => value + 1) },
              ] : [{ label: options ? tr("分支上没有提交") : tr("正在加载提交…"), disabled: true }] },
              { label: tr(sourceLabels.branch), checked: visibleScope === "git" && selection.source === "branch", checkType: "radio", onClick: () => chooseSource("branch") },
            ]} />}
          <span className="mc-diff-total-counts"><span>+{totals.plus}</span><span>-{totals.minus}</span></span>
        </div>
        {visibleScope === "git" && selection.source === "branch" && <ChangesBranchComparison currentBranch={branchOptions?.current_branch} defaultTargetBranch={branchOptions?.default_base_branch}
          targetBranch={selection.baseBranch} branches={branchOptions?.branches} loading={!branchOptions && !(optionsState?.key === optionsKey && optionsState.error)}
          error={optionsState?.key === optionsKey ? optionsState.error : undefined} onRetry={() => setRefresh(value => value + 1)}
          onSelectBranch={baseBranch => setSelection(current => ({ ...current, baseBranch }))} />}
        </div>
        <ChangesToolbarActions width={width} mode={diffViewMode} onModeChange={setDiffViewMode} wrap={wrap} onWrapChange={setWrap}
          onCopyGitApplyCommand={visibleScope === "git" && conversationId && workspaceRoot ? () => {
            void copyGitApplyCommand(conversationId, workspaceRoot, selection, tr("已复制 git apply 命令"))
              .catch(error => pushToast(error instanceof Error ? error.message : String(error), "error"));
          } : undefined}
          copyGitApplyDisabled={Boolean(pendingSwitch || pendingCreate || selection.source === "commit" && !selection.commitSha)}
          allExpanded={allExpanded} onToggleAll={() => expansion.setExpanded(!allExpanded)}
          onRefresh={visibleScope === "git" ? () => { requestGitComparison(selection); setRefresh(value => value + 1); } : undefined}
          refreshing={visibleScope === "git" && comparison?.loading} refreshDisabled={Boolean(pendingSwitch || pendingCreate || selection.source === "commit" && !selection.commitSha)}
          filesVisible={filesVisible} onToggleFiles={() => setFilesVisible(!filesVisible)} onJumpToFile={setJumpAnchor} jumpOpen={jumpAnchor !== null} />
      </div>
      {selectedCommit && <div className="mc-diff-selected-commit" aria-label={tr("选中的提交")} title={selectedCommit.message}>{selectedCommit.subject}</div>}
      <div className="mc-changes-content flex flex-1 min-h-0 overflow-hidden">
        <div className="flex flex-col flex-1 min-w-0 min-h-0 overflow-hidden">
          {singleFile && <div className="mc-diff-large-banner" role="note">
            <div><Info size={16} /><span>{tr("此差异较大，每次仅显示一个文件")}</span></div>
            <div><button type="button" aria-label={tr("上一个文件")} disabled={!activeFilePath || files.findIndex(file => file.path === activeFilePath) === 0}
              onClick={() => jumpToFile(files[files.findIndex(file => file.path === activeFilePath) - 1].path)}><ChevronLeft size={16} /></button>
            <button type="button" aria-label={tr("下一个文件")} disabled={!activeFilePath || files.findIndex(file => file.path === activeFilePath) === files.length - 1}
              onClick={() => jumpToFile(files[files.findIndex(file => file.path === activeFilePath) + 1].path)}><ChevronRight size={16} /></button></div>
          </div>}
          <div className="flex-1 min-h-0 overflow-hidden">
          {visibleScope === "review" && <ReviewTab diffReview={displayedReview} viewMode={diffViewMode} />}
          {visibleScope === "history" && <HistoryTab turn={historyTurn} turnNumber={historySources.length} viewMode={diffViewMode} />}
          {visibleScope === "git" && <GitChangesTab key={ownerKey} comparison={comparison} selection={selection} viewMode={diffViewMode} />}
          </div>
        </div>
        <ChangedFilesTree open={filesVisible} entries={files} activePath={activeFilePath} parentWidth={width} onSelect={jumpToFile} onClose={() => setFilesVisible(false)} />
      </div>
      {jumpAnchor && <ChangesFileJump anchor={jumpAnchor} entries={files} activePath={activeFilePath}
        onSelect={path => { jumpToFile(path); setJumpAnchor(null); }} onClose={() => setJumpAnchor(null)} />}
    </div>
    </ChangesNavigationContext.Provider>
    </DiffOpenPreferencesContext.Provider>
  </DiffReaderOwnerContext.Provider>;
};

const FileWorkspaceChanges = ({ owner, request, readerOwnerId }: { owner: WorkspaceFileOwner; request: GitReviewRequest; readerOwnerId?: string }) => {
  const comparisonRoot = request.workspaceRoot;
  const { t: tr, locale } = useI18n();
  const { palette, variant } = useDiffPalette();
  const { preferences } = useDesktopPreferences();
  const connected = useAppStore(state => state.isConnected);
  const version = useAppStore(state => state.fileTreeVersion);
  const localReaderId = useId();
  const readerId = readerOwnerId ?? localReaderId;
  useEffect(() => () => { if (readerOwnerId === undefined) clearDiffReaderDrafts(readerId); }, [readerOwnerId, readerId]);
  const [selection, setSelection] = useState<GitReviewSelection>(() => ({ source: request.source ?? "uncommitted", baseBranch: request.baseBranch }));
  const [refresh, setRefresh] = useState(0);
  const [optionsState, setOptionsState] = useState<{ key: string; data?: GitReviewOptions; error?: string }>();
  const optionsKey = JSON.stringify([selection.baseBranch, refresh, version]);
  const options = optionsState?.key === optionsKey ? optionsState.data : undefined;
  useEffect(() => {
    if (!connected) return;
    const operation = new AbortController();
    void readFileWorkspaceBranches(owner, operation.signal, selection.baseBranch, comparisonRoot).then(data => {
      if (!operation.signal.aborted) setOptionsState({ key: optionsKey, data });
    }, error => { if (!operation.signal.aborted) setOptionsState({ key: optionsKey, error: error.message }); });
    return () => operation.abort();
  }, [owner, connected, optionsKey]);
  const [hideWhitespace] = useDiffViewPreference("hideWhitespace");
  const [comparisonState, setComparisonState] = useState<{ key: string; data?: FileWorkspaceComparison; error?: string }>();
  const comparisonKey = JSON.stringify([selection, refresh, version, hideWhitespace]);
  const comparison = comparisonState?.key === comparisonKey ? comparisonState.data : undefined;
  const comparisonError = comparisonState?.key === comparisonKey ? comparisonState.error : undefined;
  const loading = connected && !comparison && !comparisonError && !(selection.source === "commit" && !selection.commitSha);
  useEffect(() => {
    if (!connected || selection.source === "commit" && !selection.commitSha) return;
    const operation = new AbortController();
    void readFileWorkspaceChanges(owner, selection, hideWhitespace, operation.signal, comparisonRoot).then(data => {
      if (!operation.signal.aborted) setComparisonState({ key: comparisonKey, data });
    }, error => { if (!operation.signal.aborted) setComparisonState({ key: comparisonKey, error: error.message }); });
    return () => operation.abort();
  }, [owner, connected, comparisonKey]);
  const expansion = useReviewExpansion(`files:${JSON.stringify([owner.scopeId, owner.conversationId, owner.executionCwd, comparisonRoot])}`);
  const [jump, setJump] = useState<{ path: string; nonce: number } | null>(null);
  const jumpToFile = (path: string) => {
    expansion.setFileOpen(path, true); expansion.setActivePath(path);
    setJump(value => ({ path, nonce: (value?.nonce ?? 0) + 1 }));
  };
  useEffect(() => {
    setSelection({ source: request.source ?? "uncommitted", baseBranch: request.baseBranch });
    if (request.path) jumpToFile(request.path);
  }, [request.id]);
  const files = comparison?.files ?? NO_CHANGE_FILES;
  const totals = files.reduce((total, file) => ({ plus: total.plus + file.additions, minus: total.minus + file.deletions }), { plus: 0, minus: 0 });
  const openPaths = new Set(files.filter(file => expansion.state.overrides.get(file.path)
    ?? (expansion.state.expanded && file.status !== "D")).map(file => file.path));
  const activePath = files.some(file => file.path === expansion.state.activePath) ? expansion.state.activePath : files[0]?.path;
  const singleFile = reviewNeedsSingleFile(files.length, totals.plus + totals.minus, new TextEncoder().encode(comparison?.diff ?? "").length);
  const [viewMode, setViewMode] = useState<DiffViewMode>(() => JSON.parse(localStorage.getItem("editorDiffViewMode") ?? '"auto"'));
  useEffect(() => { localStorage.setItem("editorDiffViewMode", JSON.stringify(viewMode)); }, [viewMode]);
  const [wrap, setWrap] = useDiffViewPreference("wrap");
  const [filesVisible, setFilesVisible] = useDiffViewPreference("filesVisible");
  const [scopeMenuOpen, setScopeMenuOpen] = useState(false);
  const scopeButton = useRef<HTMLButtonElement>(null);
  const [menuNow, setMenuNow] = useState(() => new Date());
  useEffect(() => {
    if (!scopeMenuOpen) return;
    setMenuNow(new Date());
    const timer = window.setInterval(() => setMenuNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, [scopeMenuOpen]);
  const [jumpAnchor, setJumpAnchor] = useState<HTMLElement | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(root.current!); return () => observer.disconnect();
  }, []);
  const chooseSource = (source: GitDiffSource, commitSha?: string) => setSelection(current => ({ ...current, source, commitSha }));
  const selectedCommit = selection.source === "commit" ? options?.commits.find(commit => commit.sha === selection.commitSha) : undefined;
  return <DiffReaderOwnerContext.Provider value={readerId}><DiffOpenPreferencesContext.Provider value={preferences}>
    <FileWorkspaceReviewContext.Provider value={{ owner, root: comparisonRoot }}>
    <ChangesNavigationContext.Provider value={{ openPaths, setFileOpen: expansion.setFileOpen, jump, onActivate: expansion.setActivePath, visiblePath: singleFile ? activePath : undefined }}>
    <div ref={root} className="mc-diff-panel h-full flex flex-col min-h-0" data-diff-palette={palette ? "textmate" : "native"} style={diffPaletteStyle(palette, variant)}>
      <div className="mc-diff-toolbar flex items-center gap-2 px-2 shrink-0" style={diffToolbarStyle}>
        <div className="mc-diff-leading-controls"><div className="mc-diff-scope-selector relative inline-flex items-center gap-1.5 min-w-0">
          <button ref={scopeButton} type="button" className="mc-diff-scope-trigger" style={scopeTriggerStyle} aria-label={tr("Diff 来源")}
            aria-haspopup="menu" aria-expanded={scopeMenuOpen} onClick={() => setScopeMenuOpen(value => !value)}>
            <span className="truncate">{tr(sourceLabels[selection.source])}</span><ChevronDown size={14} /></button>
          {scopeMenuOpen && <ContextMenu anchor={scopeButton.current!} returnFocusTo={scopeButton.current!} position={{ x: 0, y: 0 }} ariaLabel={tr("Diff 来源")} onClose={() => setScopeMenuOpen(false)} items={[
            ...(["uncommitted", "unstaged", "staged"] as const).map(source => ({ label: tr(sourceLabels[source]), checked: selection.source === source,
              checkType: "radio" as const, onClick: () => chooseSource(source) })),
            { label: locale === "zh-CN" ? "已提交" : "Commits", submenuMaxHeight: 320, submenu: options?.commits.length ? options.commits.map(commit => ({
              label: commit.subject, tooltip: commit.message, rightIcon: <span className="mc-diff-commit-age">{compactCommitTime(commit.committedAt, menuNow, locale)}<span>{locale === "zh-CN" ? "前" : "ago"}</span></span>,
              checked: selection.source === "commit" && selection.commitSha === commit.sha, checkType: "radio" as const, onClick: () => chooseSource("commit", commit.sha),
            })) : optionsState?.key === optionsKey && optionsState.error ? [{ label: tr("无法加载提交"), disabled: true }, { label: tr("重试"), onClick: () => setRefresh(value => value + 1) }]
              : [{ label: options ? tr("分支上没有提交") : tr("正在加载提交…"), disabled: true }] },
            { label: tr(sourceLabels.branch), checked: selection.source === "branch", checkType: "radio", onClick: () => chooseSource("branch") },
          ]} />}
          <span className="mc-diff-total-counts"><span>+{totals.plus}</span><span>-{totals.minus}</span></span>
        </div>{selection.source === "branch" && <ChangesBranchComparison currentBranch={options?.current_branch} defaultTargetBranch={options?.default_base_branch}
          targetBranch={selection.baseBranch} branches={options?.branches} loading={!options && !(optionsState?.key === optionsKey && optionsState.error)}
          error={optionsState?.key === optionsKey ? optionsState.error : undefined} onRetry={() => setRefresh(value => value + 1)}
          onSelectBranch={baseBranch => setSelection(current => ({ ...current, baseBranch }))} />}</div>
        <ChangesToolbarActions width={width} mode={viewMode} onModeChange={setViewMode} wrap={wrap} onWrapChange={setWrap}
          onCopyGitApplyCommand={() => { void readFileWorkspaceApplyPatch(owner, selection, comparisonRoot).then(patch => copyText(gitApplyCommand(patch), "git apply", tr("已复制 git apply 命令")))
            .catch(error => pushToast(error.message, "error")); }} copyGitApplyDisabled={loading || !comparison?.isGitRepo || comparison.status === "expected"}
          allExpanded={expansion.state.expanded} onToggleAll={() => expansion.setExpanded(!expansion.state.expanded)}
          onRefresh={() => setRefresh(value => value + 1)} refreshing={loading} refreshDisabled={!connected}
          filesVisible={filesVisible} onToggleFiles={() => setFilesVisible(!filesVisible)} onJumpToFile={setJumpAnchor} jumpOpen={jumpAnchor !== null} />
      </div>
      {selectedCommit && <div className="mc-diff-selected-commit" aria-label={tr("选中的提交")} title={selectedCommit.message}>{selectedCommit.subject}</div>}
      <div className="mc-changes-content flex flex-1 min-h-0 overflow-hidden"><div className="flex flex-col flex-1 min-w-0 min-h-0 overflow-hidden">
        {singleFile && <div className="mc-diff-large-banner" role="note"><div><Info size={16} /><span>{tr("此差异较大，每次仅显示一个文件")}</span></div><div>
          <button type="button" aria-label={tr("上一个文件")} disabled={!activePath || files.findIndex(file => file.path === activePath) === 0}
            onClick={() => jumpToFile(files[files.findIndex(file => file.path === activePath) - 1].path)}><ChevronLeft size={16} /></button>
          <button type="button" aria-label={tr("下一个文件")} disabled={!activePath || files.findIndex(file => file.path === activePath) === files.length - 1}
            onClick={() => jumpToFile(files[files.findIndex(file => file.path === activePath) + 1].path)}><ChevronRight size={16} /></button></div></div>}
        <div className="mc-diff-file-sections mc-diff-git-sections">
          {comparisonError ? <div className="mc-diff-file-loading" role="alert">{comparisonError}</div>
            : loading ? <div className="mc-diff-file-loading" role="status">{tr("正在加载 Git 更改...")}</div>
            : comparison?.isGitRepo === false ? <div className="mc-diff-file-loading">{tr("当前文件夹未启用 Git")}</div>
            : comparison?.status === "expected" ? <div className="mc-diff-file-loading">{tr(comparison.reason === "default_branch_unavailable" ? "没有可用的默认基准分支" : "无法找到所选分支的共同祖先")}</div>
            : !files.length ? <div className="mc-diff-file-loading">{tr("没有可显示的更改。")}</div> : null}
          {files.filter(file => !singleFile || file.path === activePath).map(file => <GitFileDiff key={`${selection.source}:${file.path}`} file={file} source={selection.source} viewMode={viewMode}
            open={openPaths.has(file.path)} onToggle={() => expansion.setFileOpen(file.path, !openPaths.has(file.path))} actions={null} />)}
        </div></div><ChangedFilesTree open={filesVisible} entries={files} activePath={activePath} parentWidth={width} onSelect={jumpToFile} onClose={() => setFilesVisible(false)} />
      </div>
      {jumpAnchor && <ChangesFileJump anchor={jumpAnchor} entries={files} activePath={activePath} onSelect={path => { jumpToFile(path); setJumpAnchor(null); }} onClose={() => setJumpAnchor(null)} />}
    </div></ChangesNavigationContext.Provider></FileWorkspaceReviewContext.Provider></DiffOpenPreferencesContext.Provider></DiffReaderOwnerContext.Provider>;
};

const CodeReviewCommentCard = ({ comment, filePath, workspaceRoot }: { comment: CodeReviewComment; filePath?: string; workspaceRoot?: string }) =>
  <div data-code-review-comment data-code-review-file={filePath} data-code-review-line={comment.end}>
    <MarkdownRenderer content={`${comment.title}\n\n${comment.body}`} workspaceRoot={workspaceRoot} />
  </div>;

interface DiffLineCommentData {
  filePath: string;
  lineIndex: number;
  content: string;
  models?: CodeReviewComment[];
}

interface DiffBodyProps {
  lines: DiffLine[];
  annotationSourceKey: string;
  language?: string;
  viewMode?: DiffViewMode;
  comments?: DiffLineCommentData[];
  filePath?: string;
  workspaceRoot?: string;
  conversationId?: string | null;
  annotationMode?: "conversation" | "approval";
  commentsEnabled?: boolean;
  approvalComments?: import("../stores/types").DiffLineComment[];
  rawPatch?: string;
  inline?: boolean;
  fullContext?: FullDiffContext | null;
}

interface DiffReadingProps extends DiffBodyProps {
  annotations: DiffAnnotations;
  wordSpans: DiffWordSpan[][];
  colorized: string[] | null;
  contextGaps: Map<DiffLine, DiffContextGap>;
  onExpandContext: (gap: DiffContextGap, direction: ContextDirection) => void;
}

const DiffBody = ({ lines, annotationSourceKey, language, viewMode = "unified", comments, filePath, workspaceRoot, conversationId, annotationMode, commentsEnabled = true, approvalComments, rawPatch, inline = false, fullContext }: DiffBodyProps) => {
  const [contextExpansion, setContextExpansion] = useState<ContextExpansion>({});
  const [bodyWidth, setBodyWidth] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const readingAnchor = useRef<{ index: string; offset: number } | null>(null);
  const selectedRange = useRef<{ start: string; startOffset: number; end: string; endOffset: number } | null>(null);
  const originalLines = useMemo(() => visibleDiffLines(lines), [lines]);
  const [wordDiffsEnabled] = useWordDiffsPreference();
  const [hideImports] = useDiffViewPreference("hideImports");
  const [wrap] = useDiffViewPreference("wrap");
  const parser = useDiffPresentationParser(filePath ?? "", hideImports && Boolean(fullContext));
  const lang = language ?? guessLanguageFromPath(filePath ?? extractFilePathFromDiff(lines));
  const annotations = useDiffAnnotations({ lines: fullContext?.lines ?? originalLines, rawPatch: rawPatch ?? "", filePath: filePath ?? "", workspaceRoot, conversationId,
    enabled: Boolean(annotationMode && filePath && commentsEnabled), sourceKey: annotationSourceKey,
    approvalComments: annotationMode === "approval" ? approvalComments ?? [] : undefined,
    onApprovalSave: comment => useAppStore.getState().addDiffLineComment(comment),
    onApprovalRemove: position => useAppStore.getState().removeDiffLineComment(position),
  });
  useDiffRequestChanges(annotations.requestChanges);
  const actualMode = viewMode === "auto" ? bodyWidth >= 800
    && originalLines.some(line => line.kind === "add") && originalLines.some(line => line.kind === "del") ? "split" : "unified" : viewMode;
  useLayoutEffect(() => {
    const observer = new ResizeObserver(([entry]) => setBodyWidth(entry.contentRect.width));
    observer.observe(scrollRef.current!);
    return () => observer.disconnect();
  }, []);
  const positions: DiffPresentationAnnotation[] = annotations.positions.map(position => ({
    side: position.side === "left" ? "deletions" : "additions", lineNumber: position.line,
  }));
  for (const comment of comments ?? []) {
    const line = originalLines.find(line => line.lineIndex === comment.lineIndex);
    if (line?.newLine !== undefined) positions.push({ side: "additions", lineNumber: line.newLine });
    else if (line?.oldLine !== undefined) positions.push({ side: "deletions", lineNumber: line.oldLine });
  }
  const annotationKey = positions.map(position => `${position.side}:${position.lineNumber}`).join(",");
  const presentedContext = useMemo(() => fullContext && hideImports
    ? filterImportOnlyDiffHunks(fullContext, filePath ?? "", parser, positions) : fullContext,
  [fullContext, hideImports, parser, annotationKey, filePath]);
  const displayContext = useMemo(() => presentedContext ? displayFullDiffContext(presentedContext, contextExpansion) : undefined,
    [presentedContext, contextExpansion]);
  const projectedLines = displayContext?.lines ?? originalLines;
  const changedLineCount = useMemo(() => projectedLines.filter(line => line.kind === "add" || line.kind === "del").length, [projectedLines]);
  const colorized = useColorizedLines(changedLineCount > 2000 ? NO_COLORIZED_LINES : projectedLines, lang);
  const forcePlainText = lang !== "plaintext" && colorized === null;
  const words = useMemo(() => buildDiffWordDecorations(projectedLines, { forcePlainText, wordDiffsEnabled,
    unifiedLineCount: presentedContext?.unifiedLineCount, splitLineCount: presentedContext?.splitLineCount,
  }), [projectedLines, forcePlainText, wordDiffsEnabled, presentedContext]);
  useEffect(() => { setContextExpansion({}); }, [fullContext, hideImports]);
  useEffect(() => {
    readingAnchor.current = null;
    selectedRange.current = null;
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [filePath, rawPatch]);
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
    if (!selection) return;
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
  }, [actualMode]);
  const props: DiffReadingProps = { lines: projectedLines, annotationSourceKey, language, comments, filePath, workspaceRoot, annotations, wordSpans: words.spansByLine,
    colorized, inline,
    contextGaps: displayContext?.gaps ?? new Map(),
    onExpandContext: (gap, direction) => setContextExpansion(current => expandDiffContext(current, gap.key, direction)),
  };
  return <div ref={scrollRef} data-wrap={wrap || undefined} className={`mc-diff-reading-surface flex-1 min-h-0 overflow-auto${inline ? " mc-diff-reading-inline" : ""}`} onScroll={() => {
    const surface = scrollRef.current!;
    const top = surface.getBoundingClientRect().top;
    const row = [...surface.querySelectorAll<HTMLElement>("[data-diff-index]")].find((candidate) => candidate.getBoundingClientRect().bottom > top);
    if (row) readingAnchor.current = { index: row.dataset.diffIndex!, offset: row.getBoundingClientRect().top - top };
  }}>
    {actualMode === "split" ? <SplitDiffBody {...props} /> : <UnifiedDiffBody {...props} />}
  </div>;
};

const UnifiedDiffBody = ({ lines, comments, filePath, workspaceRoot, annotations, wordSpans, colorized, inline, contextGaps, onExpandContext }: DiffReadingProps) => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
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
        if (line.kind === "hunk") return <DiffHunkDivider key={`hunk-${colorIndex}`} line={line} inline={inline}
          gap={contextGaps.get(line)} onExpand={onExpandContext} />;
        const i = line.lineIndex ?? `full:${line.unifiedIndex}`;
        const commentIndex = line.lineIndex;
        return (
        <div key={i} data-diff-index={i}>
          <div className="mc-diff-code-row" data-diff-kind={line.kind}
            data-diff-line-number={line.newLine ?? line.oldLine} data-diff-line-side={line.newLine !== undefined ? "right" : "left"}
            data-diff-selected={lineLocations(line).some(annotations.selected) || undefined}
            onPointerEnter={() => { const location = lineLocations(line).at(-1); if (location) annotations.enter(location); }} style={{ background: bgForKind(line.kind) }}>
          <div className="mc-diff-gutter-cell"><DiffLineGutter line={line} filePath={filePath} workspaceRoot={workspaceRoot} annotations={annotations} side={inline ? line.kind === "del" ? "old" : "new" : undefined} /></div>
          <div
            className="mc-diff-code-content px-2.5 whitespace-pre-wrap break-words relative"
            style={{
              borderLeft:
                line.kind === "add"
                  ? "2px solid var(--diff-added-foreground)"
                  : line.kind === "del"
                    ? "2px solid var(--diff-removed-foreground)"
                    : "2px solid transparent",
              color: !colorized?.[colorIndex] ? colorForKind(line.kind) : undefined,
            }}
          >
            <span className="mc-diff-code-sign select-none" aria-hidden="true" style={{ color: line.kind === "add" ? "var(--diff-added-foreground)" : line.kind === "del" ? "var(--diff-removed-foreground)" : colorForKind(line.kind) }}>
              {line.kind === "add" ? "+" : line.kind === "del" ? "-" : " "}
            </span>
            <span data-diff-text={`${i}:${line.newLine === undefined ? "old" : "new"}`}
              dangerouslySetInnerHTML={{ __html: decorateDiffLineHtml(line.text, colorized?.[colorIndex], wordSpans[colorIndex]) }} />
            {commentIndex !== undefined && commentMap.has(commentIndex) && (
              <span className="ml-2 inline align-middle" style={{ color: "var(--accent-primary)", fontSize: "var(--text-xs)" }} title={commentMap.get(commentIndex)!.content}>
                <MessageCircle size={14} style={{ display: "inline", verticalAlign: "middle" }} />
              </span>
            )}
          </div>
          </div>
          {commentIndex !== undefined && commentMap.has(commentIndex) && (
            <div className="py-1 px-2.5 pl-3.5" style={{ background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-base))", borderLeft: "3px solid var(--accent-primary)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
              {commentMap.get(commentIndex)!.models ? commentMap.get(commentIndex)!.models!.map((comment, index) => <CodeReviewCommentCard key={index} comment={comment} filePath={filePath} workspaceRoot={workspaceRoot} />) : commentMap.get(commentIndex)!.content}
            </div>
          )}
          {annotations.render(lineLocations(line))}
        </div>
      ); })}
    </div>
  );
};

interface SplitRow {
  left: DiffLine | null;
  right: DiffLine | null;
  leftIndex?: number;
  rightIndex?: number;
}

const DiffHunkDivider = ({ line, inline, gap, onExpand }: { line: DiffLine; inline?: boolean; gap?: DiffContextGap;
  onExpand?: (gap: DiffContextGap, direction: ContextDirection) => void }) => {
  const { t: tr } = useI18n();
  if (gap) {
    if (gap.remaining === 0) return null;
    const expand = (direction: ContextDirection, event: React.MouseEvent) => onExpand!(gap, event.shiftKey ? "all" : direction);
    return <div className="mc-diff-hunk-divider mc-diff-context-divider">
      <button type="button" className="mc-diff-context-label" onClick={event => expand("both", event)}>
        {gap.remaining.toLocaleString()} {tr("行未修改")}
      </button>
      <span className="mc-diff-context-actions">
        {gap.remaining > 20 ? <>
          {gap.allowUp && <button type="button" aria-label={tr("向上展开 20 行")} title={tr("向上展开 20 行")} onClick={event => expand("up", event)}><ArrowUp size={14} /></button>}
          {gap.allowDown && <button type="button" aria-label={tr("向下展开 20 行")} title={tr("向下展开 20 行")} onClick={event => expand("down", event)}><ArrowDown size={14} /></button>}
        </> : <button type="button" aria-label={tr("展开未修改的行")} title={tr("展开未修改的行")}
          onClick={event => expand(gap.allowUp && gap.allowDown ? "both" : gap.allowUp ? "up" : "down", event)}><Rows3 size={14} /></button>}
        {gap.remaining > 20 && <button type="button" onClick={event => expand("all", event)}>{tr("展开全部")}</button>}
      </span>
    </div>;
  }
  const omitted = Math.max(line.oldGap ?? 0, line.newGap ?? 0);
  if (omitted <= 0) return null;
  return <div className="mc-diff-hunk-divider"><span>{omitted.toLocaleString()} {tr("行未修改")}</span></div>;
};

const lineLocations = (line: DiffLine): DiffLineLocation[] => [
  ...(line.oldLine !== undefined ? [{ side: "left" as const, line: line.oldLine }] : []),
  ...(line.newLine !== undefined ? [{ side: "right" as const, line: line.newLine }] : []),
];

const DiffLineGutter = ({ line, filePath, workspaceRoot, side, annotations }: { line: DiffLine; filePath?: string; workspaceRoot?: string; side?: "old" | "new"; annotations: DiffAnnotations }) => {
  const { t: tr } = useI18n();
  const external = useExternalEditor(filePath ?? "", workspaceRoot);
  if (line.oldLine === undefined && line.newLine === undefined) return null;
  return (
    <span className="mc-diff-line-gutter" data-side={side ?? "both"}>
      {side !== "new" && <span className="mc-diff-gutter-side" onPointerEnter={() => { if (line.oldLine !== undefined) annotations.enter({ side: "left", line: line.oldLine }); }}>
        {line.oldLine !== undefined && annotations.utility({ side: "left", line: line.oldLine })}
        <span title={tr("原文件行号")} onClick={event => {
          if ((event.ctrlKey || event.metaKey) && line.oldLine !== undefined && filePath) external?.(line.oldLine);
        }}>{line.oldLine ?? ""}</span>
      </span>}
      {side !== "old" && <span className="mc-diff-gutter-side" onPointerEnter={() => { if (line.newLine !== undefined) annotations.enter({ side: "right", line: line.newLine }); }}>
        {line.newLine !== undefined && annotations.utility({ side: "right", line: line.newLine })}
        {filePath && workspaceRoot !== "" && line.newLine !== undefined ? (
        <button
          type="button"
          title={workspaceRoot && !workspaceRootsEqual(workspaceRoot, useAppStore.getState().workingDirectory) ? tr("在来源工作区打开") : tr("跳到当前代码")}
          aria-label={tr("在编辑器中打开 {v0} 第 {v1} 行", { v0: filePath, v1: line.newLine })}
          onClick={event => {
            if (event.ctrlKey || event.metaKey) external?.(line.newLine!);
            else openDiffSourceFile(filePath, workspaceRoot, line.newLine);
          }}
        >
          {line.newLine}
        </button>
      ) : <span title={tr("当前文件行号")}>{line.newLine ?? ""}</span>}
      </span>}
    </span>
  );
};

const SplitDiffBody = ({ lines, comments, filePath, workspaceRoot, annotations, wordSpans, colorized, contextGaps, onExpandContext }: DiffReadingProps) => {
  const workingDirectory = useAppStore((s) => s.workingDirectory);
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
    const map = new Map<DiffLine, string>();
    lines.forEach((line, i) => {
      map.set(line, decorateDiffLineHtml(line.text, colorized?.[i], wordSpans[i]));
    });
    return map;
  }, [colorized, lines, wordSpans]);

  return (
    <div className="mc-diff-code-body">
      {rows.map((row, i) => {
        if (row.left?.kind === "hunk") return <DiffHunkDivider key={i} line={row.left} gap={contextGaps.get(row.left)} onExpand={onExpandContext} />;
        const commentLineIndices = row.leftIndex === row.rightIndex
          ? [row.leftIndex]
          : [row.leftIndex, row.rightIndex];
        return (
          <div key={i}>
            <div className="grid grid-cols-2">
              <SplitRowPair row={row} colorizedMap={colorizedMap} annotations={annotations} filePath={filePath} workspaceRoot={workspaceRoot} />
            </div>
            {commentLineIndices.map((lineIdx) => lineIdx != null && commentMap.has(lineIdx) && (
              <div key={lineIdx} className="py-1 px-2.5 pl-3.5" style={{ background: "color-mix(in oklch, var(--accent-primary) 8%, var(--surface-base))", borderLeft: "3px solid var(--accent-primary)", fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
                {commentMap.get(lineIdx)!.models ? commentMap.get(lineIdx)!.models!.map((comment, index) => <CodeReviewCommentCard key={index} comment={comment} filePath={filePath} workspaceRoot={workspaceRoot} />) : commentMap.get(lineIdx)!.content}
              </div>
            ))}
            {annotations.render([
              ...(row.left?.oldLine !== undefined ? [{ side: "left" as const, line: row.left.oldLine }] : []),
              ...(row.right?.newLine !== undefined ? [{ side: "right" as const, line: row.right.newLine }] : []),
            ])}
          </div>
        );
      })}
    </div>
  );
};

const SplitRowPair = ({ row, colorizedMap, annotations, filePath, workspaceRoot }: { row: SplitRow; colorizedMap: Map<DiffLine, string>; annotations: DiffAnnotations; filePath?: string; workspaceRoot?: string }) => {
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
      ? "var(--diff-removed-background)"
      : side === "right" && line.kind === "add"
        ? "var(--diff-added-background)"
        : "transparent";
    const html = colorizedMap?.get(line);
    const readingIndex = lineIndex ?? `full:${line.unifiedIndex}`;
    const lineNumber = side === "left" ? line.oldLine : line.newLine;
    const location = lineNumber === undefined ? undefined : { side, line: lineNumber };
    return (
      <div className="mc-diff-code-row" data-diff-kind={line.kind} data-diff-index={readingIndex}
        data-diff-line-number={lineNumber} data-diff-line-side={side}
        data-diff-selected={location && annotations.selected(location) || undefined}
        onPointerEnter={() => { if (location) annotations.enter(location); }} style={{ background: bg }}>
      <div className="mc-diff-gutter-cell"><DiffLineGutter line={line} filePath={filePath} workspaceRoot={workspaceRoot} annotations={annotations} side={side === "left" ? "old" : "new"} /></div>
      <div
        className="mc-diff-code-content px-2 whitespace-pre-wrap break-words"
      >
        {html ? (
          <span data-diff-text={`${readingIndex}:${side === "left" ? "old" : "new"}`} dangerouslySetInnerHTML={{ __html: html }} />
        ) : (
          <span data-diff-text={`${readingIndex}:${side === "left" ? "old" : "new"}`} style={{ color: colorForKind(line.kind) }}>{line.text}</span>
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
  background: "var(--diff-added-foreground)",
  border: "1px solid var(--diff-added-foreground)",
  color: "var(--text-on-accent)",
};

const smallActionButtonStyle: React.CSSProperties = {
  ...rejectButtonStyle,
  padding: "3px 7px",
  gap: 4,
  height: 22,
  whiteSpace: "nowrap",
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
  background: "var(--diff-view-background)",
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

const firstChangedLine = (lines: DiffLine[], deleted = false): number =>
  lines.find(line => deleted ? line.kind === "del" : line.kind === "add")?.[deleted ? "oldLine" : "newLine"] ?? 1;

const useExternalEditor = (path: string, workspaceRoot: string | undefined) => {
  const preferences = useContext(DiffOpenPreferencesContext);
  const preferred = preferences && preferredFileTargetForWorkspace(preferences, workspaceRoot ?? useAppStore.getState().workingDirectory);
  const editor = preferences?.fileOpenTargets.find(target => target.id === preferred && target.id !== "minicode" && target.kind === "editor");
  return editor && !runtime()?.remoteHost && workspaceRoot !== ""
    ? (line: number) => void openPreferredFile(path, workspaceRoot, line, undefined, { targetId: editor.id }) : undefined;
};

const DiffHeaderActions = ({ path, workspaceRoot, lines, deleted, open, onToggle, fullReview = false }: {
  path: string; workspaceRoot?: string; lines: DiffLine[]; deleted?: boolean; open: boolean; onToggle: () => void; fullReview?: boolean;
}) => {
  const external = useExternalEditor(path, workspaceRoot);
  const fileReview = useContext(FileWorkspaceReviewContext);
  const line = firstChangedLine(lines, deleted);
  return workspaceRoot === "" ? null : <ChangesFileActions path={path} open={open} onToggle={onToggle}
    onOpen={() => fileReview ? useAppStore.getState().openWorkspaceFileTab(fileReview.owner,
      workspaceFileAbsolutePath({ ...fileReview.owner, browserRoot: fileReview.root }, path), undefined, line) : openDiffSourceFile(path, workspaceRoot, line)}
    onOpenExternal={!fullReview && external ? () => external(line) : undefined} />;
};

const DiffFileSection = ({ path, oldPath, additions, deletions, open, onToggle, label = path, actions, children, workspaceRoot, deleted, firstLine }: {
  path: string; additions?: number; deletions?: number; open: boolean; onToggle: () => void;
  oldPath?: string; workspaceRoot?: string; deleted?: boolean; firstLine?: number;
  label?: string; actions?: React.ReactNode; children: React.ReactNode;
}) => {
  const contentId = useId();
  const currentWorkspace = useAppStore(state => state.workingDirectory);
  const fileReview = useContext(FileWorkspaceReviewContext);
  const preferences = useContext(DiffOpenPreferencesContext);
  const sectionRef = useRef<HTMLElement>(null);
  const navigation = useContext(ChangesNavigationContext);
  const contentRef = useRef<HTMLDivElement>(null);
  const [visited, setVisited] = useState(open);
  useEffect(() => { if (open) setVisited(true); }, [open]);
  useLayoutEffect(() => { contentRef.current!.inert = !open; }, [open]);
  useLayoutEffect(() => {
    if (navigation?.jump?.path === path) sectionRef.current!.scrollIntoView({ block: "start" });
  }, [navigation?.jump, path]);
  const separator = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return <DiffFileContextMenu path={path} workspaceRoot={workspaceRoot ?? currentWorkspace} deleted={deleted} firstChangedLine={firstLine} preferences={preferences}
    canRequestChanges={!fileReview} onOpenFile={fileReview ? line => useAppStore.getState().openWorkspaceFileTab(fileReview.owner,
      workspaceFileAbsolutePath({ ...fileReview.owner, browserRoot: fileReview.root }, path), undefined, line) : undefined}>
    <section ref={sectionRef} className="mc-diff-file-section" data-open={open} data-path={path} aria-label={label}>
    <div className="mc-diff-file-section-heading">
      <button type="button" className="mc-diff-file-section-trigger" aria-label={label} title={path}
        aria-expanded={open} aria-controls={contentId} onClick={() => { navigation?.onActivate(path); onToggle(); }}>
        {fileIcon(path, { size: 18, className: "diff-file-icon" })}
        <span className="mc-diff-section-path">
          {oldPath && <><span className="mc-diff-section-old-path" title={oldPath}>{oldPath}</span><span className="mc-diff-rename-arrow" aria-hidden="true">→</span></>}
          {separator >= 0 && <span className="mc-diff-section-directory"><span>{path.slice(0, separator + 1)}</span></span>}
          <span className="mc-diff-section-filename">{path.slice(separator + 1)}</span>
        </span>
        <span className="mc-diff-file-counts">
          {additions != null && <span className="mc-diff-added">+{additions}</span>}
          {deletions != null && <span className="mc-diff-removed">-{deletions}</span>}
        </span>
        <ChevronDown className="mc-diff-file-disclosure" size={13} aria-hidden="true" />
      </button>
      {actions && <div className="mc-diff-file-section-actions">{actions}</div>}
    </div>
    <div ref={contentRef} id={contentId} className="mc-diff-file-section-body" aria-hidden={!open}>
      <div className="mc-diff-file-section-content">{visited && children}</div>
    </div>
  </section></DiffFileContextMenu>;
};

const ReadOnlyReviewTab = ({ review, viewMode }: { review: DiffReviewState; viewMode: DiffViewMode }) => {
  const { t: tr } = useI18n();
  const workingDirectory = useAppStore((state) => state.workingDirectory);
  const files: DiffReviewState["files"] = review.codeReview ? [...review.files, ...(review.modelComments ?? [])
    .filter((comment, index, comments) => !review.files.some(file => diffFilePathsEqual(file.path, comment.file, review.workspaceRoot))
      && comments.findIndex(other => diffFilePathsEqual(other.file, comment.file, review.workspaceRoot)) === index)
    .map(comment => ({ path: comment.file, patch: "" }))] : review.files.length ? review.files : [{
    path: extractFilePathFromDiff(parseUnifiedDiff(review.diff)) || "Diff", patch: review.diff,
  }];
  const selected = files.find((file) => diffFilePathsEqual(file.path, review.selectedPath, review.workspaceRoot ?? workingDirectory))?.path ?? files[0]?.path;
  const { openPaths, setFileOpen, visiblePath } = useContext(ChangesNavigationContext)!;
  useEffect(() => {
    if (review.selectedPath && selected) setFileOpen(selected, true);
  }, [review.selectedPath]);
  return <div className="mc-diff-file-sections mc-diff-review-sections">
    {review.codeReview && review.error && <div className="code-review-error" role="alert">{review.error}</div>}
    {review.codeReview && !files.length && !review.error && <div className="code-review-empty">{tr("没有可显示的更改。")}</div>}
    {review.toolName === tr("工作区比较") && <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>{tr("工作区比较")}</div>}
    {review.truncated && <div role="note" style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>{tr("不完整历史Diff：保存的差异已截断，无法撤销。")}</div>}
    {files.filter(file => visiblePath === undefined || file.path === visiblePath).map((file) => <ReadOnlyReviewFile key={file.path}
      file={file} review={review} viewMode={viewMode} open={openPaths.has(file.path)}
      onToggle={() => {
        const open = !openPaths.has(file.path);
        setFileOpen(file.path, open);
        if (open) useAppStore.getState().setDiffReviewSelectedPath(file.path);
      }} />)}

  </div>;
};

const ReadOnlyReviewFile = ({ file, review, viewMode, open, onToggle }: {
  file: DiffReviewState["files"][number]; review: DiffReviewState; viewMode: DiffViewMode;
  open: boolean; onToggle: () => void;
}) => {
  const { t: tr } = useI18n();
  useEffect(() => {
    if (open && !file.patch && !review.codeReview && review.workspaceRoot !== "") sendClientCommand({ type: "approval.file_diff", tool_call_id: review.requestId,
      path: file.path, conversation_id: review.conversationId, turn_id: review.turnId });
  }, [open, file.patch, file.path, review.requestId, review.conversationId, review.turnId, review.workspaceRoot]);
  const lines = useMemo(() => parseUnifiedDiff(file.patch ?? ""), [file.patch]);
  const modelComments = (review.modelComments ?? []).filter(comment => diffFilePathsEqual(comment.file, file.path, review.workspaceRoot));
  const attached = new Map<number, DiffLineCommentData>();
  const unplaced = modelComments.filter(comment => {
    const line = lines.find(candidate => candidate.lineIndex !== undefined && candidate.newLine === comment.end);
    if (!line) return true;
    const index = line.lineIndex!;
    const content = `${comment.title}\n\n${comment.body}`;
    const previous = attached.get(index);
    attached.set(index, { filePath: file.path, lineIndex: index, models: [...(previous?.models ?? []), comment],
      content: previous ? `${previous.content}\n\n${content}` : content });
    return false;
  });
  const additions = file.additions ?? lines.filter((line) => line.kind === "add").length;
  const deletions = file.deletions ?? lines.filter((line) => line.kind === "del").length;
  const deleted = patchDeletesFile(file.patch ?? "");
  return <DiffFileSection path={file.path} additions={additions} deletions={deletions} open={open} onToggle={onToggle}
    workspaceRoot={review.workspaceRoot} deleted={deleted} firstLine={firstChangedLine(lines, deleted)}
    actions={<DiffHeaderActions path={file.path} workspaceRoot={review.workspaceRoot} lines={lines} deleted={deleted} open={open} onToggle={onToggle} fullReview={Boolean(review.codeReview)} />}>
    {unplaced.map((comment, index) => <div key={index} className="code-review-comment" data-code-review-comment data-code-review-file={file.path} data-code-review-line={comment.end}>
      <MarkdownRenderer content={`${comment.title}\n\n${comment.body}`} workspaceRoot={review.workspaceRoot} conversationId={review.conversationId} />
    </div>)}
    {file.patch ? <DiffBody lines={lines} annotationSourceKey={`review:${review.requestId}:${file.path}`} comments={[...attached.values()]} viewMode={viewMode} rawPatch={file.patch} filePath={file.path} workspaceRoot={review.workspaceRoot}
      conversationId={review.conversationId} annotationMode="conversation" inline />
      : <div className="mc-diff-file-loading">{review.codeReview ? tr("该文件未出现在本次比较中。") : review.workspaceRoot === "" ? tr("这份记录没有保存该文件的差异。") : tr("正在加载文件差异...")}</div>}
  </DiffFileSection>;
};

// ── Review Tab ───────────────────────────────────────────────────

import type { DiffReviewState } from "../stores/types";

const ReviewTab = ({ diffReview, viewMode }: { diffReview: DiffReviewState | null; viewMode: DiffViewMode }) => {
  const { t: tr } = useI18n();
  if (!diffReview) {
    return (
      <div className="flex-1 grid place-items-center p-4" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>
        {tr("暂无待审阅的改动。需要审批的 Diff 会显示在这里。")}</div>
    );
  }

  if (diffReview.mode === "view" || diffReview.status === "viewing") return <ReadOnlyReviewTab key={diffReview.requestId} review={diffReview} viewMode={viewMode} />;

  return <ActiveReviewTab key={diffReview.requestId} diffReview={diffReview} viewMode={viewMode} />;
};

const ActiveReviewTab = ({ diffReview, viewMode }: { diffReview: DiffReviewState; viewMode: DiffViewMode }) => {
  const { t: tr } = useI18n();
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const conversationId = useAppStore((s) => s.conversationId);


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
  const isSubmitted = diffReview.status === "submitted";
  const decidedFileCount = diffReview.files.filter((file) =>
    diffFileDecisionForPath(diffReview.fileDecisions, file.path, workingDirectory),
  ).length;
  const allFilesDecided = decidedFileCount >= diffReview.files.length;


  return (
    <div className="mc-diff-review-layout h-full grid min-h-0 overflow-hidden" data-has-files={diffReview.files.length > 0 ? "true" : "false"}>
      {diffReview.files.length > 0 && (
        <aside className="mc-diff-review-files min-h-0 overflow-hidden flex flex-col">
          <div className="mc-diff-files-heading">
            <FileDiff size={14} color="var(--text-muted)" />
            <span className="flex-1 font-medium" style={{ color: "var(--text-secondary)" }}>{isReadOnly ? "Diff" : tr("Diff 审阅")}</span>
            <span style={{ color: "var(--text-muted)" }}>{diffReview.files.length}</span>
          </div>
          <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden">
            {diffReview.files.map((file) => {
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
                      {file.additions != null && <span style={{ color: "var(--diff-added-foreground)" }}>+{file.additions}</span>}
                      {file.deletions != null && <span style={{ color: "var(--diff-removed-foreground)" }}>-{file.deletions}</span>}
                      {file.isLarge && <span>{tr("大文件")}</span>}
                    </div>
                  </button>
                  {!isReadOnly && (
                    <>
                      <button
                        title={tr("接受文件")}
                        aria-label={tr("接受文件 {v0}", { v0: file.path })}
                        onClick={(e) => { e.stopPropagation(); useAppStore.getState().setDiffFileDecision(file.path, "approved"); }}
                        style={{ ...fileDecisionBtnStyle, color: decision === "approved" ? "var(--text-on-accent)" : "var(--diff-added-foreground)", background: decision === "approved" ? "var(--diff-added-foreground)" : "transparent" }}
                      >
                        <CheckCircle size={14} />
                      </button>
                      <button
                        title={tr("拒绝文件")}
                        aria-label={tr("拒绝文件 {v0}", { v0: file.path })}
                        onClick={(e) => { e.stopPropagation(); useAppStore.getState().setDiffFileDecision(file.path, "rejected"); }}
                        style={{ ...fileDecisionBtnStyle, color: decision === "rejected" ? "var(--text-on-accent)" : "var(--diff-removed-foreground)", background: decision === "rejected" ? "var(--diff-removed-foreground)" : "transparent" }}
                      >
                        <XCircle size={14} />
                      </button>
                    </>
                  )}
                </div>
              );
            })}

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
              {isSubmitted ? tr("提交中...") : tr("提交审查 ({v0}/{v1})", { v0: decidedFileCount, v1: diffReview.files.length })}
            </button>
          )}
        </aside>
      )}

      <main className="min-w-0 min-h-0 overflow-hidden flex flex-col">
        <div className="mc-diff-review-header">
          <span className="font-medium" style={{ color: "var(--text-secondary)" }}>{isReadOnly ? (diffReview.toolName || tr("工具")) : tr("{v0}审批", { v0: diffReview.toolName || tr("工具") })}</span>
          {plus > 0 && <span style={{ color: "var(--diff-added-foreground)" }}>+{plus}</span>}
          {minus > 0 && <span style={{ color: "var(--diff-removed-foreground)" }}>-{minus}</span>}
          <span className="flex-1" />
          {selectedFile && diffReview.workspaceRoot !== "" && (
            <button
              onClick={() => openDiffSourceFile(selectedFile.path, diffReview.workspaceRoot)}
              title={tr("在编辑器中打开文件")} aria-label={tr("在编辑器中打开文件")} className="mc-diff-icon-action" style={iconButtonStyle}
            >
              <ExternalLink size={14} />
            </button>
          )}
          {diffReview.status === "error" && diffReview.error && (
            <span className="mc-diff-review-error" role="alert" style={{ color: "var(--diff-removed-foreground)" }}>{diffReview.error}</span>
          )}
          {!isReadOnly && isSubmitted && <span style={{ color: "var(--text-muted)" }}>{tr("已提交")}</span>}
          {!isReadOnly && (
            <div className="mc-diff-review-actions">
              <button disabled={isSubmitted} onClick={() => respond(diffReview.requestId, false)} style={{ ...rejectButtonStyle, opacity: isSubmitted ? 0.6 : 1 }}><X size={14} /> {tr("全部拒绝")}</button>
              <button disabled={isSubmitted} onClick={() => respond(diffReview.requestId, true)} style={{ ...acceptButtonStyle, opacity: isSubmitted ? 0.6 : 1 }}><Check size={14} /> {tr("全部接受")}</button>
              {comments.length > 0 && (
                <button disabled={isSubmitted} onClick={() => useAppStore.getState().submitDiffReviewWithComments()} style={{ ...acceptButtonStyle, background: "var(--accent-primary)", borderColor: "var(--accent-primary)", opacity: isSubmitted ? 0.6 : 1 }}>
                  <MessageCircle size={14} /> {tr("提交意见 (")}{comments.length})
                </button>
              )}
            </div>
          )}
        </div>
        {needsFetch ? (
          <div className="flex-1 grid place-items-center" style={{ color: "var(--text-muted)", fontSize: "var(--text-sm)" }}>{tr("正在加载文件差异...")}</div>
        ) : (
          <DiffBody
            lines={parsed}
            annotationSourceKey={`approval:${diffReview.requestId}:${diffReview.selectedPath ?? ""}`}
            viewMode={viewMode}
            rawPatch={diff}
            approvalComments={comments}
            annotationMode="approval"
            commentsEnabled={!isSubmitted}
            conversationId={diffReview.conversationId}
            filePath={diffReview.selectedPath}
            workspaceRoot={diffReview.workspaceRoot}
          />
        )}
      </main>
    </div>
  );
};

// ── History Tab ──────────────────────────────────────────────────

const HistoryTab = ({ turn, turnNumber, viewMode }: { turn?: HistoryDiffTurn; turnNumber: number; viewMode: DiffViewMode }) => {
  const { t: tr } = useI18n();
  const conversationId = useAppStore((state) => state.conversationId);
  const { openPaths, setFileOpen, visiblePath } = useContext(ChangesNavigationContext)!;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  const deferredKey = JSON.stringify(turn?.deferredDiff);
  const hasOpenFiles = openPaths.size > 0;
  useEffect(() => {
    setLoading(false); setError("");
    return () => { request.current?.abort(); request.current = null; };
  }, [turn?.id, conversationId]);
  useEffect(() => {
    if (turn?.deferredDiff && hasOpenFiles) {
      const pending = new AbortController();
      request.current = pending;
      setLoading(true); setError("");
      void loadMessageTurnDiff(turn.deferredDiff, pending.signal).catch(failure => {
        if (pending.signal.aborted) return;
        setError(failure instanceof Error ? failure.message : tr("无法加载历史差异"));
      }).finally(() => {
        if (request.current === pending) { request.current = null; setLoading(false); }
      });
      return () => { pending.abort(); if (request.current === pending) request.current = null; };
    } else setLoading(false);
  }, [deferredKey, hasOpenFiles]);
  const toggleFile = (path: string) => {
    setFileOpen(path, !openPaths.has(path));
  };
  if (!turn) return <div className="flex-1 grid place-items-center p-4">
    <EmptyState compact icon={<GitCompare size={20} />} title={tr("暂无轮次记录")} hint={tr("完成对话后，可以按轮次查看修改。")} />
  </div>;
  if (!turn.files.length) return <div className="flex-1 grid place-items-center p-4">
    <EmptyState compact icon={<GitCompare size={20} />} title={tr("这一轮没有文件修改")} hint={tr("可以选择其他轮次查看。")} />
  </div>;
  return <div className="mc-diff-file-sections">
    {turn.source === "workspace_snapshot" && <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>{tr("工作区比较")}</div>}
    {turn.truncated && <div role="note" style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>{tr("不完整历史Diff：保存的差异已截断，无法撤销。")}</div>}
    {loading && <div style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>{tr("正在加载历史差异…")}</div>}
    {error && <div role="alert" style={{ color: "var(--state-danger)" }}>{error}</div>}
    {turn.files.filter(file => visiblePath === undefined || file.path === visiblePath).map((file) => <HistoryFileDiff key={`${turn.id}:${file.path}`} file={file} turnNumber={turnNumber}
      viewMode={viewMode} open={openPaths.has(file.path)} onToggle={() => void toggleFile(file.path)} />)}
  </div>;
};

const HistoryFileDiff = ({ file, turnNumber, viewMode, open, onToggle }: {
  file: HistoryDiffTurn["files"][number]; turnNumber: number; viewMode: DiffViewMode; open: boolean; onToggle: () => void;
}) => {
  const { t: tr } = useI18n();
  const revisions = useMemo(() => file.revisions.map((revision) => ({ ...revision, lines: parseUnifiedDiff(revision.diff) })), [file]);
  const totals = revisions.reduce((sum, revision) => {
    sum.plus += revision.lines.filter((line) => line.kind === "add").length;
    sum.minus += revision.lines.filter((line) => line.kind === "del").length;
    return sum;
  }, { plus: 0, minus: 0 });
  return <DiffFileSection path={file.path || tr("未标注文件路径")} additions={file.additions ?? totals.plus} deletions={file.deletions ?? totals.minus}
    firstLine={firstChangedLine(revisions.flatMap(revision => revision.lines))}
    open={open} onToggle={onToggle} actions={file.path && <DiffHeaderActions path={file.path} lines={revisions.flatMap(revision => revision.lines)} open={open} onToggle={onToggle} />}>
    {revisions.map((revision, index) => <div key={revision.id}>
      {revisions.length > 1 && <div className="mc-diff-revision-label">{tr("第") + " "}{index + 1} {tr("次编辑")}</div>}
      <DiffBody lines={revision.lines} annotationSourceKey={`history:${revision.id}:${file.path}`} viewMode={viewMode} rawPatch={revision.diff} annotationMode="conversation" inline
        filePath={file.path || undefined} />
    </div>)}
  </DiffFileSection>;
};

const GitChangesTab = ({ viewMode, comparison, selection }: {
  viewMode: DiffViewMode; comparison?: GitComparisonState; selection: GitReviewSelection;
}) => {
  const { t: tr } = useI18n();
  const gitChanges = useAppStore(s => s.gitChanges);
  const workspaceRoot = useAppStore(s => s.workingDirectory);
  const conversationId = useAppStore(s => s.conversationId);
  const navigation = useContext(ChangesNavigationContext)!;
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const pendingActionRef = useRef<string | null>(null);
  const files = comparison?.files ?? [];
  const source = selection.source;
  const snapshot = comparison?.snapshot;
  const runAction = async (type: "diff.git_stage_file" | "diff.git_unstage_file" | "diff.git_stage_all" | "diff.git_unstage_all" | "diff.git_revert_file" | "diff.git_revert_all", path?: string) => {
    if (!snapshot || pendingActionRef.current) return;
    const current = useAppStore.getState();
    if (current.conversationId !== conversationId || !workspaceRootsEqual(current.workingDirectory, workspaceRoot)) return;
    if (snapshot.conversationId !== current.conversationId || !workspaceRootsEqual(snapshot.workspaceRoot, current.workingDirectory)
      || current.pendingConversationSwitchId || current.pendingConversationCreateId) return;
    pendingActionRef.current = type; setPendingAction(type);
    try {
      const result = await sendClientCommandAwaitResult({ type, path,
        source: snapshot.source, snapshot_generation: snapshot.generation,
        environment_id: snapshot.environmentId, cwd_uri: snapshot.cwdUri,
        confirmed: type === "diff.git_revert_file" || type === "diff.git_revert_all" ? true : undefined,
        conversation_id: snapshot.conversationId, workspace: snapshot.workspaceRoot,
      }, type, { silent: true });
      if (!commandResultSucceeded(result)) throw new Error(result.message || tr("Git 操作失败"));
    } catch (error) {
      pushToast(error instanceof Error ? error.message : tr("Git 操作失败"), "error", 5000);
    } finally { pendingActionRef.current = null; setPendingAction(null); }
  };
  const revert = async (path?: string) => {
    const { showConfirm } = await import("../overlays/DialogService");
    const confirmed = await showConfirm({ title: tr("放弃更改"),
      message: path ? tr("确定放弃 {v0} 的本地更改吗？此操作无法撤销。", { v0: path }) : tr("确定放弃此范围内的全部更改吗？此操作无法撤销。"),
      confirmLabel: tr("放弃"), cancelLabel: tr("取消"), danger: true });
    if (confirmed) await runAction(path ? "diff.git_revert_file" : "diff.git_revert_all", path);
  };
  const busy = Boolean(pendingAction || comparison?.loading || !snapshot);
  const canModify = source === "unstaged" || source === "staged";
  return <div className="mc-diff-git-layout">
    <div className="mc-diff-file-sections mc-diff-git-sections">
    {comparison?.error ? <div className="mc-diff-file-loading" role="alert">{comparison.error}</div>
      : comparison?.status === "expected" ? <div className="mc-diff-file-loading">{tr(comparison.reason === "default_branch_unavailable" ? "没有可用的默认基准分支" : "无法找到所选分支的共同祖先")}</div>
      : source === "commit" && !selection.commitSha ? <div className="mc-diff-file-loading">{tr("选择提交以查看更改")}</div>
      : comparison?.loading && !files.length ? <div className="mc-diff-file-loading" role="status">{tr("正在加载 Git 更改...")}</div>
      : gitChanges.isGitRepo === false ? <div className="mc-diff-file-loading">{tr("当前文件夹未启用 Git")}</div>
      : comparison && !files.length ? <div className="mc-diff-file-loading">{tr("没有可显示的更改。")}</div> : null}
    {files.filter(file => navigation.visiblePath === undefined || file.path === navigation.visiblePath).map(file => <GitFileDiff key={`${source}:${file.path}`} file={file} source={source} snapshot={snapshot} viewMode={viewMode}
      open={navigation.openPaths.has(file.path)} onToggle={() => navigation.setFileOpen(file.path, !navigation.openPaths.has(file.path))} actions={canModify && <>
        <button type="button" disabled={busy} onClick={() => void revert(file.path)} title={tr("放弃更改")} aria-label={tr("放弃 {v0} 的更改", { v0: file.path })}><RotateCcw size={14} /></button>
        <button type="button" disabled={busy} onClick={() => void runAction(source === "staged" ? "diff.git_unstage_file" : "diff.git_stage_file", file.path)}
          title={tr(source === "staged" ? "取消暂存" : "暂存")} aria-label={tr(source === "staged" ? "取消暂存 {v0}" : "暂存 {v0}", { v0: file.path })}>
          {source === "staged" ? <Minus size={14} /> : <Plus size={14} />}
        </button>
      </>} />)}

    </div>
    {canModify && files.length > 0 && <div className="mc-diff-git-actions">
      {source === "unstaged" && <button type="button" disabled={busy} onClick={() => void revert()}><RotateCcw size={14} />{tr("全部放弃")}</button>}
      <button type="button" disabled={busy} onClick={() => void runAction(source === "staged" ? "diff.git_unstage_all" : "diff.git_stage_all")}>
        {source === "staged" ? <Minus size={14} /> : <Plus size={14} />}{tr(source === "staged" ? "全部取消暂存" : "全部暂存")}</button>
    </div>}
  </div>;
};

const GitFileDiff = ({ file, source, snapshot, viewMode, open, onToggle, actions }: {
  file: Partial<GitChangeFile> & { path: string };
  source: GitDiffSource; snapshot?: GitDiffSnapshot; viewMode: DiffViewMode; open: boolean; onToggle: () => void;
  actions: React.ReactNode;
}) => {
  const { t: tr } = useI18n();
  const lines = useMemo(() => parseUnifiedDiff(file.patch ?? ""), [file.patch]);
  const fileReview = useContext(FileWorkspaceReviewContext);
  const fileOwner = fileReview?.owner;
  const currentConversationId = useAppStore(state => state.conversationId);
  const currentWorkspaceRoot = useAppStore(state => state.workingDirectory);
  const conversationId = fileOwner ? fileOwner.conversationId : currentConversationId;
  const workspaceRoot = fileReview?.root ?? currentWorkspaceRoot;
  const owner = !fileOwner && snapshot?.conversationId === conversationId && workspaceRootsEqual(snapshot.workspaceRoot, workspaceRoot) ? snapshot : undefined;
  const [loadFullFiles] = useDiffViewPreference("loadFullFiles");
  const [richPreview] = useDiffViewPreference("richPreview");
  const kind = owner ? diffPreviewKind(file.path, file.status === "D", richPreview, source) : "diff";
  const format = kind === "image" || kind === "pdf" ? "binary" : "text";
  const eligible = Boolean(file.patch && file.oldOid && !file.isBinary
    && file.status !== "A" && file.status !== "D" && file.oldMode !== "160000" && file.newMode !== "160000");
  const fullKey = JSON.stringify([owner, file.path, file.revision]);
  const [cachedFull, setCachedFull] = useState<{ key: string; context: FullDiffContext }>();
  const cached = cachedFull?.key === fullKey ? cachedFull.context : undefined;
  const needsFull = eligible && loadFullFiles && !cached;
  const contents = useGitDiffContents({ path: file.path, open, snapshot: owner,
    enabled: Boolean(owner && (needsFull || kind === "image" || kind === "pdf")), format,
    sides: needsFull || kind === "image" || kind === "pdf" ? "both" : "new",
  });
  const full = useMemo(() => {
    if (!eligible || format !== "text") return null;
    const previous = contents.results?.find(result => result.side === "old"), next = contents.results?.find(result => result.side === "new");
    return previous?.type === "success" && next?.type === "success" && previous.lines && next.lines
      ? buildFullDiffContext(file.patch!, previous.lines, next.lines, owner?.hideWhitespace) : null;
  }, [contents.results, file.patch, eligible, format, owner?.hideWhitespace]);
  useEffect(() => { if (full) setCachedFull({ key: fullKey, context: full }); }, [full, fullKey]);
  useEffect(() => {
    if (loadFullFiles) return;
    const timer = window.setTimeout(() => setCachedFull(undefined), 60_000);
    return () => window.clearTimeout(timer);
  }, [loadFullFiles, fullKey]);
  const preview = useGitDiffContents({ path: file.path, open, snapshot: owner, enabled: kind === "markdown", format: "markdown-preview", sides: "new" });
  const nextText = preview.results?.find(result => result.side === "new");
  const mediaContents = kind === "markdown" ? preview : contents;
  const loading = !mediaContents.results && !mediaContents.error;
  const showPreview = kind === "image" || kind === "pdf" || kind === "markdown"
    && (loading || nextText?.type === "success" && Boolean(nextText.lines?.join("")));
  const fullError = contents.error?.message ?? contents.results?.find(result => result.type === "error" && result.error.type === "unknown");
  const errorMessage = typeof fullError === "string" ? fullError : fullError?.type === "error" ? fullError.error.message : undefined;
  const sectionLabel = tr(sourceLabels[source]);
  return <DiffFileSection path={file.path} oldPath={file.oldPath} label={tr("审阅{v0} {v1}", { v0: sectionLabel, v1: file.path })} additions={file.additions}
    workspaceRoot={workspaceRoot} deleted={file.status === "D"} firstLine={firstChangedLine(lines, file.status === "D")}
    deletions={file.deletions} open={open} onToggle={onToggle} actions={<>
      <DiffHeaderActions path={file.path} workspaceRoot={workspaceRoot} lines={lines} deleted={file.status === "D"} open={open} onToggle={onToggle} />
      {actions}
    </>}>
    <div ref={preview.element}><div ref={contents.element}>
      {eligible && loadFullFiles && errorMessage && <div className="mc-diff-content-error" title={errorMessage}>
        <span>{tr("完整文件内容加载失败")}</span><button type="button" onClick={contents.retry}>{tr("重试")}</button>
      </div>}
      {showPreview ? <GitDiffMediaPreview path={file.path} kind={kind} results={mediaContents.results} loading={loading}
        status={file.status === "A" ? "new" : file.status === "D" ? "deleted" : "changed"} workspaceRoot={workspaceRoot} conversationId={conversationId!} />
        : file.patch ? <DiffBody lines={lines} annotationSourceKey={`git:${source}:${file.path}`} viewMode={viewMode} rawPatch={file.patch} filePath={file.path}
          workspaceRoot={workspaceRoot} conversationId={conversationId} annotationMode={fileOwner ? undefined : "conversation"} commentsEnabled={!fileOwner}
          fullContext={loadFullFiles ? full ?? cached : undefined} inline />
        : <div className="mc-diff-file-loading">{tr("没有可显示的更改。")}</div>}
    </div></div>
  </DiffFileSection>;
};
