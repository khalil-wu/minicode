// @vitest-environment jsdom
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../../stores";
import { MarkdownRenderer } from "./MarkdownRenderer";

const openFile = vi.fn();
const revealFolder = vi.fn();
beforeEach(() => {
  openFile.mockClear();
  revealFolder.mockClear();
  useAppStore.setState({ workingDirectory: "C:/projects/demo", openEditorFile: openFile, requestFileTreeReveal: revealFolder });
});
afterEach(cleanup);

describe("inline file icon semantics", () => {
  it("renders CJS and MJS as editable code files rather than folders", () => {
    const view = render(<MarkdownRenderer content="新增 [regression.test.cjs](regression.test.cjs)，检查 [runner.mjs](runner.mjs)。" workspaceRoot="C:/projects/demo" />);
    const cjs = view.getByRole("button", { name: "regression.test.cjs" });
    const mjs = view.getByRole("button", { name: "runner.mjs" });
    expect(cjs.getAttribute("data-ext")).toBe("cjs");
    expect(mjs.getAttribute("data-ext")).toBe("mjs");
    expect(view.container.querySelector('[data-kind="folder"]')).toBeNull();
    fireEvent.click(cjs);
    expect(openFile).toHaveBeenCalledWith("regression.test.cjs", undefined, { line: undefined, column: undefined });
    expect(revealFolder).not.toHaveBeenCalled();
  });

  it("keeps bare known Node module references and line targets", () => {
    const view = render(<MarkdownRenderer content="检查 regression.test.cjs 和 runner.mjs:7。" workspaceRoot="C:/projects/demo" knownFilePaths={["regression.test.cjs", "runner.mjs"]} />);
    expect(view.container.querySelector('[data-ext="cjs"]')).not.toBeNull();
    const mjs = view.container.querySelector('[data-ext="mjs"]')!;
    fireEvent.click(mjs);
    expect(openFile).toHaveBeenCalledWith("runner.mjs", undefined, { line: 7, column: undefined });
  });

  it("marks only document images for content-image layout", () => {
    const view = render(<MarkdownRenderer content={'[目录](src/)\n\n![正文](data:image/png;base64,iVBORw0KGgo=)'} workspaceRoot="C:/projects/demo" />);
    expect(view.container.querySelector("img.md-content-image")).not.toBeNull();
    const folder = view.container.querySelector(".md-folder-chip-icon img")!;
    expect(folder).not.toBeNull();
    expect(folder.classList.contains("md-content-image")).toBe(false);
  });
});
