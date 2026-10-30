/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    value: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
  });
});

import { useAppStore } from "../stores";
import { ContextChipRegion } from "./ActionChipRegion";
import * as contextActions from "../chat/contextReferenceActions";

describe("ContextChipRegion", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("leaves file, folder and plugin references in the rich input without rendering duplicate prefix actions", () => {
    const references = [
      { name: "app.tsx", path: "src/app.tsx", kind: "file" as const },
      { name: "src", path: "src", kind: "folder" as const },
      { name: "Notes", path: "plugin://notes@personal", configName: "notes@personal", kind: "plugin" as const },
    ];
    useAppStore.setState({
      actionChip: null,
      mentionResults: [],
      selectedMentions: references,
    });

    const { container } = render(<ContextChipRegion />);
    expect(container.childNodes).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "打开 app.tsx" })).toBeNull();
    expect(screen.queryByRole("button", { name: "清空上下文引用" })).toBeNull();
    expect(useAppStore.getState().selectedMentions).toEqual(references);
  });

  it("keeps real browser annotations individually openable and removable without clearing inline references", () => {
    const inline = [
      { name: "app.tsx", path: "src/app.tsx", kind: "file" }, { name: "Notes", path: "plugin://notes@personal", configName: "notes@personal", kind: "plugin" },
    ] as const;
    const first = { kind: "browser_annotation" as const, name: "页面 A", path: "annotation://a", url: "https://example.test/a", note: "真实批注 A" };
    const second = { kind: "browser_annotation" as const, name: "页面 B", path: "annotation://b", url: "https://example.test/b", note: "真实批注 B" };
    const open = vi.spyOn(contextActions, "openContextReference").mockImplementation(() => {});
    useAppStore.setState({ actionChip: null, mentionResults: [], selectedMentions: [...inline, first, second] });
    render(<ContextChipRegion />);
    expect(screen.queryByRole("button", { name: "清空上下文引用" })).toBeNull();
    expect(screen.queryByRole("button", { name: "从上下文中移除 app.tsx" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "@页面 A" }));
    expect(open).toHaveBeenCalledWith(first);
    fireEvent.click(screen.getByRole("button", { name: "从上下文中移除 页面 A" }));
    expect(useAppStore.getState().selectedMentions).toEqual([...inline, second]);
    fireEvent.click(screen.getByRole("button", { name: "从上下文中移除 页面 B" }));
    expect(useAppStore.getState().selectedMentions).toEqual([...inline]);
  });
});
