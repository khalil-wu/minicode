/* @vitest-environment jsdom */
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
const highlight = vi.hoisted(() => vi.fn());
vi.mock("./CodeThemeHighlighter", () => ({ default: ({ children }: { children: string }) => {
  highlight(children);
  return <pre data-highlighted="true">{children}</pre>;
} }));
import { MarkdownRenderer } from "./MarkdownRenderer";

afterEach(() => { cleanup(); highlight.mockClear(); });

it("streams nested code as plain text and highlights once after settlement", async () => {
  await import("./CodeThemeHighlighter");
  let source = "1. Implementation\n\n   ```typescript\n   const first = 1;\n";
  const view = render(<MarkdownRenderer content={source} isStreaming />);
  await act(async () => {});
  for (let i = 0; i < 20; i++) {
    source += `   const item${i} = ${i};\n`;
    await act(async () => view.rerender(<MarkdownRenderer content={source} isStreaming />));
  }
  expect(view.container.querySelectorAll("li")).toHaveLength(1);
  expect(view.container.textContent).toContain("const item19 = 19;");
  expect(highlight).not.toHaveBeenCalled();
  await act(async () => view.rerender(<MarkdownRenderer content={source + "   ```\n"} isStreaming={false} />));
  expect(highlight).toHaveBeenCalledTimes(1);
  expect(view.container.querySelector("li pre[data-highlighted]")?.textContent).toContain("const item19 = 19;");
});
