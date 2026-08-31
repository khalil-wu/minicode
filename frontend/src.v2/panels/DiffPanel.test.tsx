/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { sendClientCommand, sendClientCommandAwaitResult, sendPromptResponseCommand } from "../protocol/ws-outbox";
import { showConfirm } from "../overlays/DialogService";
import { DiffPanel } from "./DiffPanel";

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
  useAppStore.setState({
    conversationId: "conv-diff",
    workingDirectory: "C:\\workspace",
    requestGitChanges: vi.fn(),
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
  useAppStore.setState({ diffReview: null, messages: [], gitChanges: { workingTree: [], staged: [], untracked: [], loading: false } });
  vi.mocked(sendClientCommand).mockReset();
  vi.mocked(sendClientCommandAwaitResult).mockReset();
  vi.mocked(sendPromptResponseCommand).mockReset();
  vi.mocked(showConfirm).mockReset();
});

describe("DiffPanel", () => {
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

    expect(screen.getByRole("button", { name: /Diff 来源：待审阅 1/ })).toBeTruthy();
    expect(screen.getByText("Diff")).toBeTruthy();
    expect(screen.getByText("edit_file")).toBeTruthy();
    expect(screen.queryByText("edit_file审批")).toBeNull();
    expect(screen.queryByRole("button", { name: "全部接受" })).toBeNull();
    expect(screen.queryByRole("button", { name: "全部拒绝" })).toBeNull();
  });

  it("keeps syntax highlighting out of added and deleted diff rows", () => {
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
    expect(screen.queryByTestId("syntax-add")).toBeNull();
    expect(screen.queryByTestId("syntax-del")).toBeNull();
    expect(screen.getByText("from new_module import NewThing")).toBeTruthy();
    expect(screen.getByText("from old_module import OldThing")).toBeTruthy();
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

  it("sends git action commands from the changes panel", () => {
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
    expect(screen.getByRole("option", { name: /上一轮/ })).toBeTruthy();
    vi.mocked(sendClientCommand).mockClear();

    fireEvent.click(screen.getByRole("button", { name: "全部暂存" }));
    expect(sendClientCommand).toHaveBeenLastCalledWith(expect.objectContaining({ type: "diff.git_stage_all" }));

    fireEvent.click(screen.getByRole("button", { name: "全部取消暂存" }));
    expect(sendClientCommand).toHaveBeenLastCalledWith(expect.objectContaining({ type: "diff.git_unstage_all" }));

    fireEvent.click(screen.getByRole("button", { name: "暂存 src/app.ts" }));
    expect(sendClientCommand).toHaveBeenLastCalledWith(expect.objectContaining({ type: "diff.git_stage_file", path: "src/app.ts" }));

    fireEvent.click(screen.getByRole("button", { name: "取消暂存 src/old.ts" }));
    expect(sendClientCommand).toHaveBeenLastCalledWith(expect.objectContaining({ type: "diff.git_unstage_file", path: "src/old.ts" }));
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

    expect(screen.getByText("src/file-000.ts")).toBeTruthy();
    expect(screen.queryByText("src/file-052.ts")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "再显示 5 个已修改文件" }));
    expect(screen.getByText("src/file-052.ts")).toBeTruthy();

    fireEvent.click(screen.getByText("src/file-000.ts"));
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
      expect(sendClientCommand).toHaveBeenCalledWith(expect.objectContaining({ type: "diff.git_revert_file", path: "src/app.ts", confirmed: true }));
    });
  });
});
