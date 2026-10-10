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
import type { TurnDiffState } from "../stores/types";

vi.hoisted(() => {
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
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error" && event.level !== "failed",
}));

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

beforeEach(() => {
  __resetOpenWebInBrowserForTests();
  useAppStore.setState({
    conversationId: "conv-diff",
    workingDirectory: "C:\\workspace",
    requestGitChanges: vi.fn(),
    gitReviewRequest: null,
    turnDiffs: {},
    rightPanelExpanded: false,
  });
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
  vi.restoreAllMocks();
  useAppStore.setState({ diffReview: null, messages: [], gitChanges: { workingTree: [], staged: [], untracked: [], loading: false } });
  vi.mocked(sendClientCommand).mockReset();
  vi.mocked(sendClientCommandAwaitResult).mockReset();
  vi.mocked(sendPromptResponseCommand).mockReset();
  vi.mocked(showConfirm).mockReset();
});

describe("DiffPanel", () => {
  it("keeps deferred history files visible and only fetches after a file is selected", async () => {
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
    fireEvent.click(screen.getByRole("button", { name: /Diff 来源/ }));
    fireEvent.click(screen.getByRole("option", { name: /上一轮/ }));
    expect(screen.getByText("工作区比较")).toBeTruthy();
    expect(screen.getByRole("button", { name: "src/b.ts", exact: true }).getAttribute("aria-expanded")).toBe("false");
    expect(load).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "src/b.ts", exact: true }));
    await waitFor(() => expect(screen.getByText("newB")).toBeTruthy());
    expect(load).toHaveBeenCalledTimes(1);
    expect(load.mock.calls[0][0]).toEqual({ conversationId: "conv-diff", messageId: "answer", turnId: "turn", revision: 2 });
    expect(screen.getByRole("button", { name: "src/a.ts", exact: true })).toBeTruthy();
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
      gitChanges: { staged: [], workingTree: scope === "git" ? [{ path: "src/app.ts", patch, additions: 1, deletions: 1 }] : [], untracked: [], loading: false },
    });
    const { container } = render(<DiffPanel />);
    if (scope === "history") {
      fireEvent.click(screen.getByRole("button", { name: /Diff 来源/ }));
      fireEvent.click(screen.getByRole("option", { name: /轮次记录/ }));
    } else if (scope === "git") fireEvent.click(screen.getByRole("button", { name: "审阅未暂存 src/app.ts" }));
    for (const mode of ["unified", "split"]) {
      expect(screen.queryByRole("button", { name: /加入对话|引用|提问|评论 Diff/ })).toBeNull();
      expect(container.querySelector(".mc-diff-line-quote")).toBeNull();
      expect(screen.getByText("new", { exact: true })).toBeTruthy();
      fireEvent.click(screen.getByText("new", { exact: true }));
      expect(useAppStore.getState().draft).toBe("Keep the draft");
      expect(useAppStore.getState().selectedMentions).toEqual([]);
      if (mode === "unified") fireEvent.click(screen.getByRole("button", { name: "切换为分栏视图" }));
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
    fireEvent.click(second);
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
    fireEvent.click(screen.getByRole("option", { name: /轮次记录 · 上一轮/ }));
    expect(screen.queryByRole("combobox", { name: "轮次修改文件" })).toBeNull();
    expect(screen.queryByText("intermediate")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "src/final-b.ts", exact: true }));
    expect(screen.getByText("finalA")).toBeTruthy();
    expect(screen.getByText("finalB")).toBeTruthy();
    expect(container.querySelector(".mc-diff-total-counts")?.textContent).toBe("+2-2");
    expect(screen.getByRole("button", { name: "src/final-a.ts", exact: true }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("button", { name: "src/final-b.ts", exact: true }).getAttribute("aria-expanded")).toBe("true");
  });

  it("keeps two Git file patches open and collapses only the clicked range", () => {
    const patch = (path: string, text: string) => `diff --git a/${path} b/${path}\n@@ -1 +1 @@\n-old\n+${text}`;
    useAppStore.setState({ gitChanges: { staged: [], untracked: [], loading: false, workingTree: [
      { path: "src/a.ts", patch: patch("src/a.ts", "gitA"), additions: 1, deletions: 1 },
      { path: "src/b.ts", patch: patch("src/b.ts", "gitB"), additions: 1, deletions: 1 },
    ] } });
    render(<DiffPanel />);
    const first = screen.getByRole("button", { name: "审阅未暂存 src/a.ts" });
    const second = screen.getByRole("button", { name: "审阅未暂存 src/b.ts" });
    fireEvent.click(first); fireEvent.click(second);
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
    expect(screen.getByText("中间省略 399 行")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "评论 Diff 第 4 行" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 Diff 第 4 行" }), { target: { value: "保留这里的说明" } });
    fireEvent.click(screen.getByRole("button", { name: "切换为分栏视图" }));
    expect((screen.getByRole("textbox", { name: "评论 Diff 第 4 行" }) as HTMLInputElement).value).toBe("保留这里的说明");
    expect(screen.getByRole("button", { name: "在编辑器中打开 src/app.ts 第 520 行" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    expect(useAppStore.getState().diffReview?.lineComments).toEqual([{ filePath: "src/app.ts", lineIndex: 3, content: "保留这里的说明" }]);
    fireEvent.click(screen.getByRole("button", { name: "切换为行内视图" }));
    expect(screen.getByText("保留这里的说明")).toBeTruthy();
    expect(screen.getByText("中间省略 399 行")).toBeTruthy();
  });

  it("preserves an exact text selection while changing inline and split views", () => {
    const patch = "diff --git a/src/app.ts b/src/app.ts\n@@ -100 +120 @@\n-old value\n+new value";
    useAppStore.setState({ diffReview: { requestId: "selection-mode", conversationId: "conv-diff", diff: patch, files: [{ path: "src/app.ts", patch }], selectedPath: "src/app.ts", status: "viewing", mode: "view", fileDecisions: {} } });
    const { container } = render(<DiffPanel />);
    const text = document.createTreeWalker(container.querySelector('[data-diff-text="1:new"]')!, NodeFilter.SHOW_TEXT).nextNode()!;
    const range = document.createRange();
    range.setStart(text, 1); range.setEnd(text, 6);
    window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
    fireEvent(document, new Event("selectionchange"));
    fireEvent.click(screen.getByRole("button", { name: "切换为分栏视图" }));
    expect(window.getSelection()!.toString()).toBe("ew va");
    fireEvent.click(screen.getByRole("button", { name: "切换为行内视图" }));
    expect(window.getSelection()!.toString()).toBe("ew va");
    window.getSelection()!.removeAllRanges();
  });

  it("opens the requested staged range even when the same path also has working changes", () => {
    const before = "diff --git a/src/app.ts b/src/app.ts\n@@ -10 +10 @@\n-base\n+staged value";
    const after = "diff --git a/src/app.ts b/src/app.ts\n@@ -10 +10 @@\n-staged value\n+working value";
    useAppStore.setState({ gitChanges: { staged: [{ path: "src/app.ts", patch: before, additions: 1, deletions: 1 }], workingTree: [{ path: "src/app.ts", patch: after, additions: 1, deletions: 1 }], untracked: [], loading: false } });
    render(<DiffPanel />);
    act(() => useAppStore.getState().openGitReview({ path: "src/app.ts", section: "staged", workspaceRoot: "C:\\workspace", conversationId: "conv-diff" }));
    expect(screen.getByText("base")).toBeTruthy();
    expect(screen.queryByText("working value")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "审阅未暂存 src/app.ts" }));
    expect(screen.getByText("base")).toBeTruthy();
    expect(screen.getByText("working value")).toBeTruthy();
    expect(useAppStore.getState().gitReviewRequest?.section).toBe("working");
    expect(within(screen.getByRole("region", { name: "审阅未暂存 src/app.ts" })).queryByRole("button", { name: /加入对话/ })).toBeNull();
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
      if (viewMode === "split") fireEvent.click(screen.getByRole("button", { name: "切换为分栏视图" }));
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

  it("closes a file's active line feedback before switching to another review file", () => {
    const first = "diff --git a/src/a.ts b/src/a.ts\n@@ -1 +1 @@\n-oldA\n+newA";
    const second = "diff --git a/src/b.ts b/src/b.ts\n@@ -1 +1 @@\n-oldB\n+newB";
    useAppStore.setState({ diffReview: {
      requestId: "feedback-files", conversationId: "conv-diff", toolName: "edit", diff: first,
      files: [{ path: "src/a.ts", patch: first }, { path: "src/b.ts", patch: second }],
      selectedPath: "src/a.ts", status: "pending", mode: "approval", fileDecisions: {}, lineComments: [],
    } });
    render(<DiffPanel />);
    fireEvent.click(screen.getByRole("button", { name: "评论 Diff 第 2 行" }));
    fireEvent.change(screen.getByRole("textbox", { name: "评论 Diff 第 2 行" }), { target: { value: "For file A" } });
    fireEvent.click(screen.getByRole("button", { name: "src/b.ts" }));
    expect(screen.queryByRole("textbox", { name: /评论 Diff/ })).toBeNull();
    expect(useAppStore.getState().diffReview?.lineComments).toEqual([]);
  });

  it("shows a plain-folder state and permits detecting a newly initialized repository", () => {
    useAppStore.setState({
      diffReview: null, messages: [],
      gitChanges: { isGitRepo: false, workingTree: [], staged: [], untracked: [], loading: false },
    });
    render(<DiffPanel />);

    expect(screen.getByText("当前文件夹未启用 Git")).toBeTruthy();
    expect(screen.queryByText("没有未提交的更改")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "刷新 Git 状态" }));
    expect(useAppStore.getState().requestGitChanges).toHaveBeenCalledTimes(2);
  });

  it("keeps repository failures visible without reporting a clean worktree", () => {
    useAppStore.setState({
      diffReview: null, messages: [],
      gitChanges: { error: "Repository permission denied", workingTree: [], staged: [], untracked: [], loading: false },
    });
    render(<DiffPanel />);
    expect(screen.getByText("无法加载 Git 更改")).toBeTruthy();
    expect(screen.getByText("Repository permission denied")).toBeTruthy();
    expect(screen.queryByText("没有未提交的更改")).toBeNull();
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
    useAppStore.setState({
      gitChanges: {
        workingTree: [{
          path: "src/app.ts",
          patch: "diff --git a/src/app.ts b/src/app.ts\n@@ -1 +1 @@\n-old\n+new",
          additions: 1,
          deletions: 1,
        }],
        staged: [{
          path: "src/old.ts",
          patch: "diff --git a/src/old.ts b/src/old.ts\n@@ -1 +1 @@\n-old\n+new",
          additions: 1,
          deletions: 1,
        }],
        untracked: ["src/new.ts"],
        loading: false,
      },
    });

    render(React.createElement(DiffPanel));
    expect(screen.getByRole("button", { name: /Diff 来源：未提交 3/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Diff 来源/ }));
    expect(screen.getByRole("option", { name: /轮次记录/ })).toBeTruthy();
    vi.mocked(sendClientCommand).mockClear();

    await act(async () => fireEvent.click(screen.getByRole("button", { name: "全部暂存" })));
    expect(sendClientCommandAwaitResult).toHaveBeenLastCalledWith(expect.objectContaining({ type: "diff.git_stage_all" }), "diff.git_stage_all", { silent: true });

    await act(async () => fireEvent.click(screen.getByRole("button", { name: "全部取消暂存" })));
    expect(sendClientCommandAwaitResult).toHaveBeenLastCalledWith(expect.objectContaining({ type: "diff.git_unstage_all" }), "diff.git_unstage_all", { silent: true });

    await act(async () => fireEvent.click(screen.getByRole("button", { name: "暂存 src/app.ts" })));
    expect(sendClientCommandAwaitResult).toHaveBeenLastCalledWith(expect.objectContaining({ type: "diff.git_stage_file", path: "src/app.ts" }), "diff.git_stage_file", { silent: true });

    await act(async () => fireEvent.click(screen.getByRole("button", { name: "取消暂存 src/old.ts" })));
    expect(sendClientCommandAwaitResult).toHaveBeenLastCalledWith(expect.objectContaining({ type: "diff.git_unstage_file", path: "src/old.ts" }), "diff.git_unstage_file", { silent: true });
  });

  it("previews large git diffs and batches long changed-file lists", () => {
    const largePatch = [
      "diff --git a/src/file-000.ts b/src/file-000.ts",
      "@@ -1,1000 +1,1000 @@",
      ...Array.from({ length: 1000 }, (_, index) => ` line_${index}`),
    ].join("\n");
    useAppStore.setState({
      gitChanges: {
        workingTree: Array.from({ length: 53 }, (_, index) => ({
          path: `src/file-${String(index).padStart(3, "0")}.ts`,
          patch: index === 0 ? largePatch : `diff --git a/src/file-${index}.ts b/src/file-${index}.ts\n@@ -1 +1 @@\n-old\n+new`,
          additions: index === 0 ? 0 : 1,
          deletions: index === 0 ? 0 : 1,
        })),
        staged: [],
        untracked: [],
        loading: false,
      },
    });

    render(React.createElement(DiffPanel));

    expect(screen.getByRole("button", { name: "审阅未暂存 src/file-000.ts" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "审阅未暂存 src/file-052.ts" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "再显示 5 个已修改文件" }));
    expect(screen.getByRole("button", { name: "审阅未暂存 src/file-052.ts" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "审阅未暂存 src/file-000.ts" }));
    expect(screen.getByText(/另有 .* 行 Diff 已隐藏/)).toBeTruthy();
    expect(screen.queryByText("line_999")).toBeNull();
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
      gitChanges: {
        workingTree: [{
          path: "src/app.ts",
          patch: "diff --git a/src/app.ts b/src/app.ts\n@@ -1 +1 @@\n-old\n+new",
          additions: 1,
          deletions: 1,
        }],
        staged: [],
        untracked: [],
        loading: false,
      },
    });

    render(React.createElement(DiffPanel));
    fireEvent.click(screen.getByRole("button", { name: /Diff 来源/ }));
    fireEvent.click(screen.getByRole("option", { name: /未提交/ }));
    vi.mocked(sendClientCommand).mockClear();

    fireEvent.click(screen.getByRole("button", { name: "放弃 src/app.ts 的更改" }));

    await waitFor(() => {
      expect(showConfirm).toHaveBeenCalledWith(expect.objectContaining({
        title: "放弃文件更改",
        danger: true,
      }));
      expect(sendClientCommandAwaitResult).toHaveBeenCalledWith(expect.objectContaining({ type: "diff.git_revert_file", path: "src/app.ts", confirmed: true }), "diff.git_revert_file", { silent: true });
    });
  });
});
