/* @vitest-environment jsdom */
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { emptyAppearanceThemes, defaultThemePreferences } from "../../lib/appearance-themes";
import { useAppStore } from "../../stores";
import CodeThemeHighlighter from "./CodeThemeHighlighter";
import { MarkdownRenderer } from "./MarkdownRenderer";

afterEach(() => { cleanup(); useAppStore.setState({ appearanceThemes: emptyAppearanceThemes() }); });
it("keeps the fence header, typography and line-number gutter steady when streaming settles", async () => {
  const code = "const value = 1;";
  const view = render(<MarkdownRenderer content={`\`\`\`javascript\n${code}\n`} isStreaming />);
  const styles = () => [view.container.querySelector("pre")?.getAttribute("style"),
    view.container.querySelector(".code-theme-line-number")?.getAttribute("class")];
  expect(view.container.querySelector(".code-block-wrapper")?.textContent).toContain("javascript");
  const before = styles();
  view.rerender(<MarkdownRenderer content={`\`\`\`javascript\n${code}\n\`\`\``} isStreaming={false} />);
  await waitFor(() => expect(view.container.querySelector("pre code")?.textContent).toContain(code));
  expect(styles()).toEqual(before);
  expect(view.container.querySelectorAll(".code-theme-line-number")).toHaveLength(1);
});

it.each(["\n```\n", "\n```", ""])("retains measured fence nodes when the stream finishes with %j", async closing => {
  const code = Array.from({ length: 130 }, (_, index) => `const value${index} = ${index};`).join("\n");
  const source = `Introduction\n\n\`\`\`typescript\n${code}`;
  const view = render(<MarkdownRenderer content={source} isStreaming />);
  const frame = view.container.querySelector(".code-block-wrapper");
  const pre = view.container.querySelector("pre");
  const groups = [...view.container.querySelectorAll<HTMLElement>(".code-line-group")];
  groups[0].dataset.measured = "true";
  groups[0].style.setProperty("--markdown-block-height", "1408px");
  view.rerender(<MarkdownRenderer content={source + closing} isStreaming={false} />);
  expect(view.container.querySelector(".code-block-wrapper")).toBe(frame);
  expect(view.container.querySelector("pre")).toBe(pre);
  expect([...view.container.querySelectorAll(".code-line-group")]).toEqual(groups);
  await waitFor(() => expect([...view.container.querySelectorAll("code span[style]")].some(span => span.textContent?.trim() === "const")).toBe(true));
  expect(groups[0].style.getPropertyValue("--markdown-block-height")).toBe("1408px");
  expect(view.container.querySelector(".code-block-wrapper")?.getAttribute("data-markdown-copy-text")).toBe(code);
  expect(view.container.querySelectorAll(".code-theme-line-number")).toHaveLength(130);
  if (closing) {
    view.rerender(<MarkdownRenderer content={`${source}${closing}\n\nMore text follows.`} isStreaming={false} />);
    expect(view.container.querySelector("pre")).toBe(pre);
  }
});

it("updates an already rendered fence with actual TextMate colors in the same mode while retaining typography and source", async () => {
  const themes = { ...emptyAppearanceThemes(), dark: { codeThemeId: "github" as const, ...defaultThemePreferences("dark") } };
  useAppStore.setState({ appearanceThemes: themes });
  const code = '// 注释\nconst answer = "<Hello>";';
  const view = render(<CodeThemeHighlighter hasLanguage language="javascript" resolvedTheme="dark">{code}</CodeThemeHighlighter>);
  const token = () => Array.from(view.container.querySelectorAll('code span[style]')).find(span => span.textContent?.trim() === "const") as HTMLElement;
  await waitFor(() => expect(token()?.style.color).toBe("rgb(255, 123, 114)"));
  act(() => useAppStore.setState({ appearanceThemes: { ...themes, dark: { ...themes.dark, codeThemeId: "dracula" } } }));
  await waitFor(() => expect(token()?.style.color).toBe("rgb(255, 121, 198)"));
  expect(view.container.querySelector("pre")!.style.fontFamily).toBe("var(--editor-font-family)");
  expect(view.container.querySelector("pre")!.style.fontSize).toBe("var(--editor-font-size)");
  expect(view.container.querySelectorAll(".code-theme-line-number")).toHaveLength(2);
  expect(view.container.textContent).toContain('const answer = "<Hello>";');
  expect(view.container.querySelector("Hello")).toBeNull();
});
