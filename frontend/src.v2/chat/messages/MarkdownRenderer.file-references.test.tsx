// @vitest-environment jsdom
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../../stores";
import { MarkdownRenderer } from "./MarkdownRenderer";

const { previewFile } = vi.hoisted(() => ({ previewFile: vi.fn() }));
vi.mock("../openAttachmentPreview", () => ({
  openWorkspaceFilePreview: previewFile,
  openLocalFilePreview: vi.fn(),
}));

const originalState = useAppStore.getState();
const openFile = vi.fn();
const root = "C:/projects/demo";
const path = "pelican-bicycle-3d.html";

beforeEach(() => {
  openFile.mockClear();
  previewFile.mockClear();
  useAppStore.setState({ workingDirectory: root, openEditorFile: openFile });
});
afterEach(() => {
  cleanup();
  useAppStore.setState({ workingDirectory: originalState.workingDirectory, openEditorFile: originalState.openEditorFile });
});

describe("Markdown file reference projection", () => {
  const locations = [
    { suffix: ":13", line: 13, column: undefined },
    { suffix: ":13:4", line: 13, column: 4 },
    { suffix: ":13-14", line: 13, column: undefined },
    { suffix: ":13–14", line: 13, column: undefined },
    { suffix: ":13—14", line: 13, column: undefined },
    { suffix: "#L13-L14", line: 13, column: undefined },
    { suffix: "#L13-14", line: 13, column: undefined },
  ];

  describe.each(["link", "code", "bare"] as const)("%s references", (format) => {
    it.each(locations)("preserves the reference presentation and opens the start location for $suffix", ({ suffix, line, column }) => {
      const label = `${path}${suffix}`;
      const content = format === "link" ? `[${label}](${label})` : format === "code" ? `\`${label}\`` : `检查 ${label}。`;
      const view = render(<MarkdownRenderer content={content} workspaceRoot={root} knownFilePaths={[path]} />);
      const chip = view.getByRole("button", { name: label });
      expect(chip.getAttribute("data-presentation")).toBe(format === "code" ? "code" : "link");
      expect(Boolean(chip.querySelector(".md-file-link-icon"))).toBe(format !== "code");
      expect(chip.querySelector(".md-file-chip-name")?.textContent).toBe(path);
      expect(chip.querySelector(".md-file-chip-meta")?.textContent).toBe(suffix);
      expect(view.container.querySelector('[data-kind="folder"]')).toBeNull();
      fireEvent.click(chip);
      expect(openFile).toHaveBeenCalledWith(path, undefined, { line, column });
    });
  });

  it.each(locations)("retains a basename href ending in $suffix when its label is descriptive", ({ suffix, line, column }) => {
    const view = render(<MarkdownRenderer content={`[检查控制条](${path}${suffix})`} workspaceRoot={root} />);
    fireEvent.click(view.getByRole("button", { name: "检查控制条" }));
    expect(openFile).toHaveBeenCalledWith(path, undefined, { line, column });
  });

  it("renders every file location in the reported table with the same chip semantics", () => {
    const view = render(<MarkdownRenderer content={[
      "| 问题 | 位置 |",
      "| --- | --- |",
      "| 旧监听 API | `pelican-bicycle.html:311`、`qinshihuang-polarbear.html:286` |",
      "| 控制条 | [pelican-bicycle-3d.html:13–14](pelican-bicycle-3d.html:13–14) |",
      "| 滚轮单位 | `pelican-bicycle-3d.html:108` |",
      "| 项目说明 | `项目内容概览.md:117` |",
    ].join("\n")} workspaceRoot={root} knownFilePaths={[
      "pelican-bicycle.html", "qinshihuang-polarbear.html", path, "项目内容概览.md",
    ]} />);
    expect(view.container.querySelectorAll("td .md-file-chip")).toHaveLength(5);
    expect(view.container.querySelectorAll("td .md-file-link-icon")).toHaveLength(1);
    expect(view.container.querySelectorAll("td .md-file-code-reference")).toHaveLength(4);
    expect(view.container.querySelector("td a.md-text-link")).toBeNull();
  });

  it.each(["report.pdf", "report.docx", "metrics.csv", "diagram.svg"])("gives a known bare %s the same preview as a Markdown link", (file) => {
    const view = render(<MarkdownRenderer content={`查看 ${file}.`} workspaceRoot={root} conversationId="owner" knownFilePaths={[file]} />);
    const chip = view.getByRole("button", { name: file });
    expect(chip.querySelector(".md-file-link-icon")).not.toBeNull();
    fireEvent.click(chip);
    expect(previewFile).toHaveBeenCalledWith({ path: file, name: file, workspaceRoot: root, conversationId: "owner" });
    expect(openFile).not.toHaveBeenCalled();
  });

  it("uses the HTML type icon and editor action for HTM files", () => {
    const view = render(<MarkdownRenderer content="[页面](index.htm:9–10)" workspaceRoot={root} />);
    const chip = view.getByRole("button", { name: "页面" });
    expect(chip.getAttribute("data-ext")).toBe("htm");
    expect(chip.querySelector(".md-file-link-icon")).not.toBeNull();
    fireEvent.click(chip);
    expect(openFile).toHaveBeenCalledWith("index.htm", undefined, { line: 9, column: undefined });
  });

  it.each(["`页面`", "**页面**"])("keeps a Windows destination containing spaces behind the formatted label %s", (label) => {
    const view = render(<MarkdownRenderer content={`[${label}](C:\\projects\\demo\\my page.html:13–14)`} workspaceRoot={root} />);
    const chip = view.getByRole("button", { name: "页面" });
    expect(chip.querySelector(".md-file-link-icon")).not.toBeNull();
    fireEvent.click(chip);
    expect(openFile).toHaveBeenCalledWith("C:/projects/demo/my page.html", undefined, { line: 13, column: undefined });
  });

  it("keeps external web targets even when their label is a local file range", () => {
    const url = "https://example.com/review";
    const view = render(<MarkdownRenderer content={`[${path}:13–14](${url})`} workspaceRoot={root} knownFilePaths={[path]} />);
    expect(view.getByRole("link", { name: `${path}:13–14` }).getAttribute("href")).toBe(url);
    expect(view.container.querySelector(".md-file-chip")).toBeNull();
  });

  it("keeps unverified or ambiguous ranges as source text", () => {
    const view = render(<MarkdownRenderer content={`检查 ${path}:13–14 和 \`${path}#L13-L14\`。`} workspaceRoot={root} knownFilePaths={[`one/${path}`, `two/${path}`]} />);
    expect(view.container.querySelector(".md-file-chip")).toBeNull();
    expect(view.container.textContent).toContain(`${path}:13–14`);
    expect(view.container.querySelector("code")?.textContent).toBe(`${path}#L13-L14`);
  });

  it("retains file icons and line targets as streaming content settles", () => {
    const content = `检查 \`${path}:13–14\`。\n\n`;
    const view = render(<MarkdownRenderer content={content} isStreaming workspaceRoot={root} knownFilePaths={[path]} />);
    const chip = view.getByRole("button", { name: `${path}:13–14` });
    view.rerender(<MarkdownRenderer content={`${content}已完成。`} isStreaming={false} workspaceRoot={root} knownFilePaths={[path]} />);
    expect(view.getByRole("button", { name: `${path}:13–14` })).toBe(chip);
    fireEvent.click(chip);
    expect(openFile).toHaveBeenCalledWith(path, undefined, { line: 13, column: undefined });
  });
});
