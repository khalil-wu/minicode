/* @vitest-environment jsdom */
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { emptyAppearanceThemes, defaultThemePreferences } from "../../lib/appearance-themes";
import { useAppStore } from "../../stores";
import CodeThemeHighlighter from "./CodeThemeHighlighter";

afterEach(() => { cleanup(); useAppStore.setState({ appearanceThemes: emptyAppearanceThemes() }); });
it("updates an already rendered fence with actual TextMate colors in the same mode while retaining typography and source", async () => {
  const themes = { ...emptyAppearanceThemes(), dark: { codeThemeId: "github" as const, ...defaultThemePreferences("dark") } };
  useAppStore.setState({ appearanceThemes: themes });
  const code = '// 注释\nconst answer = "<Hello>";';
  const view = render(<CodeThemeHighlighter hasLanguage language="javascript" resolvedTheme="dark">{code}</CodeThemeHighlighter>);
  const token = () => Array.from(view.container.querySelectorAll('code span[style]')).find(span => span.textContent === "const") as HTMLElement;
  await waitFor(() => expect(token()?.style.color).toBe("rgb(255, 123, 114)"));
  act(() => useAppStore.setState({ appearanceThemes: { ...themes, dark: { ...themes.dark, codeThemeId: "dracula" } } }));
  await waitFor(() => expect(token()?.style.color).toBe("rgb(255, 121, 198)"));
  expect(view.container.querySelector("pre")!.style.fontFamily).toBe("var(--editor-font-family)");
  expect(view.container.querySelector("pre")!.style.fontSize).toBe("var(--editor-font-size)");
  expect(view.container.querySelectorAll(".code-theme-line-number")).toHaveLength(2);
  expect(view.container.textContent).toContain('const answer = "<Hello>";');
  expect(view.container.querySelector("Hello")).toBeNull();
});
