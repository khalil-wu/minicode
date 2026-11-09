/* @vitest-environment jsdom */
import { afterEach, describe, expect, it } from "vitest";
import sourceCases from "../../test/fixtures/codex-markdown-clipboard.json";
import { readMarkdownClipboard, writeMarkdownSelection } from "./markdown-clipboard";

afterEach(() => { document.body.replaceChildren(); document.getSelection()?.removeAllRanges(); });

describe("actual Codex semantic clipboard fixtures", () => {
  for (const { name, html, expected } of sourceCases.fixtures) it(name, () => {
    const root = document.createElement("div"); root.innerHTML = html; document.body.append(root);
    const before = root.innerHTML;
    expect(readMarkdownClipboard(root)).toEqual(expected === null ? null : { "text/plain": expected.plainText, "text/html": expected.htmlText });
    expect(root.innerHTML).toBe(before);
  });

  it("copies the actual selection and expands a partially selected math expression", () => {
    const root = document.createElement("div");
    root.innerHTML = '<p>Prefix <span class="katex"><annotation encoding="application/x-tex">x^2</annotation><span>rendered</span></span> suffix</p>';
    document.body.append(root);
    const text = root.querySelector(".katex span")!.firstChild!;
    const range = document.createRange(); range.setStart(text, 1); range.setEnd(text, 3);
    document.getSelection()!.addRange(range);
    const result = new Map<string, string>();
    expect(writeMarkdownSelection(root, { setData: (type: string, value: string) => result.set(type, value) } as DataTransfer)).toBe(true);
    expect(result.get("text/plain")).toBe("\\(x^2\\)");
    expect(result.get("text/html")).toContain('data-math-source="x^2"');
    expect(root.textContent).toContain("rendered");
  });

  it("leaves text editing and selections outside the answer to their actual owner", () => {
    const root = document.createElement("div"); root.innerHTML = '<div contenteditable="true">draft</div>';
    document.body.append(root);
    const range = document.createRange(); range.selectNodeContents(root.firstChild!);
    document.getSelection()!.addRange(range);
    const result = new Map<string, string>();
    const data = { setData: (type: string, value: string) => result.set(type, value) } as DataTransfer;
    expect(writeMarkdownSelection(root, data)).toBe(false);
    document.getSelection()!.removeAllRanges();
    const elsewhere = document.createElement("p"); elsewhere.textContent = "other answer"; document.body.append(elsewhere);
    range.selectNodeContents(elsewhere); document.getSelection()!.addRange(range);
    expect(writeMarkdownSelection(root, data)).toBe(false);
    expect(result.size).toBe(0);
  });
});
