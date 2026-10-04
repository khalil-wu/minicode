import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { normalizeMessageContextRefs } from "./transcriptHydration";
import { buildContextPayload } from "../composer/contextPayload";
import { contextReferenceLabel, openContextReference } from "./contextReferenceActions";
import type { FileContextRef } from "../stores/types";

vi.mock("../desktop/runtime", () => ({ isDesktop: () => false }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const range = { startLineNumber: 40, startColumn: 3, endLineNumber: 56, endColumn: 1 };
const reference: FileContextRef = { kind: "file", name: "same.ts", path: "src/same.ts", range, text: "selected body", workspaceRoot: "/repo" };

beforeEach(() => useAppStore.setState({ workingDirectory: "/repo", editorOpenRequests: [], activeEditorOpenRequestId: null, sideChats: {}, sideChatOpen: false }));

describe("code context round trip", () => {
  it("preserves the complete selection through payload, transcript hydration and source navigation", async () => {
    const restored = normalizeMessageContextRefs([JSON.parse(JSON.stringify(reference))])[0];
    expect(restored).toEqual(reference);
    expect(contextReferenceLabel(restored)).toBe("same.ts:40–55");
    expect(await buildContextPayload([restored])).toContain("src/same.ts:40:3-56:1");
    openContextReference(restored);
    expect(useAppStore.getState().editorOpenRequests.at(-1)).toMatchObject({ path: "src/same.ts", line: 40, column: 3, endLine: 56, endColumn: 1 });
  });

  it("keeps a large code selection intact instead of silently discarding its tail", () => {
    const text = `  ${"value\n".repeat(3000)}last line`;
    useAppStore.getState().openSideChatWithSelection(text, "src/same.ts", { range, workspaceRoot: "/repo" });
    expect(useAppStore.getState().sideChatPendingContext).toEqual({ text, source: "src/same.ts", range, workspaceRoot: "/repo" });
  });

  it("does not open an old workspace reference against the current workspace", () => {
    useAppStore.setState({ workingDirectory: "/other" });
    openContextReference(reference);
    expect(useAppStore.getState().editorOpenRequests).toEqual([]);
  });

  it.each([
    ["src/same.ts#L2-L4", 4],
    ["src/same.ts#L2", undefined],
  ])("preserves the explicit line range while keeping single-line %s navigation compatible", (path, expectedEndLine) => {
    openContextReference({ kind: "file", name: "same.ts", path, workspaceRoot: "/repo" });
    const request = useAppStore.getState().editorOpenRequests.at(-1)!;
    expect({ path: request.path, line: request.line, endLine: request.endLine, endColumn: request.endColumn }).toEqual({
      path: "src/same.ts", line: 2, endLine: expectedEndLine, endColumn: undefined,
    });
  });
});
