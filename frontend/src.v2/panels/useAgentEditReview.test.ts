/* @vitest-environment jsdom */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAgentEditReview, type AgentEditReviewModel } from "./useAgentEditReview";
import type { TurnDiffState } from "../stores/types";

/**
 * A fake Monaco editor recording the edits the hook applies. The buffer is
 * modeled as a plain array of lines so undo/keep behavior is observable
 * without a real Monaco instance.
 */
function fakeEditor(lines: string[]) {
  const state = { lines: [...lines], decorations: [] as unknown[], position: { lineNumber: 1, column: 1 }, revealed: [] as number[], focused: 0 };
  const collection = {
    set: (d: unknown[]) => { state.decorations = d; },
    clear: () => { state.decorations = []; },
  };
  const editor: NonNullable<AgentEditReviewModel["editor"]> = {
    getModel: () => ({
      getLineCount: () => state.lines.length,
      getLineMaxColumn: (line: number) => (state.lines[line - 1]?.length ?? 0) + 1,
      getEOL: () => "\n",
    }),
    createDecorationsCollection: () => collection,
    executeEdits: (_source, edits) => {
      // Model the col-1..col-1 block replacements the hook emits: the text
      // after the range begins at the start of endLine, so lines [start, end)
      // are replaced by the inserted lines. A trailing newline in the edit text
      // maps to the line boundary, not a new empty line.
      for (const edit of edits) {
        const range = edit.range as { startLineNumber: number; endLineNumber: number };
        const before = state.lines.slice(0, range.startLineNumber - 1);
        const after = state.lines.slice(range.endLineNumber - 1);
        const parts = edit.text.split("\n");
        if (parts.length > 1 && parts[parts.length - 1] === "") parts.pop();
        const inserted = edit.text === "" ? [] : parts;
        state.lines = [...before, ...inserted, ...after];
      }
    },
    setPosition: (position) => { state.position = position; },
    revealLineInCenter: (line) => { state.revealed.push(line); },
    focus: () => { state.focused += 1; },
  };
  return { editor, state };
}

const WORKDIR = "/repo";

function turnDiffFor(path: string, patch: string): TurnDiffState {
  const diff = [
    `diff --git a/${path} b/${path}`,
    "index 000..111 100644",
    `--- a/${path}`,
    `+++ b/${path}`,
    patch,
  ].join("\n");
  return { threadId: "t1", turnId: "turn-1", diff, updatedAt: 1, revision: 1 };
}

describe("useAgentEditReview", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  const content = "alpha\nBRAVO\ncharlie\n";
  const patch = "@@ -1,3 +1,3 @@\n alpha\n-bravo\n+BRAVO\n charlie";

  it("reports the reviewable blocks for the active file", () => {
    const { editor } = fakeEditor(content.split("\n"));
    const editorRef = { current: editor };
    const { result } = renderHook(() =>
      useAgentEditReview({
        editorRef,
        path: "src/app.ts",
        content,
        readOnly: false,
        turnDiff: turnDiffFor("src/app.ts", patch),
        workingDirectory: WORKDIR,
        editorEpoch: 1,
      }),
    );
    expect(result.current.total).toBe(1);
  });

  it("ignores diffs that do not touch the active file", () => {
    const { editor } = fakeEditor(content.split("\n"));
    const editorRef = { current: editor };
    const { result } = renderHook(() =>
      useAgentEditReview({
        editorRef,
        path: "src/other.ts",
        content,
        readOnly: false,
        turnDiff: turnDiffFor("src/app.ts", patch),
        workingDirectory: WORKDIR,
        editorEpoch: 1,
      }),
    );
    expect(result.current.total).toBe(0);
  });

  it("shows nothing for a read-only buffer", () => {
    const { editor } = fakeEditor(content.split("\n"));
    const editorRef = { current: editor };
    const { result } = renderHook(() =>
      useAgentEditReview({
        editorRef,
        path: "src/app.ts",
        content,
        readOnly: true,
        turnDiff: turnDiffFor("src/app.ts", patch),
        workingDirectory: WORKDIR,
        editorEpoch: 1,
      }),
    );
    expect(result.current.total).toBe(0);
  });

  it("keep dismisses the current block without editing the buffer", () => {
    const { editor, state } = fakeEditor(content.split("\n"));
    const editorRef = { current: editor };
    const { result } = renderHook(() =>
      useAgentEditReview({
        editorRef,
        path: "src/app.ts",
        content,
        readOnly: false,
        turnDiff: turnDiffFor("src/app.ts", patch),
        workingDirectory: WORKDIR,
        editorEpoch: 1,
      }),
    );
    expect(result.current.total).toBe(1);
    act(() => result.current.keep());
    expect(result.current.total).toBe(0);
    expect(state.lines.join("\n")).toBe(content);
  });

  it("undo restores the removed text through the editor", () => {
    const { editor, state } = fakeEditor(["alpha", "BRAVO", "charlie", ""]);
    const editorRef = { current: editor };
    const { result } = renderHook(() =>
      useAgentEditReview({
        editorRef,
        path: "src/app.ts",
        content,
        readOnly: false,
        turnDiff: turnDiffFor("src/app.ts", patch),
        workingDirectory: WORKDIR,
        editorEpoch: 1,
      }),
    );
    act(() => result.current.undo());
    expect(state.lines).toEqual(["alpha", "bravo", "charlie", ""]);
    expect(result.current.total).toBe(0);
    expect(state.focused).toBeGreaterThan(0);
  });

  it("navigation reveals blocks and moves the cursor", () => {
    const twoBlock = "line1\nADDED\nline2\nline3\nALSO\nline4\n";
    const twoPatch = "@@ -1,4 +1,6 @@\n line1\n+ADDED\n line2\n line3\n+ALSO\n line4";
    const { editor, state } = fakeEditor(twoBlock.split("\n"));
    const editorRef = { current: editor };
    const { result } = renderHook(() =>
      useAgentEditReview({
        editorRef,
        path: "src/app.ts",
        content: twoBlock,
        readOnly: false,
        turnDiff: turnDiffFor("src/app.ts", twoPatch),
        workingDirectory: WORKDIR,
        editorEpoch: 1,
      }),
    );
    expect(result.current.total).toBe(2);
    act(() => result.current.next());
    expect(state.revealed.length).toBeGreaterThan(0);
    expect(state.position.lineNumber).toBeGreaterThan(0);
  });

  it("keepAll clears every block at once", () => {
    const twoBlock = "line1\nADDED\nline2\nline3\nALSO\nline4\n";
    const twoPatch = "@@ -1,4 +1,6 @@\n line1\n+ADDED\n line2\n line3\n+ALSO\n line4";
    const { editor } = fakeEditor(twoBlock.split("\n"));
    const editorRef = { current: editor };
    const { result } = renderHook(() =>
      useAgentEditReview({
        editorRef,
        path: "src/app.ts",
        content: twoBlock,
        readOnly: false,
        turnDiff: turnDiffFor("src/app.ts", twoPatch),
        workingDirectory: WORKDIR,
        editorEpoch: 1,
      }),
    );
    expect(result.current.total).toBe(2);
    act(() => result.current.keepAll());
    expect(result.current.total).toBe(0);
  });

  it("locates the displayed current change without skipping it or editing the buffer", () => {
    const content = "line1\nADDED\nline2\nline3\nALSO\nline4\n";
    const patch = "@@ -1,4 +1,6 @@\n line1\n+ADDED\n line2\n line3\n+ALSO\n line4";
    const { editor, state } = fakeEditor(content.split("\n"));
    const { result } = renderHook(() => useAgentEditReview({
      editorRef: { current: editor }, path: "src/app.ts", content, readOnly: false,
      turnDiff: turnDiffFor("src/app.ts", patch), workingDirectory: WORKDIR, editorEpoch: 1,
    }));

    expect(result.current.currentLine).toBe(2);
    act(() => result.current.reveal());
    expect(state.position).toEqual({ lineNumber: 2, column: 1 });
    act(() => result.current.onCursorLine(6));
    expect(result.current.currentLine).toBe(5);
    act(() => result.current.reveal());
    expect(state.revealed).toEqual([2, 5]);
    expect(state.lines.join("\n")).toBe(content);
    expect(result.current.total).toBe(2);
  });
});
