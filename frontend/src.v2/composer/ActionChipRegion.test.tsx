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

describe("ContextChipRegion", () => {
  afterEach(() => cleanup());

  it("uses separate native actions to open and remove a selected file", () => {
    useAppStore.setState({
      actionChip: null,
      mentionResults: [],
      selectedMentions: [{ name: "app.tsx", path: "src/app.tsx", kind: "file" }],
    });

    render(<ContextChipRegion />);

    expect(screen.getByRole("button", { name: "打开 app.tsx" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "从上下文中移除 app.tsx" }));
    expect(useAppStore.getState().selectedMentions).toEqual([]);
  });
});
