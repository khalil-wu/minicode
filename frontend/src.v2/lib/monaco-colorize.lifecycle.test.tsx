// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { useColorizedLines } from "./monaco-colorize";
const themeTokens = readFileSync("src.v2/styles/tokens.css", "utf8");

const native = vi.hoisted(() => ({ colorize: vi.fn(), defineTheme: vi.fn(), setTheme: vi.fn() }));
vi.mock("monaco-editor/editor/editor.api.js", () => ({ editor: native }));
vi.mock("monaco-editor/languages/definitions/typescript/register.js", () => ({}));

const themeStyle = document.createElement("style");
beforeAll(() => { themeStyle.textContent = themeTokens; document.head.append(themeStyle); });
afterAll(() => themeStyle.remove());
afterEach(() => { cleanup(); native.colorize.mockReset(); });
const lines = (text: string) => [{ kind: "context", text }];

describe("native colorization result ownership", () => {
  it.each(["plaintext", "empty"])("does not publish an old native result after the consumer switches to %s", async (target) => {
    let finish!: (html: string) => void;
    native.colorize.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const { result, rerender } = renderHook(({ input, language }) => useColorizedLines(input, language), { initialProps: { input: lines("OLD"), language: "typescript" } });
    await waitFor(() => expect(native.colorize).toHaveBeenCalledOnce());
    rerender({ input: target === "empty" ? [] : lines("NEW"), language: target === "plaintext" ? "plaintext" : "typescript" });
    await act(async () => finish('<span class="mtk1">OLD</span>'));
    expect(result.current).toBeNull();
  });
  it("shows the current source while a newer native result is pending and ignores out-of-order completions", async () => {
    let first!: (html: string) => void;
    let second!: (html: string) => void;
    native.colorize.mockImplementationOnce(() => new Promise((resolve) => { first = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { second = resolve; }));
    const { result, rerender } = renderHook((input) => useColorizedLines(input, "typescript"), { initialProps: lines("OLD") });
    await waitFor(() => expect(native.colorize).toHaveBeenCalledTimes(1));
    rerender(lines("NEW"));
    expect(result.current).toBeNull();
    await waitFor(() => expect(native.colorize).toHaveBeenCalledTimes(2));
    await act(async () => second('<span class="mtk1">NEW</span>'));
    expect(result.current).toEqual(['<span class="mtk1">NEW</span>']);
    await act(async () => first('<span class="mtk1">OLD</span>'));
    expect(result.current).toEqual(['<span class="mtk1">NEW</span>']);
  });
});
