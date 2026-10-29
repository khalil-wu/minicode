/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { InlineDiff } from "./InlineDiff";
import { TranscriptReadingContext } from "../transcriptReadingState";
import { TranscriptSearchContext } from "../TranscriptSearchContext";
import { useColorizedLines } from "../../lib/monaco-colorize";

vi.mock("../../lib/monaco-colorize", async (original) => ({
  ...await original<typeof import("../../lib/monaco-colorize")>(),
  useColorizedLines: vi.fn((lines: Array<{ kind: string; text: string }>) => lines.map(line => {
    if (!["context", "add", "del"].includes(line.kind)) return "";
    const token = document.createElement("span");
    token.className = "mock-syntax-token";
    token.textContent = line.text;
    return token.outerHTML;
  })),
}));

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("InlineDiff", () => {
  it("collapses files independently, skips hidden syntax work, and preserves context choices through search", () => {
    const patch = [
      "diff --git a/one.ts b/one.ts", "--- a/one.ts", "+++ b/one.ts", "@@ -1,3 +1,3 @@", " stable", " context", "-old", "+new",
      "diff --git a/two.py b/two.py", "--- a/two.py", "+++ b/two.py", "@@ -1 +1 @@", "-before", "+after",
    ].join("\n");
    const view = (searching = false) => <TranscriptSearchContext.Provider value={searching}>
      <InlineDiff patch={patch} contextLines={0} />
    </TranscriptSearchContext.Provider>;
    const rendered = render(view());
    fireEvent.click(screen.getByRole("button", { name: "展开 2 行未修改内容" }));
    fireEvent.click(screen.getByRole("button", { name: "文件：one.ts" }));
    expect(screen.queryByText("stable")).toBeNull();
    expect(screen.getByText("after")).toBeTruthy();
    vi.mocked(useColorizedLines).mockClear();
    rendered.rerender(view());
    expect(vi.mocked(useColorizedLines).mock.calls.every(([, language]) => language === "python")).toBe(true);
    rendered.rerender(view(true));
    expect(screen.getByText("stable")).toBeTruthy();
    rendered.rerender(view());
    expect(screen.getByRole("button", { name: "文件：one.ts" }).getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "文件：one.ts" }));
    expect(screen.getByText("stable")).toBeTruthy();
    expect(screen.getByRole("button", { name: "收起 2 行未修改内容" })).toBeTruthy();
  });

  it("shows header-shaped changed lines and keeps newline markers out of line numbering", () => {
    const { container } = render(<InlineDiff patch={[
      "--- a/sample.txt", "+++ b/sample.txt", "@@ -1,2 +1,2 @@",
      "--- before", "+++ after", " unchanged", "\\ No newline at end of file", "",
    ].join("\n")} />);
    expect(container.querySelector(".inline-diff-line-removed .inline-diff-text")?.textContent).toBe("-- before");
    expect(container.querySelector(".inline-diff-line-added .inline-diff-text")?.textContent).toBe("++ after");
    expect(container.querySelector(".inline-diff-line-marker .inline-diff-number")?.textContent).toBe("");
    expect(container.querySelectorAll(".inline-diff-line-context")).toHaveLength(1);
  });

  it("keeps one visible line-number column using each concatenated hunk's coordinates", () => {
    const { container } = render(
      <InlineDiff
        patch={[
          "diff --git a/file.ts b/file.ts",
          "@@ -10 +10 @@",
          "-old one",
          "+new one",
          "@@ -10 +10 @@",
          "-old two",
          "+new two",
        ].join("\n")}
      />,
    );

    const numbers = [...container.querySelectorAll<HTMLElement>(".inline-diff-number")]
      .map((node) => Number(node.textContent))
      .filter((value) => Number.isFinite(value));
    expect(numbers).toEqual([10, 10, 10, 10]);
    expect([...container.querySelectorAll(".inline-diff-line")].every(row => row.querySelectorAll(".inline-diff-number").length === 1)).toBe(true);
  });

  it("expands each carried context segment independently using its real coordinates", () => {
    const { container } = render(<InlineDiff patch={[
      "--- a/app.ts", "+++ b/app.ts", "@@ -10,8 +10,8 @@",
      " const first = 1;", " const second = 2;", " const third = 3;",
      "-const answer = 0;", "+const answer = 4;",
      " const fifth = 5;", " const sixth = 6;", " const seventh = 7;", " const eighth = 8;",
    ].join("\n")} contextLines={0} />);
    const beginning = screen.getByRole("button", { name: "展开 3 行未修改内容" });
    const ending = screen.getByRole("button", { name: "展开 4 行未修改内容" });
    expect(container.querySelectorAll(".inline-diff-line-context")).toHaveLength(0);
    fireEvent.click(beginning);
    expect(ending.getAttribute("aria-expanded")).toBe("false");
    expect([...container.querySelectorAll(".inline-diff-line-context .inline-diff-number")].map(node => node.textContent)).toEqual(["10", "11", "12"]);
    expect(screen.getByText("const first = 1;").closest(".inline-diff-text")?.querySelector(".mock-syntax-token")).toBeTruthy();
    fireEvent.click(ending);
    expect([...container.querySelectorAll(".inline-diff-line-context .inline-diff-number")].map(node => node.textContent)).toEqual(["10", "11", "12", "14", "15", "16", "17"]);
    fireEvent.click(beginning);
    expect(container.querySelectorAll(".inline-diff-line-context")).toHaveLength(4);
    expect(screen.queryByText("const first = 1;")).toBeNull();
  });

  it("keeps requested adjacent context and does not count newline markers as unchanged source", () => {
    const { container } = render(<InlineDiff patch={[
      "@@ -1,7 +1,7 @@", " a", " b", " c", "-old", "+new", " d", " e", " f", "\\ No newline at end of file",
    ].join("\n")} contextLines={1} />);
    expect(screen.getAllByRole("button", { name: "展开 2 行未修改内容" })).toHaveLength(2);
    expect([...container.querySelectorAll(".inline-diff-line-context .inline-diff-text")].map(node => node.textContent)).toEqual(["c", "d"]);
    expect(container.querySelector(".inline-diff-line-marker .inline-diff-number")?.textContent).toBe("");
  });

  it("shows only coordinate-proven gaps without pretending absent patch bytes are expandable", () => {
    render(<InlineDiff patch={[
      "@@ -36,2 +36,2 @@", " unchanged", "-old", "+new",
      "@@ -41 +41 @@", "-later old", "+later new",
    ].join("\n")} />);
    expect(screen.getByText("35 行未修改").className).toBe("inline-diff-context-gap");
    expect(screen.getByText("3 行未修改").className).toBe("inline-diff-context-gap");
    expect(screen.queryByRole("button", { name: /行未修改内容/ })).toBeNull();
  });

  it("colors changes and retained context with each actual file's language", () => {
    const { container } = render(<InlineDiff patch={[
      "diff --git a/app.ts b/app.ts", "--- a/app.ts", "+++ b/app.ts", "@@ -1,2 +1,2 @@", " const stable = true;", "-const value = 0;", "+const value = 1;",
      "diff --git a/task.py b/task.py", "--- a/task.py", "+++ b/task.py", "@@ -5 +5 @@", "-value = 0", "+value = 1",
    ].join("\n")} />);
    const files = container.querySelectorAll(".inline-diff-file");
    expect(files).toHaveLength(2);
    expect(screen.getByText("文件：app.ts")).toBeTruthy();
    expect(screen.getByText("文件：task.py")).toBeTruthy();
    expect(vi.mocked(useColorizedLines).mock.calls.map(([, language]) => language)).toEqual(["typescript", "python"]);
    expect(files[0].querySelectorAll(".mock-syntax-token")).toHaveLength(3);
    expect(files[1].querySelector(".inline-diff-line-added .inline-diff-number")?.textContent).toBe("5");
    expect(files[1].querySelector(".inline-diff-line-added .inline-diff-text")?.textContent).toBe("value = 1");
  });

  it("uses the owning file path to colorize a hunk-only tool patch", () => {
    render(<InlineDiff patch={"@@ -1 +1 @@\n-value = 0\n+value = 1"} filePath="src/app.py" />);
    expect(vi.mocked(useColorizedLines)).toHaveBeenCalledWith(expect.any(Array), "python");
  });

  it("retains an explicitly expanded segment when its virtual transcript row remounts", () => {
    const preferences = new Map<string, unknown>();
    const view = <TranscriptReadingContext.Provider value={preferences}>
      <InlineDiff patch={"@@ -1,3 +1,3 @@\n first\n second\n-old\n+new"} contextLines={0} readingKey="edit-1" />
    </TranscriptReadingContext.Provider>;
    const first = render(view);
    fireEvent.click(screen.getByRole("button", { name: "展开 2 行未修改内容" }));
    first.unmount();
    render(view);
    expect(screen.getByRole("button", { name: "收起 2 行未修改内容" }).getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("first")).toBeTruthy();
  });

  it("combines changed words with existing syntax tokens without changing source text", () => {
    const { container } = render(<InlineDiff patch={"@@ -10 +20 @@\n-const answer = old;\n+const answer = new;"} filePath="answer.ts" />);
    const before = container.querySelector(".inline-diff-line-removed .inline-diff-text")!;
    const after = container.querySelector(".inline-diff-line-added .inline-diff-text")!;
    expect(before.textContent).toBe("const answer = old;");
    expect(after.textContent).toBe("const answer = new;");
    expect([...before.querySelectorAll("[data-diff-span]")].map(node => node.textContent)).toEqual(["old"]);
    expect([...after.querySelectorAll("[data-diff-span]")].map(node => node.textContent)).toEqual(["new"]);
    expect(before.querySelector(".mock-syntax-token")).toBeTruthy();
    expect(after.querySelector(".mock-syntax-token")).toBeTruthy();
  });

  it("keeps pure changes in separate files from becoming a fictional paired word diff", () => {
    const { container } = render(<InlineDiff patch={[
      "diff --git a/old.ts b/old.ts", "--- a/old.ts", "+++ /dev/null", "@@ -1 +0,0 @@", "-const old = 1;",
      "diff --git a/new.ts b/new.ts", "--- /dev/null", "+++ b/new.ts", "@@ -0,0 +1 @@", "+const new = 2;",
    ].join("\n")} />);
    expect(container.querySelectorAll(".inline-diff-file")).toHaveLength(2);
    expect(container.querySelectorAll("[data-diff-span]")).toHaveLength(0);
  });

  it("uses escaped text and word decorations while syntax is pending", () => {
    vi.mocked(useColorizedLines).mockReturnValueOnce(null);
    const { container } = render(<InlineDiff patch={'@@ -1 +1 @@\n-<img title="old">\n+<img title="new">'} filePath="snippet.html" />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector(".inline-diff-line-added .inline-diff-text")?.textContent).toBe('<img title="new">');
    expect([...container.querySelectorAll("[data-diff-span]")].map(node => node.textContent)).toEqual(["old", "new"]);
  });

  it("requests plain rendering and omits word spans above 2000 changed rows", () => {
    const patch = ["@@ -1,1000 +1,1001 @@", ...Array.from({ length: 1000 }, () => "-old"), ...Array.from({ length: 1001 }, () => "+new")].join("\n");
    const { container } = render(<InlineDiff patch={patch} filePath="large.ts" />);
    expect(vi.mocked(useColorizedLines)).toHaveBeenLastCalledWith([], "typescript");
    expect(container.querySelectorAll(".inline-diff-line")).toHaveLength(2001);
    expect(container.querySelectorAll("[data-diff-span]")).toHaveLength(0);
    expect(container.querySelector(".mock-syntax-token")).toBeNull();
    expect(container.querySelector(".inline-diff-text")?.textContent).toBe("old");
  });

  it("distinguishes an actual plaintext language from a temporarily pending highlighter", () => {
    vi.mocked(useColorizedLines).mockReturnValueOnce(null);
    const patch = ["@@ -1,1001 +1,1001 @@", ...Array.from({ length: 1000 }, () => " stable"), "-old", "+new"].join("\n");
    const { container } = render(<InlineDiff patch={patch} filePath="plain.txt" />);
    expect([...container.querySelectorAll("[data-diff-span]")].map(node => node.textContent)).toEqual(["old", "new"]);
  });
});
