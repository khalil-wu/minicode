// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../../stores";
import { defaultThemePreferences, emptyAppearanceThemes } from "../../lib/appearance-themes";
import { emptyAppearanceColors } from "../../lib/appearance-colors";
import { InlineDiff } from "./InlineDiff";
import { DiffPanel } from "../../panels/DiffPanel";

vi.mock("../../lib/monaco-colorize", () => ({
  guessLanguageFromPath: () => "plaintext", useColorizedLines: () => null, extractFilePathFromDiff: () => "sample.txt",
}));
vi.mock("../../protocol/ws-outbox", () => ({
  sendClientCommand: vi.fn(), sendClientCommandAwaitResult: vi.fn(), sendPromptResponseCommand: vi.fn(), commandResultSucceeded: () => true,
}));
const patch = "--- a/sample.txt\n+++ b/sample.txt\n@@ -1 +1 @@\n-old\n+new\n";
beforeEach(() => {
  useAppStore.setState({ resolvedTheme: "dark", appearanceColors: emptyAppearanceColors(), appearanceThemes: emptyAppearanceThemes(),
    conversationId: "diff-palette", workingDirectory: "C:\\audit", messages: [],
    diffReview: { requestId: "palette-review", conversationId: "diff-palette", workspaceRoot: "C:\\audit", diff: patch,
      files: [{ path: "sample.txt", patch }], selectedPath: "sample.txt", mode: "view", status: "viewing", fileDecisions: {}, lineComments: [] },
  });
});
afterEach(() => { cleanup(); useAppStore.setState({ diffReview: null, appearanceColors: emptyAppearanceColors(), appearanceThemes: emptyAppearanceThemes() }); });

it("applies the chrome surface and native raw-color formulas to both default HTML Diff paths", () => {
  const view = render(<><InlineDiff patch={patch} /><DiffPanel /></>);
  for (const node of view.container.querySelectorAll<HTMLElement>("[data-diff-palette]")) {
    expect(node.dataset.diffPalette).toBe("native");
    expect(node.style.getPropertyValue("--diff-view-background")).toBe("var(--surface-base)");
    expect(node.style.getPropertyValue("--diff-view-foreground")).toBe("var(--editor-foreground)");
  }
  const content = view.container.querySelector<HTMLElement>(".mc-diff-code-content")!;
  expect(content.style.background).toBe("");
  expect(content.style.color).toBe("var(--diff-view-foreground)");
});

it("keeps selected raw foreground and line colors when chrome ink/surface change, without changing source or typography", async () => {
  useAppStore.setState({ appearanceColors: { light: null, dark: { surface: "#345678", ink: "#abcdef", accent: "#2468ac" } },
    appearanceThemes: { light: null, dark: { ...defaultThemePreferences("dark"), codeThemeId: "github",
      semanticColors: { diffAdded: "#40c977", diffRemoved: "#fa423e", skill: "#ad7bf9" } } },
  });
  document.documentElement.style.setProperty("--font-ui", "User Font");
  const view = render(<><InlineDiff patch={patch} /><DiffPanel /></>);
  await waitFor(() => expect([...view.container.querySelectorAll<HTMLElement>("[data-diff-palette]")].every(node => node.dataset.diffPalette === "textmate")).toBe(true));
  const roots = [...view.container.querySelectorAll<HTMLElement>("[data-diff-palette]")];
  expect(roots).toHaveLength(2);
  for (const node of roots) {
    expect(node.style.getPropertyValue("--diff-view-foreground")).toBe("#e6edf3");
    expect(node.style.getPropertyValue("--diff-view-background")).toBe("#345678");
    expect(node.style.getPropertyValue("--diff-added-background")).toBe("#1c3129");
    expect(node.style.getPropertyValue("--diff-added-gutter-background")).toBe("#132017");
    expect(node.style.getPropertyValue("--diff-added-word-background")).toBe("#40c97733");
  }
  act(() => useAppStore.setState({ appearanceColors: { light: null, dark: { surface: "#102030", ink: "#fedcba", accent: "#2468ac" } } }));
  for (const node of roots) {
    expect(node.style.getPropertyValue("--diff-view-background")).toBe("#102030");
    expect(node.style.getPropertyValue("--diff-added-background")).toBe("#1c3129");
    expect(node.style.getPropertyValue("--diff-view-foreground")).toBe("#e6edf3");
    expect(node.textContent).toContain("new");
  }
  expect(document.documentElement.style.getPropertyValue("--font-ui")).toBe("User Font");
  document.documentElement.style.removeProperty("--font-ui");
});
