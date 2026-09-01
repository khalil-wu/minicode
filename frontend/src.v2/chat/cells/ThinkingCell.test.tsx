/* @vitest-environment jsdom */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThinkingCell } from "./ThinkingCell";
import type { ThinkingCellState } from "./cellTypes";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    writable: true,
    value: () => ({
      matches: false,
      media: "",
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

afterEach(() => {
  cleanup();
});

const thinkingCell = (patch: Partial<ThinkingCellState>): ThinkingCellState => ({
  kind: "thinking",
  id: "thinking-1",
  content: "我先查一下昨天发生了什么新闻。",
  source: "model_preamble",
  createdAt: 1,
  ...patch,
});

describe("ThinkingCell", () => {
  it.each(["model_preamble", "commentary"] as const)("renders %s narration as plain ordered process text", (source) => {
    render(<ThinkingCell cell={thinkingCell({ source, isStreaming: true })} isStreaming />);
    expect(document.body.textContent).toContain("我先查一下昨天发生了什么新闻。");
    expect(document.querySelector(".thinking-cell-commentary")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("shows provider reasoning immediately while streaming and removes it when settled", () => {
    const { rerender } = render(<ThinkingCell
      cell={thinkingCell({
        source: "provider",
        providerReasoningType: "reasoning_content",
        content: "Provider reasoning preview",
        isStreaming: true,
      })}
      isStreaming
    />);

    expect(document.body.textContent).toContain("Provider reasoning preview");
    expect(screen.queryByRole("button")).toBeNull();
    expect(document.querySelector(".thinking-cell-live")).toBeTruthy();

    rerender(<ThinkingCell
      cell={thinkingCell({
        source: "provider",
        providerReasoningType: "reasoning_content",
        content: "Provider reasoning preview",
        isStreaming: false,
      })}
    />);

    expect(document.body.textContent).not.toContain("Provider reasoning preview");
    expect(document.querySelector(".thinking-cell-live")).toBeNull();
  });

  it("does not retain completed provider reasoning", () => {
    render(<ThinkingCell cell={thinkingCell({
      source: "provider",
      providerReasoningType: "reasoning_content",
      content: "Provider reasoning preview",
    })} />);
    expect(document.body.textContent).toBe("");
  });

  it("preserves a provider reasoning summary after streaming settles", () => {
    const { rerender } = render(<ThinkingCell cell={thinkingCell({
      source: "provider",
      providerReasoningType: "reasoning_summary_text",
      content: "**Fetching Beijing's Weather**\n\nChecking current conditions.",
      isStreaming: true,
    })} isStreaming />);

    expect(document.body.textContent).toContain("Fetching Beijing's Weather");
    expect(document.body.textContent).toContain("Checking current conditions.");
    expect(document.querySelector("strong")).toBeTruthy();
    expect(document.querySelector(".thinking-cell-summary")).toBeTruthy();

    rerender(<ThinkingCell cell={thinkingCell({
      source: "provider",
      providerReasoningType: "reasoning_summary_text",
      content: "**Fetching Beijing's Weather**\n\nChecking current conditions.",
      isStreaming: false,
    })} />);

    expect(document.body.textContent).toContain("Fetching Beijing's Weather");
    expect(document.body.textContent).toContain("Checking current conditions.");
    expect(document.querySelector(".thinking-cell-summary")).toBeTruthy();
  });
});
