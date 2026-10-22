/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ErrorCell } from "./ErrorCell";
import type { ErrorCellState } from "./cellTypes";

const links = vi.hoisted(() => ({ open: vi.fn() }));
vi.mock("../../lib/open-preferred-target", () => ({ openPreferredWebLink: links.open }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const base = (overrides: Partial<ErrorCellState> = {}): ErrorCellState => ({
  kind: "error",
  id: "err-1",
  title: "Something failed",
  message: "",
  recoverable: true,
  createdAt: 1,
  ...overrides,
});

describe("ErrorCell", () => {
  it("renders actual terminal model failures as the Source neutral standalone notice", () => {
    const view = render(<ErrorCell cell={base({ source: "agent", title: "请求失败", message: "The server is currently busy." })} />);
    expect(screen.getByRole("alert").className).toContain("error-cell-notice");
    expect(view.container.querySelector(".error-cell-danger")).toBeNull();
    expect(view.container.querySelector(".error-cell-header")).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain("The server is currently busy.");
  });

  it("links bare HTTP(S) tokens while preserving the exact notice text, punctuation and inline layout", () => {
    const message = "See HTTPS://example.com/help?a=1&b=2 and (http://localhost:8080/details).";
    const view = render(<ErrorCell cell={base({ source: "agent", message })} conversationId="original" />);
    const anchors = screen.getAllByRole("link");
    expect(anchors.map(anchor => anchor.getAttribute("href"))).toEqual([
      "HTTPS://example.com/help?a=1&b=2", "http://localhost:8080/details",
    ]);
    expect(screen.getByRole("alert").textContent).toBe(`Something failed${message}`);
    expect(anchors.every(anchor => anchor.className.includes("no-underline"))).toBe(true);
    expect(view.container.querySelectorAll('[data-source-icon="system-error-inline"]')).toHaveLength(1);
    expect(view.container.querySelectorAll("svg")).toHaveLength(1);
    expect(view.container.querySelector("p")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("keeps all other inline tokens literal instead of interpreting Markdown, email, images or invalid URLs", () => {
    const message = [
      "[guide](https://example.com/markdown)", "<https://example.com/autolink>",
      "www.example.com", "person@example.com", "ftp://example.com/file", "javascript:alert(1)",
      "https://[invalid]", "`https://example.com/code`", "![image](https://example.com/image.png)",
      "**https://example.com/bold**",
    ].join(" | ");
    const view = render(<ErrorCell cell={base({ source: "network", message })} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByRole("alert").textContent).toBe(`Something failed${message}`);
    expect(view.container.querySelector("img, code, strong")).toBeNull();
  });

  it("links distinct raw diagnostics without changing their whitespace or adding another control", () => {
    const message = "The request ended. See https://example.com/help.";
    const rawError = "Gateway detail:\n  https://example.com/request?id=abc  \nTrace retained.";
    const view = render(<ErrorCell cell={base({ source: "agent", message, rawError })} />);
    expect(screen.getAllByRole("link").map(anchor => anchor.textContent)).toEqual([
      "https://example.com/help", "https://example.com/request?id=abc",
    ]);
    expect(screen.getByRole("alert").textContent).toBe(`Something failed${message}${rawError}`);
    expect(view.container.querySelectorAll("br")).toHaveLength(1);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it.each([
    { gesture: "ordinary", ctrlKey: false, metaKey: false, altKey: false, shiftKey: false },
    { gesture: "Control", ctrlKey: true, metaKey: false, altKey: false, shiftKey: false },
    { gesture: "Command", ctrlKey: false, metaKey: true, altKey: false, shiftKey: false },
    { gesture: "Option and Shift", ctrlKey: false, metaKey: false, altKey: true, shiftKey: true },
  ])("uses the existing web target consumer with the rendered owner and $gesture modifiers", ({ gesture: _gesture, ...modifiers }) => {
    render(<ErrorCell conversationId="original" cell={base({ source: "agent", message: "See https://example.com/help." })} />);
    expect(fireEvent.click(screen.getByRole("link"), modifiers)).toBe(false);
    expect(links.open).toHaveBeenCalledExactlyOnceWith("https://example.com/help", "original", modifiers);
  });

  it("strips model-facing <tool_use_error> / <error> markup from the message", () => {
    render(
      <ErrorCell
        cell={base({
          message: "<tool_use_error>Error: npm install failed</tool_use_error>",
        })}
      />,
    );
    // getByText throws if not found, so this asserts presence.
    expect(screen.getByText("Error: npm install failed")).toBeTruthy();
    expect(screen.queryByText(/tool_use_error/)).toBeNull();
  });

  it("renders a clean message unchanged", () => {
    render(<ErrorCell cell={base({ message: "File not found" })} />);
    expect(screen.getByText("File not found")).toBeTruthy();
  });

  it("omits the message block when purification leaves it empty", () => {
    const { container } = render(
      <ErrorCell cell={base({ message: "<error></error>" })} />,
    );
    expect(container.querySelector(".error-cell-message")).toBeNull();
  });

  it("uses semantic Lucide icons for permission errors and recovery guidance", () => {
    const { container } = render(
      <ErrorCell
        cell={base({
          source: "permission",
          suggestedAction: "Allow access and retry",
        })}
      />,
    );

    expect(container.querySelector(".lucide-shield-alert")).toBeTruthy();
    expect(container.querySelector(".lucide-lightbulb")).toBeTruthy();
    expect(screen.getByText("Allow access and retry")).toBeTruthy();
  });

  it("keeps the actual error without exposing the internal recoverability flag", () => {
    const recoverable = render(<ErrorCell cell={base({ recoverable: true })} />);
    expect(screen.queryByText("不可恢复")).toBeNull();
    recoverable.unmount();

    render(<ErrorCell cell={base({ recoverable: false })} />);
    expect(screen.queryByText("不可恢复")).toBeNull();
    expect(screen.getByText("Something failed")).toBeTruthy();
  });
});
