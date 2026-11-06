/* @vitest-environment jsdom */
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { emptyAppearanceThemes, defaultThemePreferences } from "../../lib/appearance-themes";
import { useAppStore } from "../../stores";
import * as worker from "../../lib/code-highlighting-worker-client";
import CodeThemeHighlighter from "./CodeThemeHighlighter";

afterEach(() => { cleanup(); vi.restoreAllMocks(); useAppStore.setState({ appearanceThemes: emptyAppearanceThemes() }); });

it("keeps matching token nodes until the newest palette is ready, ignoring an older completion", async () => {
  const pending: Array<(lines: string[]) => void> = [];
  vi.spyOn(worker, "highlightCodeInWorker").mockImplementation(() => new Promise(resolve => pending.push(resolve)));
  const themes = { ...emptyAppearanceThemes(), dark: { ...defaultThemePreferences("dark"), codeThemeId: "codex" as const } };
  useAppStore.setState({ appearanceThemes: themes });
  const code = "const value = 1;";
  const view = render(<CodeThemeHighlighter hasLanguage language="javascript" resolvedTheme="dark">{code}</CodeThemeHighlighter>);
  const token = () => view.container.querySelector<HTMLElement>("code .block > span[style]");
  await act(async () => pending[0](['<span style="color: #123456">const value = 1;</span>']));
  const original = token();
  act(() => useAppStore.setState({ appearanceThemes: { ...themes, dark: { ...themes.dark, codeThemeId: "github" } } }));
  expect(token()).toBe(original);
  act(() => useAppStore.setState({ appearanceThemes: { ...themes, dark: { ...themes.dark, codeThemeId: "dracula" } } }));
  await act(async () => pending[1](['<span style="color: red">const value = 1;</span>']));
  expect(token()).toBe(original);
  await act(async () => pending[2](['<span style="color: #abcdef">const value = 1;</span>']));
  expect(token()?.style.color).toBe("rgb(171, 205, 239)");
  expect(view.container.querySelector("pre")?.getAttribute("data-markdown-copy-text")).toBe(code);
});

it.each(["source", "language", "mode", "stream"])("discards incompatible coloring when %s changes while preserving current source", async change => {
  const pending: Array<(lines: string[]) => void> = [];
  vi.spyOn(worker, "highlightCodeInWorker").mockImplementation(() => new Promise(resolve => pending.push(resolve)));
  const view = render(<CodeThemeHighlighter hasLanguage language="javascript" resolvedTheme="dark">const old = 1;</CodeThemeHighlighter>);
  await act(async () => pending[0](['<span style="color: #123456">const old = 1;</span>']));
  const source = change === "source" ? "const current = 2;" : "const old = 1;";
  view.rerender(<CodeThemeHighlighter hasLanguage language={change === "language" ? "text" : "javascript"}
    resolvedTheme={change === "mode" ? "light" : "dark"} isStreaming={change === "stream"}>{source}</CodeThemeHighlighter>);
  expect(view.container.querySelector("code .block > span[style]")).toBeNull();
  expect(view.container.querySelector("pre")?.getAttribute("data-markdown-copy-text")).toBe(source);
  expect(view.container.textContent).toContain(source);
});
