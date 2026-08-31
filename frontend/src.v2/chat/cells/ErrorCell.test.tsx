/* @vitest-environment jsdom */

import { cleanup, render, screen } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it } from "vitest";
import { ErrorCell } from "./ErrorCell";
import type { ErrorCellState } from "./cellTypes";

afterEach(() => {
  cleanup();
});

const base = (overrides: Partial<ErrorCellState> = {}): ErrorCellState => ({
  kind: "error",
  id: "err-1",
  title: "Something failed",
  message: "",
  recoverable: true,
  ...overrides,
});

describe("ErrorCell", () => {
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

  it("only shows the fatal badge for non-recoverable errors", () => {
    const recoverable = render(<ErrorCell cell={base({ recoverable: true })} />);
    expect(screen.queryByText("不可恢复")).toBeNull();
    recoverable.unmount();

    render(<ErrorCell cell={base({ recoverable: false })} />);
    expect(screen.getByText("不可恢复")).toBeTruthy();
  });
});
