// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { StatusIcon } from "./icons";

describe("StatusIcon", () => {
  afterEach(() => {
    cleanup();
  });

  it("spins running status by default", () => {
    render(<StatusIcon status="running" />);

    expect(screen.getByTestId("status-icon-running").classList.contains("animate-spin")).toBe(true);
  });

  it("uses a caller-provided spinning class for running status", () => {
    render(<StatusIcon status="running" spinningClassName="custom-spin" />);

    const icon = screen.getByTestId("status-icon-running");
    expect(icon.classList.contains("custom-spin")).toBe(true);
    expect(icon.classList.contains("animate-spin")).toBe(false);
  });
});
