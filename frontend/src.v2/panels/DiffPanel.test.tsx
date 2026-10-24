/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { sendClientCommand, sendClientCommandAwaitResult, sendPromptResponseCommand } from "../protocol/ws-outbox";
import { showConfirm } from "../overlays/DialogService";
import { DiffPanel } from "./DiffPanel";
import { __resetOpenWebInBrowserForTests, subscribeBrowserRequests } from "../chat/openWebInBrowser";
import * as deferredTurnDiff from "../chat/loadMessageTurnDiff";
import type { GitChangeFile, GitChangesState, GitComparisonState, GitDiffSource, GitReviewSelection, TurnDiffState } from "../stores/types";
import { clearDiffCommentScope, clearDiffReaderDrafts, readDiffCommentState } from "../lib/diff-comment-storage";
import { setWordDiffsEnabled } from "../chat/diff/word-diff-preference";
import { clearGitReviewSelection } from "../chat/diff/git-review-selection";
import { readGitReviewOptions, type GitReviewOptions } from "../chat/diff/git-review-options";
import { clearReviewExpansion } from "../chat/diff/review-expansion-state";
import { readFileWorkspaceApplyPatch, readFileWorkspaceBranches, readFileWorkspaceChanges } from "../protocol/file-workspace";
import * as clipboard from "../lib/clipboard";

const commandIds = vi.hoisted(() => ({ next: 0 }));

vi.hoisted(() => {
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as any;
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(),
  sendClientCommandAwaitResult: vi.fn(),
  sendPromptResponseCommand: vi.fn(),
  createClientCommandId: () => `diff-test-command-${++commandIds.next}`,
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed",
}));

vi.mock("../chat/diff/git-review-options", () => ({ readGitReviewOptions: vi.fn() }));
vi.mock("../protocol/file-workspace", async importOriginal => ({ ...await importOriginal<typeof import("../protocol/file-workspace")>(),
  readFileWorkspaceApplyPatch: vi.fn(), readFileWorkspaceBranches: vi.fn(), readFileWorkspaceChanges: vi.fn() }));

vi.mock("../overlays/DialogService", () => ({
  showConfirm: vi.fn(),
}));

vi.mock("../lib/monaco-colorize", () => ({
  guessLanguageFromPath: () => "python",
  extractFilePathFromDiff: () => "src/app.py",
  useColorizedLines: (lines: Array<{ kind: string; text: string }>) =>
    lines.map((line) => `<span data-testid="syntax-${line.kind}">${line.text}</span>`),
}));

vi.mock("../components/MonacoDiffView", () => ({
  MonacoDiffView: () => null,
}));

const defaultOptions: GitReviewOptions = {
  is_git_repo: true, current_branch: "feature", default_base_branch: "main", branches: ["main", "feature"],
  merge_base_sha: "a".repeat(40), commits: [{ sha: "b".repeat(40), committedAt: "2026-10-17T00:00:00Z", subject: "Actual commit", message: "Actual commit body" }],
  comparison_status: "success", reason: null,
};

function comparisonFixture(files: GitChangeFile[] = [], overrides: Partial<GitComparisonState> = {}): GitComparisonState {
  const source = overrides.source ?? "unstaged";
  return { source, files, loading: false, status: "success", snapshot: {
    conversationId: "conv-diff", workspaceRoot: "C:\\workspace", source,
    generation: `snapshot-${source}-1`, hideWhitespace: false,
  }, ...overrides };
}

function gitFixture(files: GitChangeFile[] = [], comparison: Partial<GitComparisonState> = {}, fields: Partial<GitChangesState> = {}): GitChangesState {
  return { workingTree: [], staged: [], untracked: [], loading: false, isGitRepo: true,
    comparison: comparisonFixture(files, comparison), ...fields };
}

function supplyComparisons(...responses: GitComparisonState[]) {
  vi.mocked(useAppStore.getState().requestGitComparison).mockImplementation((selection: GitReviewSelection) => {
    const response = responses.find(item => item.source === selection.source);
    if (response) useAppStore.setState(state => ({ gitChanges: { ...state.gitChanges,
      comparison: { ...response, ...selection } } }));
  });
}

function chooseDiffSource(label: string) {
  fireEvent.click(screen.getByRole("button", { name: /Diff 来源/ }));
  fireEvent.click(screen.getByRole("menuitemradio", { name: label, exact: true }));
}

function gitFileTrigger(path: string, source: GitDiffSource = "unstaged") {
  const labels: Record<GitDiffSource, string> = { unstaged: "未暂存", staged: "已暂存", uncommitted: "未提交", branch: "此分支", commit: "提交" };
  return screen.getByRole("button", { name: `审阅${labels[source]} ${path}`, exact: true });
}

function openGitFile(path: string, source: GitDiffSource = "unstaged") {
  const trigger = gitFileTrigger(path, source);
  if (trigger.getAttribute("aria-expanded") === "false") fireEvent.click(trigger);
  return trigger;
}

function changeDiffLayout(mode: "unified" | "split") {
  const current = JSON.parse(localStorage.getItem("editorDiffViewMode")!) as "unified" | "split" | "auto";
  if (current === mode) return;
  if (current === "split") fireEvent.click(screen.getByRole("button", { name: "将差异视图切换为自动布局" }));
  if (current !== "unified") fireEvent.click(screen.getByRole("button", { name: "自动差异视图：改用统一差异视图" }));
  if (mode === "split") fireEvent.click(screen.getByRole("button", { name: "切换到拆分差异视图" }));
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("last-explicit-review-diff-filter", JSON.stringify("unstaged"));
  localStorage.setItem("editorDiffViewMode", JSON.stringify("unified"));
  for (const owner of ["conv-diff", "conv-other", "conv-next", "newer-owner"]) {
    clearGitReviewSelection(owner);
    clearReviewExpansion(owner);
  }
  vi.stubGlobal("ResizeObserver", class {
    constructor(private callback: ResizeObserverCallback) {}
    observe(target: Element) { this.callback([{ target, contentRect: { width: 900, height: 600 } } as ResizeObserverEntry], this as unknown as ResizeObserver); }
    unobserve() {} disconnect() {}
  });
  vi.stubGlobal("IntersectionObserver", class {
    constructor(private callback: IntersectionObserverCallback) {}
    observe(target: Element) { this.callback([{ target, isIntersecting: true } as IntersectionObserverEntry], this as unknown as IntersectionObserver); }
    unobserve() {} disconnect() {}
  });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  setWordDiffsEnabled(true);
  clearDiffCommentScope("conv-diff");
  __resetOpenWebInBrowserForTests();
  useAppStore.setState({
    conversationId: "conv-diff",
    pendingConversationSwitchId: null,
    pendingConversationCreateId: null,
    workingDirectory: "C:\\workspace",
    requestGitChanges: vi.fn(), requestGitComparison: vi.fn(),
    gitReviewRequest: null,
    turnDiffs: {},
    rightPanelExpanded: false,
    gitChanges: gitFixture(), diffReview: null, messages: [],
  });
  vi.mocked(readGitReviewOptions).mockReset().mockResolvedValue(defaultOptions);
  vi.mocked(sendClientCommand).mockReturnValue(true);
  vi.mocked(sendClientCommandAwaitResult).mockResolvedValue({
    type: "command.result",
    command: "control_response",
    level: "info",
    message: "",
    data: {},
  });
  vi.mocked(sendPromptResponseCommand).mockResolvedValue(null);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  useAppStore.setState({ diffReview: null, messages: [], gitChanges: gitFixture([]) });
  vi.mocked(sendClientCommand).mockReset();
  vi.mocked(sendClientCommandAwaitResult).mockReset();
  vi.mocked(sendPromptResponseCommand).mockReset();
  vi.mocked(showConfirm).mockReset();
});

describe("Files All changes repository ownership", () => {
  it.each([null, "conv-diff"])("keeps an independent repository away from the active chat state (%s)", async conversationId => {
    const owner = { scopeId: conversationId ? `conversation:${conversationId}` : "home:C:/B", conversationId: null, executionCwd: "C:/B", browserRoot: "C:/B" };
    const patch = "diff --git a/foreign.ts b/foreign.ts\n--- a/foreign.ts\n+++ b/foreign.ts\n@@ -1 +1 @@\n-old\n+foreign content\n";
    vi.mocked(readFileWorkspaceBranches).mockReset().mockResolvedValue(defaultOptions);
    vi.mocked(readFileWorkspaceChanges).mockReset().mockResolvedValue({ source: "branch", baseBranch: "origin/main", isGitRepo: true, loading: false,
      diff: patch, files: [{ path: "foreign.ts", patch, additions: 1, deletions: 1 }], status: "success" });
    vi.mocked(readFileWorkspaceApplyPatch).mockReset().mockResolvedValue(patch);
    const copy = vi.spyOn(clipboard, "copyText").mockResolvedValue(true);
    useAppStore.setState({ conversationId, workingDirectory: "C:/A", isConnected: true, draft: "keep draft", workspaceFileTabs: [],
      gitReviewRequest: { id: "files-owner-B", path: "", section: "working", workspaceRoot: "C:/B", conversationId,
        source: "branch", baseBranch: "origin/main", fileOwner: owner } });
    const before = useAppStore.getState();
    render(<DiffPanel />);
    await screen.findByText("foreign content");
    expect(readFileWorkspaceChanges).toHaveBeenCalledWith(owner, { source: "branch", baseBranch: "origin/main" }, false, expect.any(AbortSignal), "C:/B");
    expect(before.requestGitComparison).not.toHaveBeenCalled();
    expect(useAppStore.getState().gitChanges).toBe(before.gitChanges);
    expect(useAppStore.getState().diffReview).toBe(before.diffReview);
    expect(sendClientCommand).not.toHaveBeenCalled();
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /评论 [LR]/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "“变更”选项" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "复制 git apply 命令" }));
    await waitFor(() => expect(readFileWorkspaceApplyPatch).toHaveBeenCalledWith(owner, { source: "branch", baseBranch: "origin/main" }, "C:/B"));
    expect(copy).toHaveBeenCalledWith(expect.stringContaining(patch), "git apply", "已复制 git apply 命令");
    fireEvent.click(screen.getByRole("button", { name: "打开位置" }));
    expect(useAppStore.getState().workspaceFileTabs).toMatchObject([{ owner, path: "foreign.ts" }]);
    expect(useAppStore.getState()).toMatchObject({ conversationId, workingDirectory: "C:/A", draft: "keep draft" });
  });

  it("passes Files comparison selection into the existing same-root conversation producer", async () => {
    useAppStore.setState({ gitReviewRequest: { id: "same-root-files", path: "", section: "working", workspaceRoot: "C:/workspace", conversationId: "conv-diff",
      source: "branch", baseBranch: "origin/main", fileOwner: { scopeId: "conversation:conv-diff", conversationId: "conv-diff", executionCwd: "C:/workspace", browserRoot: "C:/workspace" } } });
    render(<DiffPanel />);
    await waitFor(() => expect(useAppStore.getState().requestGitComparison).toHaveBeenCalledWith({ source: "branch", baseBranch: "origin/main" }));
  });
});

describe("DiffPanel", () => {
  it("renders an unknown child-root patch without editor navigation against the parent workspace", () => {
    useAppStore.setState({ diffReview: { requestId: "unknown-child", conversationId: "conv-diff", workspaceRoot: "", diff: "@@ -1 +1 @@\n-old\n+new",
      files: [{ path: "src/app.ts", patch: "@@ -1 +1 @@\n-old\n+new" }], selectedPath: "src/app.ts", mode: "view", status: "viewing", fileDecisions: {}, lineComments: [] } });
    render(<DiffPanel />);
    expect(screen.getByText("new")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "在编辑器中打开文件" })).toBeNull();
    expect(screen.queryByRole("button", { name: /在编辑器中打开 src\/app.ts 第/ })).toBeNull();
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });
  it("activates an isolated review's actual workspace before opening its source file", async () => {
    const openEditorFile = vi.fn();
    const originalOpen = useAppStore.getState().openEditorFile;
    useAppStore.setState({ openEditorFile, conversations: [{ id: "conv-diff", title: "Parent", workspaceRoot: "C:/workspace", updatedAt: "now" },
      { id: "child-workspace", title: "Child", workspaceRoot: "C:/isolated-child", updatedAt: "now" }], diffReview: {
      requestId: "child-review", conversationId: "conv-diff", workspaceRoot: "C:/isolated-child", diff: "@@ -1 +1 @@\n-old\n+new",
      selectedPath: "src/app.ts", files: [{ path: "src/app.ts", patch: "@@ -1 +1 @@\n-old\n+new" }], status: "viewing", mode: "view", fileDecisions: {}, lineComments: [],
    } });
    vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(async () => {
      useAppStore.getState().applyConversationSwitched({ conversationId: "child-workspace" });
      return { command: "workspace.set", level: "success", message: "activated", data: { conversation_id: "child-workspace", workspace_root: "C:/isolated-child" } };
    });
    try {
      render(<DiffPanel />);
      fireEvent.click(screen.getByRole("button", { name: "打开位置" }));
      await waitFor(() => expect(openEditorFile).toHaveBeenCalledWith("C:/isolated-child/src/app.ts", "app.ts", { line: 1, exact: true }));
      expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ type: "workspace.set", path: "C:/isolated-child" }), "workspace.set");
    } finally { useAppStore.setState({ openEditorFile: originalOpen }); }
  });

  it("does not steal editor focus when workspace activation returns after a newer scope selection", async () => {
    const originalOpen = useAppStore.getState().openEditorFile;
    const openEditorFile = vi.fn();
    useAppStore.setState({ openEditorFile, draft: "保留未发送内容", diffReview: {
      requestId: "late-child-review", conversationId: "conv-diff", workspaceRoot: "C:/isolated-child", diff: "@@ -1 +1 @@\n-old\n+new",
      selectedPath: "src/app.ts", files: [{ path: "src/app.ts", patch: "@@ -1 +1 @@\n-old\n+new" }], status: "viewing", mode: "view", fileDecisions: {}, lineComments: [],
    } });
    let finish!: (event: Awaited<ReturnType<typeof sendClientCommandAwaitResult>>) => void;
    vi.mocked(sendClientCommandAwaitResult).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    try {
      render(<DiffPanel />);
      fireEvent.click(screen.getByRole("button", { name: "打开位置" }));
      useAppStore.setState({ conversationId: "newer-owner", workingDirectory: "C:/newer-workspace" });
      await act(async () => finish({ command: "workspace.set", level: "success", message: "activated", data: { conversation_id: "child-workspace", workspace_root: "C:/isolated-child" } }));
      expect(openEditorFile).not.toHaveBeenCalled();
      expect(useAppStore.getState().draft).toBe("保留未发送内容");
    } finally { useAppStore.setState({ openEditorFile: originalOpen }); }
  });
  it("loads the real deferred history once when its scope opens with files expanded by default", async () => {
    const persisted: TurnDiffState = { threadId: "conv-diff", turnId: "turn", messageId: "answer", diff: null,
      deferred: true, revision: 2, updatedAt: 1, source: "workspace_snapshot",
      files: [{ path: "src/a.ts", additions: 1, deletions: 1 }, { path: "src/b.ts", additions: 1, deletions: 1 }] };
    const answer = { id: "answer", turnId: "turn", role: "assistant" as const, content: "Done", artifacts: [], timestamp: 1, turnDiff: persisted };
    const load = vi.spyOn(deferredTurnDiff, "loadMessageTurnDiff").mockImplementation(async () => {
      const loaded = { ...persisted, deferred: false, diff: "diff --git a/src/a.ts b/src/a.ts\n@@ -1 +1 @@\n-oldA\n+newA\n"
        + "diff --git a/src/b.ts b/src/b.ts\n@@ -1 +1 @@\n-oldB\n+newB\n" };
      useAppStore.setState({ messages: [{ ...answer, turnDiff: loaded }] });
      return loaded;
    });
    useAppStore.setState({ diffReview: null, messages: [answer] });
    render(<DiffPanel />);
    expect(load).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /Diff 来源/ }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "上一轮", exact: true }));
    expect(screen.getByText("工作区比较")).toBeTruthy();
    await waitFor(() => expect(screen.getByText("newB")).toBeTruthy());
    expect(screen.getByText("newA")).toBeTruthy();
    expect(screen.getByRole("button", { name: "src/b.ts", exact: true }).getAttribute("aria-expanded")).toBe("true");
    expect(load).toHaveBeenCalledTimes(1);
    expect(load.mock.calls[0][0]).toEqual({ conversationId: "conv-diff", messageId: "answer", turnId: "turn", revision: 2 });
    expect(screen.getByRole("button", { name: "src/a.ts", exact: true })).toBeTruthy();
  });

  it("keeps an actual history load error visible without automatically repeating the same revision", async () => {
    const load = vi.spyOn(deferredTurnDiff, "loadMessageTurnDiff").mockRejectedValue(new Error("Retained patch is unavailable"));
    useAppStore.setState({ messages: [{ id: "deferred-answer", role: "assistant", content: "Done", turnId: "deferred-turn", timestamp: 1,
      turnDiff: { threadId: "conv-diff", turnId: "deferred-turn", messageId: "deferred-answer", deferred: true, revision: 7,
        diff: null, updatedAt: 1, files: [{ path: "src/history.ts", additions: 1, deletions: 1 }] } }] });
    const view = render(<DiffPanel />);
    chooseDiffSource("上一轮");
    await screen.findByText("Retained patch is unavailable");
    expect(load).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "自动换行" }));
    view.rerender(<DiffPanel />);
    await act(async () => {});
    expect(load).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alert").textContent).toBe("Retained patch is unavailable");
  });

  it("aborts the old deferred revision before reading its replacement and ignores the old completion", async () => {
    const deferred = (revision: number): TurnDiffState => ({ threadId: "conv-diff", turnId: "deferred-turn", messageId: "deferred-answer",
      deferred: true, revision, diff: null, updatedAt: revision, files: [{ path: "src/history.ts", additions: 1, deletions: 1 }] });
    const answer = (turnDiff: TurnDiffState) => ({ id: "deferred-answer", role: "assistant" as const, content: "Done",
      turnId: "deferred-turn", timestamp: 1, turnDiff });
    const signals: AbortSignal[] = [], completions: Array<(value: TurnDiffState | undefined) => void> = [];
    const load = vi.spyOn(deferredTurnDiff, "loadMessageTurnDiff").mockImplementation((_owner, signal) => new Promise(resolve => {
      signals.push(signal!); completions.push(resolve);
    }));
    useAppStore.setState({ draft: "Keep my draft", messages: [answer(deferred(1))] });
    render(<DiffPanel />); chooseDiffSource("上一轮");
    expect(load).toHaveBeenCalledTimes(1);
    act(() => useAppStore.setState({ messages: [answer(deferred(2))] }));
    expect(signals[0].aborted).toBe(true);
    expect(load.mock.calls.map(([owner]) => owner.revision)).toEqual([1, 2]);
    await act(async () => completions[0](undefined));
    expect(screen.getByText("正在加载历史差异…")).toBeTruthy();
    const loaded = { ...deferred(2), deferred: false, diff: "diff --git a/src/history.ts b/src/history.ts\n@@ -1 +1 @@\n-old\n+new_revision" };
    await act(async () => { useAppStore.setState({ messages: [answer(loaded)] }); completions[1](loaded); });
    expect(screen.getByText("new_revision", { exact: true })).toBeTruthy();
    expect(screen.queryByText("正在加载历史差异…")).toBeNull();
    expect(load).toHaveBeenCalledTimes(2);
    expect(useAppStore.getState().draft).toBe("Keep my draft");
  });

  it("shows incomplete workspace history explicitly in read-only review", () => {
    const patch = "diff --git a/src/a.ts b/src/a.ts\n@@ -1 +1 @@\n-old\n+new\n";
    useAppStore.setState({ diffReview: { requestId: "history", conversationId: "conv-diff", mode: "view", status: "viewing",
      toolName: "工作区比较", truncated: true, diff: patch, files: [{ path: "src/a.ts", patch }], fileDecisions: {}, lineComments: [] } });
    render(<DiffPanel />);
    expect(screen.getByText("工作区比较")).toBeTruthy();
    expect(screen.getByRole("note").textContent).toContain("不完整历史Diff");
    expect(screen.queryByRole("button", { name: /撤销|全部接受|全部拒绝/ })).toBeNull();
  });
  it.each(["review", "history", "git"] as const)("keeps %s diff rows free of chat and question affordances in both layouts", (scope) => {
    const patch = "diff --git a/src/app.ts b/src/app.ts\n--- a/src/app.ts\n+++ b/src/app.ts\n@@ -10 +10 @@\n-old\n+new";
    useAppStore.setState({ draft: "Keep the draft", selectedMentions: [], diffReview: scope === "review" ? {
      requestId: "no-row-chat", conversationId: "conv-diff", mode: "view", status: "viewing", diff: patch,
      files: [{ path: "src/app.ts", patch }], selectedPath: "src/app.ts", fileDecisions: {}, lineComments: [],
    } : null,
      messages: scope === "history" ? [{ id: "answer", role: "assistant", turnId: "turn", timestamp: 1, content: "Done", artifacts: [],
        turnDiff: { threadId: "conv-diff", turnId: "turn", messageId: "answer", updatedAt: 1, diff: patch } }] : [],
      gitChanges: gitFixture(scope === "git" ? [{ path: "src/app.ts", patch, additions: 1, deletions: 1 }] : []),
    });
    const { container } = render(<DiffPanel />);
    if (scope === "history") {
      fireEvent.click(screen.getByRole("button", { name: /Diff 来源/ }));
      fireEvent.click(screen.getByRole("menuitemradio", { name: "上一轮", exact: true }));
    } else if (scope === "git") openGitFile("src/app.ts");
    for (const mode of ["unified", "split"]) {
      expect(screen.queryByRole("button", { name: /加入对话|引用|提问|评论 Diff/ })).toBeNull();
      expect(container.querySelector(".mc-diff-line-quote")).toBeNull();
      expect(screen.getByText("new", { exact: true })).toBeTruthy();
      fireEvent.click(screen.getByText("new", { exact: true }));
      expect(useAppStore.getState().draft).toBe("Keep the draft");
      expect(useAppStore.getState().selectedMentions).toEqual([]);
      if (mode === "unified") changeDiffLayout("split");
    }
  });

  it("keeps multiple current-turn files expanded without line chat controls", () => {
    const patch = (path: string, content: string) => `diff --git a/${path} b/${path}\n@@ -419 +419 @@\n-old\n+${content}`;
    useAppStore.setState({ draft: "已有问题", diffReview: {
      requestId: "multi-file-reading", conversationId: "conv-diff", mode: "view", status: "viewing",
      diff: patch("src/a.ts", "newA"), selectedPath: "src/a.ts", fileDecisions: {},
      files: [{ path: "src/a.ts", patch: patch("src/a.ts", "newA"), additions: 1, deletions: 1 },
        { path: "src/b.ts", patch: patch("src/b.ts", "newB"), additions: 1, deletions: 1 }],
    } });
    render(<DiffPanel />);
    const first = screen.getByRole("button", { name: "src/a.ts", exact: true });
    const second = screen.getByRole("button", { name: "src/b.ts", exact: true });
    expect(first.getAttribute("aria-expanded")).toBe("true");
    expect(second.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("newA")).toBeTruthy();
    expect(screen.getByText("newB")).toBeTruthy();
    expect(screen.getAllByText("418 行未修改")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /加入对话/ })).toBeNull();
    expect(useAppStore.getState().draft).toBe("已有问题");
    fireEvent.click(first);
    expect(first.getAttribute("aria-expanded")).toBe("false");
    expect(second.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("region", { name: "src/a.ts" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "src/b.ts" })).toBeTruthy();
  });

  it("shows the authoritative multi-file turn patch and opens each file without a file picker", () => {
    const patch = (path: string, text: string) => `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -1 +1 @@\n-before\n+${text}`;
    useAppStore.setState({ diffReview: null, messages: [{ id: "answer", turnId: "turn", role: "assistant", content: "完成", artifacts: [], timestamp: 1,
      blocks: [{ type: "tool_call", record: { id: "intermediate", name: "edit_file", status: "success", startedAt: 1,
        args: { path: "src/obsolete.ts", patch: patch("src/obsolete.ts", "intermediate") } } }],
    }], turnDiffs: { "conv-diff": { threadId: "conv-diff", turnId: "turn", messageId: "answer", updatedAt: 2,
      diff: `${patch("src/final-a.ts", "finalA")}\n${patch("src/final-b.ts", "finalB")}`,
    } } });
    const { container } = render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: /Diff 来源/ }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "上一轮", exact: true }));
    expect(screen.queryByRole("combobox", { name: "轮次修改文件" })).toBeNull();
    expect(screen.queryByText("intermediate")).toBeNull();
    expect(screen.getByText("finalA")).toBeTruthy();
    expect(screen.getByText("finalB")).toBeTruthy();
    expect(container.querySelector(".mc-diff-total-counts")?.textContent).toBe("+2-2");
    expect(screen.getByRole("button", { name: "src/final-a.ts", exact: true }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: "src/final-b.ts", exact: true }).getAttribute("aria-expanded")).toBe("true");
  });

  it("keeps two Git file patches open and collapses only the clicked range", () => {
    const patch = (path: string, text: string) => `diff --git a/${path} b/${path}\n@@ -1 +1 @@\n-old\n+${text}`;
    useAppStore.setState({ gitChanges: gitFixture([
      { path: "src/a.ts", patch: patch("src/a.ts", "gitA"), additions: 1, deletions: 1 },
      { path: "src/b.ts", patch: patch("src/b.ts", "gitB"), additions: 1, deletions: 1 },
    ]) });
    render(<DiffPanel />);
    const first = gitFileTrigger("src/a.ts");
    const second = gitFileTrigger("src/b.ts");
    expect(first.getAttribute("aria-expanded")).toBe("true");
    expect(second.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("gitA")).toBeTruthy();
    expect(screen.getByText("gitB")).toBeTruthy();
    fireEvent.click(first);
    expect(first.getAttribute("aria-expanded")).toBe("false");
    expect(second.getAttribute("aria-expanded")).toBe("true");
  });

  it("keeps feedback draft, true hunk offsets and saved comments when switching reading modes", () => {
    const patch = "diff --git a/src/app.ts b/src/app.ts\n@@ -100 +120 @@\n-old\n+new\n@@ -500 +520 @@\n-later old\n+later new";
    useAppStore.setState({ diffReview: {
      requestId: "hunk-feedback", conversationId: "conv-diff", diff: patch,
      files: [{ path: "src/app.ts", patch }], selectedPath: "src/app.ts", status: "pending", mode: "approval", fileDecisions: {}, lineComments: [],
    } });
    render(<DiffPanel />);
    expect(screen.getByText(/399\s*行未修改/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "评论 R520" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R520" }), { target: { value: "保留这里的说明" } });
    changeDiffLayout("split");
    expect((screen.getByRole("textbox", { name: "评论 R520" }) as HTMLTextAreaElement).value).toBe("保留这里的说明");
    expect(screen.getByRole("button", { name: "在编辑器中打开 src/app.ts 第 520 行" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "评论", exact: true }));
    expect(useAppStore.getState().diffReview?.lineComments).toEqual([expect.objectContaining({ filePath: "src/app.ts",
      position: { path: "src/app.ts", side: "right", line: 520 }, content: "保留这里的说明" })]);
    changeDiffLayout("unified");
    expect((screen.getByRole("textbox", { name: "评论 R520" }) as HTMLTextAreaElement).value).toBe("保留这里的说明");
    expect(screen.getByText(/399\s*行未修改/)).toBeTruthy();
  });

  it("preserves an exact text selection while changing inline and split views", () => {
    const patch = "diff --git a/src/app.ts b/src/app.ts\n@@ -100 +120 @@\n-old value\n+new value";
    useAppStore.setState({ diffReview: { requestId: "selection-mode", conversationId: "conv-diff", diff: patch, files: [{ path: "src/app.ts", patch }], selectedPath: "src/app.ts", status: "viewing", mode: "view", fileDecisions: {} } });
    const { container } = render(<DiffPanel />);
    const element = container.querySelector('[data-diff-text="1:new"]')!;
    const point = (offset: number) => {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      let node = walker.nextNode()!;
      while (offset > node.textContent!.length) { offset -= node.textContent!.length; node = walker.nextNode()!; }
      return { node, offset };
    };
    const start = point(1), end = point(6);
    const range = document.createRange();
    range.setStart(start.node, start.offset); range.setEnd(end.node, end.offset);
    window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
    fireEvent(document, new Event("selectionchange"));
    changeDiffLayout("split");
    expect(window.getSelection()!.toString()).toBe("ew va");
    changeDiffLayout("unified");
    expect(window.getSelection()!.toString()).toBe("ew va");
    window.getSelection()!.removeAllRanges();
  });

  it("opens the requested staged comparison and never concatenates the working comparison for the same path", () => {
    const before = "diff --git a/src/app.ts b/src/app.ts\n@@ -10 +10 @@\n-base\n+staged value";
    const after = "diff --git a/src/app.ts b/src/app.ts\n@@ -10 +10 @@\n-staged value\n+working value";
    const working = comparisonFixture([{ path: "src/app.ts", patch: after, additions: 1, deletions: 1 }]);
    const staged = comparisonFixture([{ path: "src/app.ts", patch: before, additions: 1, deletions: 1 }], { source: "staged" });
    supplyComparisons(working, staged);
    useAppStore.setState({ gitChanges: gitFixture(working.files) });
    render(<DiffPanel />);
    act(() => useAppStore.getState().openGitReview({ path: "src/app.ts", section: "staged", workspaceRoot: "C:\\workspace", conversationId: "conv-diff" }));
    expect(screen.getByText("base")).toBeTruthy();
    expect([...document.querySelectorAll("[data-diff-text]")].some(node => node.textContent === "working value")).toBe(false);
    chooseDiffSource("未暂存");
    openGitFile("src/app.ts");
    expect(screen.queryByText("base", { exact: true })).toBeNull();
    expect([...document.querySelectorAll("[data-diff-text]")].some(node => node.textContent === "working value")).toBe(true);
    expect(useAppStore.getState().requestGitComparison).toHaveBeenLastCalledWith(expect.objectContaining({ source: "unstaged" }));
    expect(within(screen.getByRole("region", { name: "审阅未暂存 src/app.ts" })).queryByRole("button", { name: /加入对话/ })).toBeNull();
  });

  it("retains explicit file expansion after changing comparison source without replaying an earlier file-open request", () => {
    const files = ["src/a.ts", "src/b.ts"].map(path => ({ path, patch: "@@ -1 +1 @@\n-old\n+new", additions: 1, deletions: 1 }));
    supplyComparisons(comparisonFixture(files), comparisonFixture(files, { source: "staged" }));
    useAppStore.setState({ gitChanges: gitFixture(files) });
    render(<DiffPanel />);
    act(() => useAppStore.getState().openGitReview({ path: "src/a.ts", section: "unstaged", workspaceRoot: "C:\\workspace", conversationId: "conv-diff" }));
    expect(gitFileTrigger("src/a.ts").getAttribute("aria-expanded")).toBe("true");
    expect(gitFileTrigger("src/b.ts").getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(gitFileTrigger("src/a.ts"));
    chooseDiffSource("已暂存");
    expect(gitFileTrigger("src/a.ts", "staged").getAttribute("aria-expanded")).toBe("false");
    expect(gitFileTrigger("src/b.ts", "staged").getAttribute("aria-expanded")).toBe("false");
    chooseDiffSource("未暂存");
    expect(gitFileTrigger("src/a.ts").getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "跳转到文件", exact: true }));
    fireEvent.click(screen.getByRole("option", { name: "src/b.ts", exact: true }));
    expect(gitFileTrigger("src/b.ts").getAttribute("aria-expanded")).toBe("true");
    expect(gitFileTrigger("src/a.ts").getAttribute("aria-expanded")).toBe("false");
  });
  it.each(["unified", "split"])("uses real source lines across hunks and opens current code in %s view", (viewMode) => {
    const patch = [
      "diff --git a/src/app.ts b/src/app.ts", "--- a/src/app.ts", "+++ b/src/app.ts",
      "@@ -40,2 +60,2 @@", " context", "-old", "+new",
      "@@ -80 +100 @@", "-laterOld", "+laterNew",
    ].join("\n");
    const originalOpenEditorFile = useAppStore.getState().openEditorFile;
    const openEditorFile = vi.fn();
    useAppStore.setState({
      openEditorFile,
      diffReview: {
        requestId: "line-review", conversationId: "conv-diff", toolName: "本轮修改", diff: patch,
        files: [{ path: "src/app.ts", patch }], selectedPath: "src/app.ts",
        status: "viewing", mode: "view", fileDecisions: {}, lineComments: [],
      },
    });
    try {
      render(<DiffPanel />);
      if (viewMode === "split") changeDiffLayout("split");
      expect(screen.getByRole("button", { name: "在编辑器中打开 src/app.ts 第 60 行" })).toBeTruthy();
      expect(screen.getByRole("button", { name: "在编辑器中打开 src/app.ts 第 61 行" })).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "在编辑器中打开 src/app.ts 第 100 行" }));
      expect(openEditorFile).toHaveBeenCalledWith("src/app.ts", "app.ts", { line: 100, exact: true });
      expect(screen.queryByRole("textbox", { name: /评论 Diff/ })).toBeNull();
    } finally {
      useAppStore.setState({ openEditorFile: originalOpenEditorFile });
    }
  });

  it("returns only to a preview belonging to this review and conversation", () => {
    const onBrowserRequest = vi.fn();
    const unsubscribe = subscribeBrowserRequests(onBrowserRequest);
    useAppStore.setState({
      rightStackTab: "diff",
      diffReview: {
        requestId: "from-preview", conversationId: "conv-diff", toolName: "本轮修改", diff: "@@ -1 +1 @@\n-old\n+new",
        files: [], status: "viewing", mode: "view", fileDecisions: {},
        previewReturnTarget: { conversationId: "conv-diff", tab: "browser", targetId: "page", url: "http://localhost:4173/" },
      },
    });
    render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: "返回预览" }));
    expect(useAppStore.getState().rightStackTab).toBe("browser");
    expect(onBrowserRequest).toHaveBeenCalledWith(expect.objectContaining({ kind: "resume", conversationId: "conv-diff", targetId: "page", url: "http://localhost:4173/" }));
    act(() => useAppStore.setState({ conversationId: "other" }));
    expect(screen.queryByRole("button", { name: "返回预览" })).toBeNull();
    unsubscribe();
  });

  it("keeps approval feedback drafts bound to their own selected file", () => {
    const first = "diff --git a/src/a.ts b/src/a.ts\n@@ -1 +1 @@\n-oldA\n+newA";
    const second = "diff --git a/src/b.ts b/src/b.ts\n@@ -1 +1 @@\n-oldB\n+newB";
    useAppStore.setState({ diffReview: {
      requestId: "feedback-files", conversationId: "conv-diff", toolName: "edit", diff: first,
      files: [{ path: "src/a.ts", patch: first }, { path: "src/b.ts", patch: second }],
      selectedPath: "src/a.ts", status: "pending", mode: "approval", fileDecisions: {}, lineComments: [],
    } });
    render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: "评论 R1" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R1" }), { target: { value: "For file A" } });
    fireEvent.click(screen.getByRole("button", { name: "src/b.ts", exact: true }));
    expect(screen.queryByRole("textbox", { name: /评论 [LR]/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "评论 R1" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R1" }), { target: { value: "For file B" } });
    fireEvent.click(screen.getByRole("button", { name: "src/a.ts", exact: true }));
    expect((screen.getByRole("textbox", { name: "评论 R1" }) as HTMLTextAreaElement).value).toBe("For file A");
    fireEvent.click(screen.getByRole("button", { name: "src/b.ts", exact: true }));
    expect((screen.getByRole("textbox", { name: "评论 R1" }) as HTMLTextAreaElement).value).toBe("For file B");
    expect(useAppStore.getState().diffReview?.lineComments).toEqual([]);
  });

  it("shows a plain-folder state and permits detecting a newly initialized repository", () => {
    useAppStore.setState({
      diffReview: null, messages: [],
      gitChanges: gitFixture([], {  }, { isGitRepo: false }),
    });
    render(<DiffPanel />);

    expect(screen.getByText("当前文件夹未启用 Git")).toBeTruthy();
    expect(screen.queryByText("没有未提交的更改")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "刷新", exact: true }));
    expect(useAppStore.getState().requestGitComparison).toHaveBeenCalledTimes(2);
  });

  it.each(["pendingConversationSwitchId", "pendingConversationCreateId"] as const)(
    "resumes the mounted Git tab when %s settles, including a same-root owner change", pending => {
      useAppStore.setState({ diffReview: null, messages: [], [pending]: "conv-next",
        gitChanges: gitFixture([]) });
      render(<DiffPanel />);
      const request = useAppStore.getState().requestGitComparison;
      expect(request).not.toHaveBeenCalled();
      act(() => useAppStore.setState({ [pending]: null, conversationId: "conv-next" }));
      expect(request).toHaveBeenCalledTimes(1);
      act(() => useAppStore.setState({ workingDirectory: "C:/another" }));
      expect(request).toHaveBeenCalledTimes(2);
    },
  );

  it("refreshes the mounted Git tab after navigation is cancelled with the current owner unchanged", () => {
    useAppStore.setState({ diffReview: null, messages: [],
      gitChanges: gitFixture([]) });
    render(<DiffPanel />);
    const request = useAppStore.getState().requestGitComparison;
    expect(request).toHaveBeenCalledTimes(1);
    act(() => useAppStore.setState({ pendingConversationSwitchId: "conv-next" }));
    expect(request).toHaveBeenCalledTimes(1);
    act(() => useAppStore.setState({ pendingConversationSwitchId: null }));
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("keeps repository failures visible without reporting a clean worktree", () => {
    useAppStore.setState({
      diffReview: null, messages: [],
      gitChanges: gitFixture([], { error: "Repository permission denied" }),
    });
    render(<DiffPanel />);
    expect(screen.getByRole("alert").textContent).toBe("Repository permission denied");
    expect(screen.queryByText("没有可显示的更改。")).toBeNull();
  });

  it("renders historical tool diffs as read-only", () => {
    useAppStore.setState({
      diffReview: {
        requestId: "edit-view",
        toolName: "edit_file",
        diff: "diff --git a/src/app.ts b/src/app.ts\n@@ -1 +1 @@\n-old\n+new",
        files: [{
          path: "src/app.ts",
          patch: "diff --git a/src/app.ts b/src/app.ts\n@@ -1 +1 @@\n-old\n+new",
          additions: 1,
          deletions: 1,
        }],
        selectedPath: "src/app.ts",
        status: "viewing",
        mode: "view",
        fileDecisions: {},
        lineComments: [],
      },
    });

    render(React.createElement(DiffPanel));

    expect(screen.getByRole("button", { name: /Diff 来源：当前修改 1/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "src/app.ts" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.queryByText("edit_file")).toBeNull();
    expect(screen.queryByText("edit_file审批")).toBeNull();
    expect(screen.queryByRole("button", { name: "全部接受" })).toBeNull();
    expect(screen.queryByRole("button", { name: "全部拒绝" })).toBeNull();
  });

  it("uses code syntax highlighting for context, added and deleted diff rows", () => {
    useAppStore.setState({
      diffReview: {
        requestId: "theme-diff",
        toolName: "edit_file",
        diff: [
          "diff --git a/src/app.py b/src/app.py",
          "@@ -1,2 +1,2 @@",
          " context_value = 1",
          "-from old_module import OldThing",
          "+from new_module import NewThing",
        ].join("\n"),
        files: [{
          path: "src/app.py",
          patch: [
            "diff --git a/src/app.py b/src/app.py",
            "@@ -1,2 +1,2 @@",
            " context_value = 1",
            "-from old_module import OldThing",
            "+from new_module import NewThing",
          ].join("\n"),
          additions: 1,
          deletions: 1,
        }],
        selectedPath: "src/app.py",
        status: "viewing",
        mode: "view",
        fileDecisions: {},
        lineComments: [],
      },
    });

    render(React.createElement(DiffPanel));

    expect(screen.getByTestId("syntax-context")).toBeTruthy();
    expect(screen.getByTestId("syntax-add").textContent).toBe("from new_module import NewThing");
    expect(screen.getByTestId("syntax-del").textContent).toBe("from old_module import OldThing");
  });

  it("renders the complete read-only review diff without a second expand control", () => {
    const largePatch = [
      "diff --git a/src/app.py b/src/app.py",
      "@@ -1,1300 +1,1300 @@",
      ...Array.from({ length: 1300 }, (_, index) => ` line_${index}`),
    ].join("\n");
    useAppStore.setState({
      diffReview: {
        requestId: "large-diff",
        toolName: "edit_file",
        diff: largePatch,
        files: [{
          path: "src/app.py",
          patch: largePatch,
          additions: 0,
          deletions: 0,
        }],
        selectedPath: "src/app.py",
        status: "viewing",
        mode: "view",
        fileDecisions: {},
        lineComments: [],
      },
    });

    render(React.createElement(DiffPanel));

    expect(screen.getByText("line_1299")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "显示完整 Diff" })).toBeNull();
  }, 30_000);

  it("sends git action commands from the changes panel", async () => {
    const patch = "diff --git a/src/app.ts b/src/app.ts\n@@ -1 +1 @@\n-old\n+new";
    const working = comparisonFixture([{ path: "src/app.ts", patch, additions: 1, deletions: 1 }]);
    const staged = comparisonFixture([{ path: "src/old.ts", patch, additions: 1, deletions: 1 }], { source: "staged" });
    supplyComparisons(working, staged);
    useAppStore.setState({ gitChanges: gitFixture(working.files) });
    render(<DiffPanel />);
    await waitFor(() => expect(readGitReviewOptions).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: /Diff 来源：未暂存 1/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "全部取消暂存" })).toBeNull();
    expect(screen.getByRole("button", { name: "全部放弃", exact: true })).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "全部暂存" })));
    expect(sendClientCommandAwaitResult).toHaveBeenLastCalledWith(expect.objectContaining({ type: "diff.git_stage_all",
      source: "unstaged", snapshot_generation: "snapshot-unstaged-1", conversation_id: "conv-diff", workspace: "C:\\workspace" }), "diff.git_stage_all", { silent: true });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "暂存 src/app.ts" })));
    expect(sendClientCommandAwaitResult).toHaveBeenLastCalledWith(expect.objectContaining({ type: "diff.git_stage_file", path: "src/app.ts",
      source: "unstaged", snapshot_generation: "snapshot-unstaged-1" }), "diff.git_stage_file", { silent: true });
    chooseDiffSource("已暂存");
    expect(screen.queryByRole("button", { name: "全部暂存" })).toBeNull();
    expect(screen.queryByRole("button", { name: "全部放弃", exact: true })).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "全部取消暂存" })));
    expect(sendClientCommandAwaitResult).toHaveBeenLastCalledWith(expect.objectContaining({ type: "diff.git_unstage_all",
      source: "staged", snapshot_generation: "snapshot-staged-1" }), "diff.git_unstage_all", { silent: true });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "取消暂存 src/old.ts" })));
    expect(sendClientCommandAwaitResult).toHaveBeenLastCalledWith(expect.objectContaining({ type: "diff.git_unstage_file", path: "src/old.ts",
      source: "staged", snapshot_generation: "snapshot-staged-1" }), "diff.git_unstage_file", { silent: true });
  });

  it("shows all fifty-three ordinary Git files and complete patch content without pagination", () => {
    const largePatch = [
      "diff --git a/src/file-000.ts b/src/file-000.ts",
      "@@ -1,1000 +1,1000 @@",
      ...Array.from({ length: 1000 }, (_, index) => ` line_${index}`),
    ].join("\n");
    useAppStore.setState({
      gitChanges: gitFixture(Array.from({ length: 53 }, (_, index) => ({
          path: `src/file-${String(index).padStart(3, "0")}.ts`,
          patch: index === 0 ? largePatch : `diff --git a/src/file-${index}.ts b/src/file-${index}.ts\n@@ -1 +1 @@\n-old\n+new`,
          additions: index === 0 ? 0 : 1,
          deletions: index === 0 ? 0 : 1,
        }))),
    });

    render(React.createElement(DiffPanel));

    expect(gitFileTrigger("src/file-000.ts")).toBeTruthy();
    expect(gitFileTrigger("src/file-052.ts")).toBeTruthy();
    expect(screen.getByText("line_999")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /再显示 .* 个文件|显示完整 Diff/ })).toBeNull();
    expect(screen.queryByText(/另有 .* 行 Diff 已隐藏/)).toBeNull();
    expect(screen.queryByText("此差异较大，每次仅显示一个文件")).toBeNull();
  }, 30_000);

  it("switches the actual displayed file through Next, Previous and Jump in a 129-file Git comparison", () => {
    const path = (index: number) => `src/file-${String(index).padStart(3, "0")}.ts`;
    useAppStore.setState({ gitChanges: gitFixture(Array.from({ length: 129 }, (_, index) => ({
      path: path(index), patch: `diff --git a/${path(index)} b/${path(index)}\n@@ -1 +1 @@\n-old_${index}\n+new_${index}`,
      additions: 1, deletions: 1,
    }))) });
    render(<DiffPanel />);
    expect(screen.getByText("此差异较大，每次仅显示一个文件")).toBeTruthy();
    expect(gitFileTrigger(path(0))).toBeTruthy();
    expect(screen.queryByRole("button", { name: `审阅未暂存 ${path(1)}` })).toBeNull();
    expect(screen.getByText("new_0", { exact: true })).toBeTruthy();
    expect((screen.getByRole("button", { name: "上一个文件" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "下一个文件" }));
    expect(screen.getByText("new_1", { exact: true })).toBeTruthy();
    expect(screen.queryByText("new_0", { exact: true })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "上一个文件" }));
    expect(screen.getByText("new_0", { exact: true })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "跳转到文件", exact: true }));
    fireEvent.change(screen.getByRole("combobox", { name: "跳转到文件" }), { target: { value: "file-128" } });
    fireEvent.click(screen.getByRole("option", { name: path(128), exact: true }));
    expect(screen.queryByRole("dialog", { name: "跳转到文件" })).toBeNull();
    expect(gitFileTrigger(path(128))).toBeTruthy();
    expect(screen.getByText("new_128", { exact: true })).toBeTruthy();
    expect(screen.queryByText("new_0", { exact: true })).toBeNull();
    expect((screen.getByRole("button", { name: "下一个文件" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("defaults ordinary Git files open and deleted files closed while keeping explicit overrides", () => {
    useAppStore.setState({ gitChanges: gitFixture([
      { path: "src/active.ts", patch: "@@ -1 +1 @@\n-old\n+active_content", additions: 1, deletions: 1, status: "M" },
      { path: "src/deleted.ts", patch: "deleted file mode 100644\n--- a/src/deleted.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-deleted_content", additions: 0, deletions: 1, status: "D" },
    ]) });
    render(<DiffPanel />);
    expect(gitFileTrigger("src/active.ts").getAttribute("aria-expanded")).toBe("true");
    expect(gitFileTrigger("src/deleted.ts").getAttribute("aria-expanded")).toBe("false");
    expect(screen.getByText("active_content", { exact: true })).toBeTruthy();
    expect(screen.queryByText("deleted_content", { exact: true })).toBeNull();
    fireEvent.click(gitFileTrigger("src/deleted.ts"));
    expect(screen.getByText("deleted_content", { exact: true })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "折叠全部差异" }));
    expect(gitFileTrigger("src/active.ts").getAttribute("aria-expanded")).toBe("false");
    expect(gitFileTrigger("src/deleted.ts").getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "展开全部差异" }));
    expect(gitFileTrigger("src/active.ts").getAttribute("aria-expanded")).toBe("true");
    expect(gitFileTrigger("src/deleted.ts").getAttribute("aria-expanded")).toBe("false");
  });

  it("labels a selected commit as Commit and displays its real subject separately", async () => {
    const sha = defaultOptions.commits[0].sha;
    supplyComparisons(comparisonFixture([{ path: "src/commit.ts", patch: "@@ -1 +1 @@\n-old\n+committed_content", additions: 1, deletions: 1 }], {
      source: "commit", commitSha: sha,
    }));
    render(<DiffPanel />);
    await waitFor(() => expect(readGitReviewOptions).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: /Diff 来源/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: "已提交", exact: true }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Actual commit", exact: true }));
    expect(screen.getByRole("button", { name: "Diff 来源：提交 1", exact: true }).textContent).toBe("提交");
    expect(screen.getByLabelText("选中的提交").textContent).toBe("Actual commit");
    expect(screen.getByLabelText("选中的提交").getAttribute("title")).toBe("Actual commit body");
    expect(screen.getByText("committed_content", { exact: true })).toBeTruthy();
    expect(screen.queryByText(sha)).toBeNull();
    expect(localStorage.getItem("last-explicit-review-diff-filter")).toBe('"unstaged"');
  });

  it("responds to control-protocol diff reviews with control_response", async () => {
    useAppStore.setState({
      diffReview: {
        requestId: "ctrl-diff",
        conversationId: "conv-diff",
        protocol: "control",
        toolName: "write_file",
        diff: "diff --git a/src/app.ts b/src/app.ts\n@@ -1 +1 @@\n-old\n+new",
        files: [{
          path: "src/app.ts",
          patch: "diff --git a/src/app.ts b/src/app.ts\n@@ -1 +1 @@\n-old\n+new",
          additions: 1,
          deletions: 1,
        }],
        selectedPath: "src/app.ts",
        status: "pending",
        mode: "approval",
        fileDecisions: {},
        lineComments: [],
      },
    });

    render(React.createElement(DiffPanel));
    fireEvent.click(screen.getByRole("button", { name: "全部接受" }));

    await waitFor(() => {
      expect(sendPromptResponseCommand).toHaveBeenCalledWith({
        type: "control_response",
        request_id: "ctrl-diff",
        conversation_id: "conv-diff",
        response: {
          subtype: "success",
          response: { action: "approve" },
        },
      });
    });
  });

  it("confirms before discarding a modified file", async () => {
    vi.mocked(showConfirm).mockResolvedValue(true);
    useAppStore.setState({
      gitChanges: gitFixture([{
          path: "src/app.ts",
          patch: "diff --git a/src/app.ts b/src/app.ts\n@@ -1 +1 @@\n-old\n+new",
          additions: 1,
          deletions: 1,
        }]),
    });

    render(React.createElement(DiffPanel));
    fireEvent.click(screen.getByRole("button", { name: /Diff 来源/ }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "未暂存", exact: true }));
    vi.mocked(sendClientCommand).mockClear();

    fireEvent.click(screen.getByRole("button", { name: "放弃 src/app.ts 的更改" }));

    await waitFor(() => {
      expect(showConfirm).toHaveBeenCalledWith(expect.objectContaining({
        title: "放弃更改",
        danger: true,
      }));
      expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ type: "diff.git_revert_file", path: "src/app.ts", confirmed: true }), "diff.git_revert_file", { silent: true });
    });
  });

  it("renders only the selected comparison payload even when both legacy arrays contain the same path", () => {
    const path = "src/same.ts";
    const patch = (value: string) => `diff --git a/${path} b/${path}\n@@ -1 +1 @@\n-old\n+${value}`;
    useAppStore.setState({ gitChanges: gitFixture([{ path, patch: patch("selected_snapshot"), additions: 13, deletions: 5 }], {}, {
      workingTree: [{ path, patch: patch("unselected_working"), additions: 90, deletions: 80 }],
      staged: [{ path, patch: patch("unselected_staged"), additions: 70, deletions: 60 }],
    }) });
    const { container } = render(<DiffPanel />);
    openGitFile(path);
    expect(screen.getByText("selected_snapshot")).toBeTruthy();
    expect(screen.queryByText("unselected_working")).toBeNull();
    expect(screen.queryByText("unselected_staged")).toBeNull();
    expect(screen.getAllByRole("button", { name: `审阅未暂存 ${path}` })).toHaveLength(1);
    expect(container.querySelector(".mc-diff-total-counts")?.textContent).toBe("+13-5");
    expect(useAppStore.getState().requestGitComparison).toHaveBeenCalledWith({ source: "unstaged" });
  });

  it("keeps a branch comparison selected when no default base exists instead of switching to another source", async () => {
    localStorage.setItem("last-explicit-review-diff-filter", JSON.stringify("branch"));
    vi.mocked(readGitReviewOptions).mockResolvedValue({ ...defaultOptions, default_base_branch: null,
      merge_base_sha: null, branches: ["feature"], comparison_status: "expected", reason: "default_branch_unavailable" });
    useAppStore.setState({ gitChanges: gitFixture([], { source: "branch", status: "expected",
      reason: "default_branch_unavailable", snapshot: undefined }) });
    render(<DiffPanel />);
    await screen.findByText("没有可用的默认基准分支");
    expect(screen.getByRole("button", { name: /Diff 来源：此分支/ })).toBeTruthy();
    expect(vi.mocked(useAppStore.getState().requestGitComparison).mock.calls.every(([selection]) => selection.source === "branch")).toBe(true);
    expect(localStorage.getItem("last-explicit-review-diff-filter")).toBe('"branch"');
    expect(screen.queryByRole("button", { name: "全部暂存" })).toBeNull();
  });

  it("forwards the captured source, snapshot and execution owner when acting on one comparison", async () => {
    const file = { path: "src/owner.ts", patch: "@@ -1 +1 @@\n-old\n+new", additions: 1, deletions: 1 };
    const snapshot = { conversationId: "conv-diff", workspaceRoot: "C:\\workspace", source: "unstaged" as const,
      generation: "owned-comparison-7", environmentId: "accepted-environment", cwdUri: "workspace://accepted/repo", hideWhitespace: false };
    useAppStore.setState({ gitChanges: gitFixture([file], { snapshot }) });
    render(<DiffPanel />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "暂存 src/owner.ts" })));
    expect(sendClientCommandAwaitResult).toHaveBeenCalledWith({ type: "diff.git_stage_file", path: "src/owner.ts",
      source: "unstaged", snapshot_generation: "owned-comparison-7", environment_id: "accepted-environment", cwd_uri: "workspace://accepted/repo",
      conversation_id: "conv-diff", workspace: "C:\\workspace", confirmed: undefined }, "diff.git_stage_file", { silent: true });
  });

  it("does not execute an old snapshot's file action after its owner changes", () => {
    useAppStore.setState({ draft: "Keep the current draft", gitChanges: gitFixture([{ path: "src/old.ts",
      patch: "@@ -1 +1 @@\n-old\n+new", additions: 1, deletions: 1 }]) });
    render(<DiffPanel />);
    act(() => useAppStore.setState({ conversationId: "conv-other" }));
    const action = screen.queryByRole("button", { name: "暂存 src/old.ts" });
    if (action) fireEvent.click(action);
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    expect(useAppStore.getState().draft).toBe("Keep the current draft");
  });

  it("does not discard a captured file when navigation begins while confirmation is pending", async () => {
    let answer!: (confirmed: boolean) => void;
    vi.mocked(showConfirm).mockReturnValue(new Promise(resolve => { answer = resolve; }));
    useAppStore.setState({ gitChanges: gitFixture([{ path: "src/app.ts", patch: "@@ -1 +1 @@\n-old\n+new", additions: 1, deletions: 1 }]) });
    render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: "放弃 src/app.ts 的更改" }));
    await waitFor(() => expect(showConfirm).toHaveBeenCalledOnce());
    act(() => useAppStore.setState({ pendingConversationSwitchId: "conv-next" }));
    await act(async () => answer(true));
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
  });
});

describe("snapshot-backed complete-file Diff reading", () => {
  const path = "src/full.ts";
  const patch = `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -50 +50 @@\n-line_50\n+changed_50\n`;
  const oldLines = Array.from({ length: 100 }, (_, index) => `line_${index + 1}\n`);
  const newLines = oldLines.map((line, index) => index === 49 ? "changed_50\n" : line);
  const file: GitChangeFile = { path, patch, additions: 1, deletions: 1, status: "M", oldOid: "a".repeat(40), newOid: null,
    oldMode: "100644", newMode: "100644", revision: "complete-reading-file" };
  const reply = (command: Record<string, any>, results = (command.requests as { side: "old" | "new" }[]).map(request => ({ type: "success",
    path, side: request.side, source_kind: request.side === "old" ? "object" : "disk", lines: request.side === "old" ? oldLines : newLines }))) => ({
    type: "command.result" as const, command: "diff.git_file_contents", level: "info" as const, message: "",
    data: { source: command.source, snapshot_generation: command.snapshot_generation, results },
  });

  it("uses the real forty-ms content command and expands real context by twenty lines or Shift-all", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.mocked(sendClientCommandAwaitResult).mockImplementation(async command => reply(command));
    useAppStore.setState({ gitChanges: gitFixture([file], { snapshot: { conversationId: "conv-diff", workspaceRoot: "C:\\workspace",
      source: "unstaged", generation: "complete-reading-snapshot", environmentId: "execution-owner", cwdUri: "file:///owned/repo", hideWhitespace: false } }) });
    render(<DiffPanel />);
    openGitFile(path);
    await act(async () => { await vi.advanceTimersByTimeAsync(39); });
    expect(sendClientCommandAwaitResult).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(sendClientCommandAwaitResult).toHaveBeenCalledOnce();
    expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ type: "diff.git_file_contents", conversation_id: "conv-diff",
      workspace: "C:\\workspace", environment_id: "execution-owner", cwd_uri: "file:///owned/repo", source: "unstaged", snapshot_generation: "complete-reading-snapshot",
      requests: [{ path, side: "old", format: "text" }, { path, side: "new", format: "text" }], max_object_bytes: 5 * 1024 * 1024 }), "diff.git_file_contents", { silent: true });
    expect(screen.queryByText("line_1", { exact: true })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "向下展开 20 行" }));
    expect(screen.getByText("line_30", { exact: true })).toBeTruthy();
    expect(screen.getByText("line_49", { exact: true })).toBeTruthy();
    expect(screen.queryByText("line_29", { exact: true })).toBeNull();
    expect(screen.queryByText("line_51", { exact: true })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "向下展开 20 行" }), { shiftKey: true });
    expect(screen.getByText("line_1", { exact: true })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "向上展开 20 行" }), { shiftKey: true });
    expect(screen.getByText("line_100", { exact: true })).toBeTruthy();
    expect(sendClientCommandAwaitResult).toHaveBeenCalledOnce();
  });

  it("retains a real partial patch when original objects are unavailable without inventing omitted context", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.mocked(sendClientCommandAwaitResult).mockImplementation(async command => reply(command,
      (command.requests as { side: "old" | "new" }[]).map(request => ({ type: "error", path, side: request.side, error: { type: "not-found" } })) as any));
    useAppStore.setState({ gitChanges: gitFixture([file], { snapshot: { conversationId: "conv-diff", workspaceRoot: "C:\\workspace",
      source: "unstaged", generation: "unavailable-reading-snapshot", hideWhitespace: false } }) });
    render(<DiffPanel />); openGitFile(path);
    await act(async () => { await vi.advanceTimersByTimeAsync(40); });
    expect(screen.getByText("changed_50", { exact: true })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "向下展开 20 行" })).toBeNull();
    expect(screen.queryByText("line_1", { exact: true })).toBeNull();
    expect(screen.queryByText("完整文件内容加载失败")).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(sendClientCommandAwaitResult).toHaveBeenCalledOnce();
  });

  it("does not project late old-owner full content into the replacement comparison or disturb its draft", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let finish!: (result: Awaited<ReturnType<typeof sendClientCommandAwaitResult>>) => void;
    let request!: Record<string, any>;
    vi.mocked(sendClientCommandAwaitResult).mockImplementation(command => {
      request = command;
      return new Promise(resolve => { finish = resolve; });
    });
    useAppStore.setState({ draft: "Unsent current draft", gitChanges: gitFixture([file], { snapshot: { conversationId: "conv-diff", workspaceRoot: "C:\\workspace",
      source: "unstaged", generation: "late-old-reading-snapshot", hideWhitespace: false } }) });
    render(<DiffPanel />); openGitFile(path);
    await act(async () => { await vi.advanceTimersByTimeAsync(40); });
    act(() => useAppStore.setState({ conversationId: "conv-other", workingDirectory: "C:/new-owner", gitChanges: gitFixture([
      { path, patch: "@@ -1 +1 @@\n-latest_old\n+latest_change", additions: 1, deletions: 1 },
    ], { snapshot: { conversationId: "conv-other", workspaceRoot: "C:/new-owner", source: "unstaged", generation: "new-owner-snapshot", hideWhitespace: false } }) }));
    openGitFile(path);
    await act(async () => finish(reply(request)));
    expect(screen.getByText("latest_change", { exact: true })).toBeTruthy();
    expect(screen.queryByText("changed_50", { exact: true })).toBeNull();
    expect(screen.queryByText("line_1", { exact: true })).toBeNull();
    expect(useAppStore.getState().draft).toBe("Unsent current draft");
    expect(sendClientCommandAwaitResult).toHaveBeenCalledOnce();
  });
});

describe("Source ordinary Diff annotations", () => {
  const path = "src/app.ts";
  const patch = `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -100,3 +200,3 @@\n-old value\n+new value\n unchanged\n last context`;
  const review = (workspaceRoot = "C:\\workspace") => ({ requestId: "ordinary-comments", conversationId: "conv-diff", workspaceRoot,
    diff: patch, files: [{ path, patch }], selectedPath: path, mode: "view" as const, status: "viewing" as const, fileDecisions: {}, lineComments: [] });

  it("toggles the real word-diff preference from Changes options without disturbing comment text", () => {
    useAppStore.setState({ diffReview: review() });
    const { container } = render(<DiffPanel />);
    expect(container.querySelectorAll("[data-diff-span]").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "评论 R200" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R200" }), { target: { value: "Keep this draft" } });
    fireEvent.click(screen.getByRole("button", { name: "“变更”选项" }));
    const toggle = screen.getByRole("menuitemcheckbox", { name: "词级差异" });
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(toggle);
    expect(screen.queryByRole("menu", { name: "“变更”选项" })).toBeNull();
    expect(container.querySelectorAll("[data-diff-span]")).toHaveLength(0);
    expect((screen.getByRole("textbox", { name: "评论 R200" }) as HTMLTextAreaElement).value).toBe("Keep this draft");
    fireEvent.click(screen.getByRole("button", { name: "“变更”选项" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "词级差异" }));
    expect(container.querySelectorAll("[data-diff-span]").length).toBeGreaterThan(0);
  });

  it("uses gutter dragging for a real multi-line selection", () => {
    useAppStore.setState({ diffReview: review() }); render(<DiffPanel />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "评论 R200" }), { button: 0 });
    fireEvent.pointerEnter(screen.getByRole("button", { name: "评论 R202" }).closest(".mc-diff-gutter-side")!);
    fireEvent.pointerUp(document);
    const input = screen.getByRole("textbox", { name: "评论 R200–R202" });
    fireEvent.change(input, { target: { value: "The actual selected range" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(readDiffCommentState("conv-diff").comments[0].position).toEqual({ path, side: "right", line: 202, start_line: 200 });
  });

  it.each(["review", "git", "history"])("saves real ordinary comment coordinates from the %s caller without approving a tool", entry => {
    if (entry === "review") useAppStore.setState({ diffReview: review() });
    else if (entry === "git") useAppStore.setState({ diffReview: null, messages: [], gitChanges: gitFixture([{ path, patch, additions: 1, deletions: 1 }]) });
    else useAppStore.setState({ diffReview: null, messages: [
      { id: "u", role: "user", content: "change the value", timestamp: 1 },
      { id: "a", role: "assistant", content: "done", timestamp: 2, toolCalls: [{ id: "edit", name: "apply_patch", status: "success", args: {}, diff: { patch } }] },
    ] as any });
    const { container } = render(<DiffPanel />);
    if (entry === "git") openGitFile(path);
    if (entry === "history") {
      fireEvent.click(screen.getByRole("button", { name: /Diff 来源/ }));
      fireEvent.click(screen.getByRole("menuitemradio", { name: "上一轮", exact: true }));
    }
    fireEvent.click(container.querySelector('[data-diff-text="1:new"]')!);
    expect(screen.queryByRole("textbox", { name: /评论 [LR]/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "评论 R200" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R200" }), { target: { value: "Use the actual new line" } });
    fireEvent.click(screen.getByRole("button", { name: "评论", exact: true }));
    expect(readDiffCommentState("conv-diff").comments).toEqual([{
      type: "comment", content: [{ content_type: "text", text: "Use the actual new line" }],
      position: { path, side: "right", line: 200 }, localWorkspaceRoot: "C:\\workspace",
      localDiffHunk: "@@ -100,3 +200,3 @@\r\n-old value\r\n+new value\r\n unchanged\r\n last context",
    }]);
    expect(sendPromptResponseCommand).not.toHaveBeenCalled();
    expect(useAppStore.getState().diffReview?.lineComments ?? []).toHaveLength(0);
  });

  it("preserves a cross-side range and permits editing and deleting the saved comment", () => {
    useAppStore.setState({ diffReview: review() }); render(<DiffPanel />);
    changeDiffLayout("split");
    fireEvent.click(screen.getByRole("button", { name: "评论 L100" }));
    fireEvent.click(screen.getByRole("button", { name: "评论 R201" }), { shiftKey: true });
    const input = screen.getByRole("textbox", { name: "评论 L100–R201" });
    fireEvent.change(input, { target: { value: "A range across both sides" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(readDiffCommentState("conv-diff").comments[0].position).toEqual({ path, side: "right", line: 201, start_side: "left", start_line: 100 });
    const saved = screen.getByRole("textbox", { name: "评论 L100–R201" });
    fireEvent.focus(saved); fireEvent.change(saved, { target: { value: "Updated range comment" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(readDiffCommentState("conv-diff").comments[0].content[0].text).toBe("Updated range comment");
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    expect(readDiffCommentState("conv-diff").comments).toEqual([]);
  });

  it("retains new and edited comment text across unified/split and file collapse", () => {
    useAppStore.setState({ diffReview: review() }); render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: "评论 R200" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R200" }), { target: { value: "Original draft" } });
    changeDiffLayout("split");
    expect((screen.getByRole("textbox", { name: "评论 R200" }) as HTMLTextAreaElement).value).toBe("Original draft");
    fireEvent.click(screen.getByRole("button", { name: "评论", exact: true }));
    fireEvent.focus(screen.getByRole("textbox", { name: "评论 R200" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R200" }), { target: { value: "Unsaved edit" } });
    changeDiffLayout("unified");
    fireEvent.click(screen.getByRole("button", { name: path, exact: true }));
    fireEvent.click(screen.getByRole("button", { name: path, exact: true }));
    expect((screen.getByRole("textbox", { name: "评论 R200" }) as HTMLTextAreaElement).value).toBe("Unsaved edit");
    expect(readDiffCommentState("conv-diff").comments[0].content[0].text).toBe("Original draft");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(readDiffCommentState("conv-diff").comments[0].content[0].text).toBe("Unsaved edit");
  });

  it("keeps same-path draft owners separate when a conversation changes workspace", () => {
    useAppStore.setState({ diffReview: review("C:/workspace/A") }); render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: "评论 R200" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R200" }), { target: { value: "A pending draft" } });
    act(() => useAppStore.setState({ diffReview: review("C:/workspace/B") }));
    expect(screen.queryByRole("textbox", { name: "评论 R200" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "评论 R200" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R200" }), { target: { value: "B pending draft" } });
    act(() => useAppStore.setState({ diffReview: review("C:/workspace/A") }));
    expect((screen.getByRole("textbox", { name: "评论 R200" }) as HTMLTextAreaElement).value).toBe("A pending draft");
    act(() => useAppStore.setState({ diffReview: review("C:/workspace/B") }));
    expect((screen.getByRole("textbox", { name: "评论 R200" }) as HTMLTextAreaElement).value).toBe("B pending draft");
  });

  it("releases only the unmounted reader's transient drafts and retains saved conversation comments", () => {
    useAppStore.setState({ diffReview: review() });
    const first = render(<DiffPanel />), second = render(<DiffPanel />);
    fireEvent.click(within(first.container).getByRole("button", { name: "评论 R200" }));
    fireEvent.click(within(second.container).getByRole("button", { name: "评论 R201" }));
    expect(Object.keys(readDiffCommentState("conv-diff").drafts)).toHaveLength(2);
    first.unmount();
    expect(Object.keys(readDiffCommentState("conv-diff").drafts)).toHaveLength(1);
    expect(within(second.container).getByRole("textbox", { name: "评论 R201" })).toBeTruthy();
    fireEvent.change(within(second.container).getByRole("textbox", { name: "评论 R201" }), { target: { value: "Keep saved evidence" } });
    fireEvent.click(within(second.container).getByRole("button", { name: "评论", exact: true }));
    second.unmount();
    expect(readDiffCommentState("conv-diff").comments[0].content[0].text).toBe("Keep saved evidence");
    expect(Object.keys(readDiffCommentState("conv-diff").drafts)).toHaveLength(0);
  });

  it("makes an existing new approval draft read-only while its original request is submitted", () => {
    useAppStore.setState({ diffReview: { ...review(), mode: "approval", status: "pending" } }); render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: "评论 R200" }));
    const input = screen.getByRole("textbox", { name: "评论 R200" });
    fireEvent.change(input, { target: { value: "Still pending" } });
    act(() => useAppStore.setState({ diffReview: { ...useAppStore.getState().diffReview!, status: "submitted" } }));
    expect((screen.getByRole("textbox", { name: "评论 R200" }) as HTMLTextAreaElement).readOnly).toBe(true);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(useAppStore.getState().diffReview?.lineComments).toEqual([]);
    expect(screen.queryByRole("button", { name: "评论", exact: true })).toBeNull();
  });

  it("keeps an old approval draft bound to its original request when another request takes the view", () => {
    useAppStore.setState({ diffReview: { ...review(), mode: "approval", status: "pending" } }); render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: "评论 R200" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R200" }), { target: { value: "Only for the first request" } });
    act(() => useAppStore.setState({ diffReview: { ...review(), requestId: "replacement-request", mode: "approval", status: "pending" } }));
    expect(screen.queryByRole("textbox", { name: "评论 R200" })).toBeNull();
    expect(Object.values(readDiffCommentState("conv-diff").drafts).map(draft => draft.text)).toEqual(["Only for the first request"]);
    act(() => useAppStore.setState({ diffReview: { ...review(), mode: "approval", status: "pending" } }));
    expect((screen.getByRole("textbox", { name: "评论 R200" }) as HTMLTextAreaElement).value).toBe("Only for the first request");
  });

  it("restores unsaved comments after switching conversations and after switching Diff sources", () => {
    clearDiffCommentScope("conv-other");
    useAppStore.setState({ diffReview: review() }); render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: "评论 R200" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R200" }), { target: { value: "Conversation A only" } });
    act(() => useAppStore.setState({ conversationId: "conv-other", diffReview: { ...review(), requestId: "other-review", conversationId: "conv-other" } }));
    expect(screen.queryByRole("textbox", { name: "评论 R200" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "评论 R200" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R200" }), { target: { value: "Conversation B only" } });
    act(() => useAppStore.setState({ conversationId: "conv-diff", diffReview: review() }));
    expect((screen.getByRole("textbox", { name: "评论 R200" }) as HTMLTextAreaElement).value).toBe("Conversation A only");
    act(() => useAppStore.setState({ diffReview: null }));
    expect(screen.queryByRole("textbox", { name: "评论 R200" })).toBeNull();
    act(() => useAppStore.setState({ diffReview: review() }));
    expect((screen.getByRole("textbox", { name: "评论 R200" }) as HTMLTextAreaElement).value).toBe("Conversation A only");
    expect(Object.values(readDiffCommentState("conv-other").drafts).map(draft => draft.text)).toEqual(["Conversation B only"]);
  });

  it("reconnects a reopened Changes pane to its existing window-owned in-memory draft", () => {
    useAppStore.setState({ diffReview: review() });
    const first = render(<DiffPanel readerOwnerId="owned-window-changes" />);
    fireEvent.click(screen.getByRole("button", { name: "评论 R200" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 R200" }), { target: { value: "Reopen this pane" } });
    first.unmount();
    const second = render(<DiffPanel readerOwnerId="owned-window-changes" />);
    expect((screen.getByRole("textbox", { name: "评论 R200" }) as HTMLTextAreaElement).value).toBe("Reopen this pane");
    second.unmount();
    clearDiffReaderDrafts("owned-window-changes");
    expect(Object.keys(readDiffCommentState("conv-diff").drafts)).toHaveLength(0);
  });

  it("sends approval feedback with the real deletion side and file line, preserving the approval owner", async () => {
    vi.mocked(sendPromptResponseCommand).mockResolvedValue({ type: "command.result", level: "info", command: "control_response", message: "" });
    useAppStore.setState({ diffReview: { ...review(), mode: "approval", status: "pending", turnId: "turn-owner" } }); render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: "评论 L100" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 L100" }), { target: { value: "Keep the removed behavior" } });
    fireEvent.click(screen.getByRole("button", { name: "评论", exact: true }));
    fireEvent.click(screen.getByRole("button", { name: /提交意见/ }));
    await waitFor(() => expect(sendPromptResponseCommand).toHaveBeenCalledOnce());
    const command = vi.mocked(sendPromptResponseCommand).mock.calls[0][0] as any;
    expect(command).toMatchObject({ type: "control_response", request_id: "ordinary-comments", conversation_id: "conv-diff", turn_id: "turn-owner" });
    expect(command.response.response).toMatchObject({ action: "reject" });
    expect(command.response.response.feedback).toContain("File: src/app.ts\nWorkspace: C:\\workspace\nSide: L\nLines: 100\n");
    expect(command.response.response.feedback).not.toContain("src/app.ts:1:");
    expect(readDiffCommentState("conv-diff").comments).toEqual([]);
  });
});
