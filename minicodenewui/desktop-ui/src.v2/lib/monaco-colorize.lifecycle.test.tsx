// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { useColorizedLines } from "./monaco-colorize";
import { useAppStore } from "../stores";
import { defaultThemePreferences } from "./appearance-themes";
const themeTokens = readFileSync("src.v2/styles/tokens.css", "utf8");

const native = vi.hoisted(() => ({ colorize: vi.fn(), defineTheme: vi.fn(), setTheme: vi.fn() }));
vi.mock("../panels/monacoRuntime", () => ({ editor: native }));
vi.mock("monaco-editor/languages/definitions/typescript/register.js", () => ({}));
vi.mock("../panels/monacoTheme", () => ({
  defineMiniCodeMonacoTheme: native.defineTheme,
  miniCodeMonacoThemeName: (mode: string) => `minicode-${mode}`,
}));

const themeStyle = document.createElement("style");
beforeAll(() => { themeStyle.textContent = themeTokens; document.head.append(themeStyle); });
afterAll(() => themeStyle.remove());
afterEach(() => {
  cleanup(); native.colorize.mockReset(); native.defineTheme.mockReset(); native.setTheme.mockClear();
  useAppStore.setState({ resolvedTheme: "dark", appearanceColors: { light: null, dark: null }, appearanceThemes: { light: null, dark: null } });
});
const lines = (text: string) => [{ kind: "context", text }];

it("maps escaped worker markup to diff rows and recolors with the selected TextMate theme", async () => {
  const selection = { ...defaultThemePreferences("dark"), codeThemeId: "github" as const };
  useAppStore.setState({ resolvedTheme: "dark", appearanceThemes: { light: null, dark: selection } });
  const input = [{ kind: "hunk", text: "@@ -1 +1 @@" },
    { kind: "del", text: "const old = 1;" }, { kind: "add", text: 'const value = "<img src=x onerror=evil()>";' }];
  const { result } = renderHook(() => useColorizedLines(input, "typescript"));
  await waitFor(() => expect(result.current?.[1]).toContain("#FF7B72"));
  expect(result.current?.[0]).toBe("");
  const code = document.createElement("code");
  code.innerHTML = result.current![2];
  expect(code.textContent).toBe(input[2].text);
  expect(code.querySelector("img")).toBeNull();
  act(() => useAppStore.setState({ appearanceThemes: { light: null, dark: { ...selection, codeThemeId: "dracula" } } }));
  await waitFor(() => expect(result.current?.[1]).toContain("#FF79C6"));
  expect(native.colorize).not.toHaveBeenCalled();
});

describe("native colorization result ownership", () => {
  it("does not switch every editor back to an obsolete mode when a pending theme definition completes", async () => {
    useAppStore.setState({ resolvedTheme: "dark" });
    let finish!: () => void;
    native.defineTheme.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
    native.colorize.mockResolvedValue('<span>LIGHT</span>');
    const input = lines("const value = 1;");
    renderHook(() => useColorizedLines(input, "typescript"));
    await waitFor(() => expect(native.defineTheme).toHaveBeenCalledTimes(1));
    act(() => useAppStore.setState({ resolvedTheme: "light" }));
    await waitFor(() => expect(native.setTheme).toHaveBeenCalledWith("minicode-light"));
    await act(async () => finish());
    expect(native.setTheme.mock.calls).toEqual([["minicode-light"]]);
  });
  it("recolors an existing diff after a same-mode chrome change and does not publish the older pending palette", async () => {
    let first!: (html: string) => void;
    let second!: (html: string) => void;
    native.colorize.mockImplementationOnce(() => new Promise(resolve => { first = resolve; }))
      .mockImplementationOnce(() => new Promise(resolve => { second = resolve; }));
    const input = lines("const value = 1;");
    const { result } = renderHook(() => useColorizedLines(input, "typescript"));
    await waitFor(() => expect(native.colorize).toHaveBeenCalledTimes(1));
    act(() => useAppStore.setState({ appearanceColors: { light: null, dark: { accent: "#334455", surface: "#101010", ink: "#eeeeee" } } }));
    await waitFor(() => expect(native.colorize).toHaveBeenCalledTimes(2));
    await act(async () => second('<span style="color: #eeeeee">CURRENT</span>'));
    await act(async () => first('<span style="color: #aaaaaa">STALE</span>'));
    expect(result.current).toEqual(['<span style="color: #eeeeee">CURRENT</span>']);
  });
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
