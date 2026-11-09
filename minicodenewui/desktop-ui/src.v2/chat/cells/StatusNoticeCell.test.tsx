/* @vitest-environment jsdom */

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { StatusNoticeCell } from "./StatusNoticeCell";

afterEach(() => cleanup());

describe("StatusNoticeCell", () => {
  it("renders tone-specific vector icons instead of character glyphs", () => {
    const { container } = render(
      <StatusNoticeCell
        cell={{
          kind: "status_notice",
          id: "notice-1",
          title: "Ready",
          message: "The operation completed.",
          tone: "success",
          createdAt: 1,
        }}
      />,
    );

    expect(container.querySelector("svg.lucide")).toBeTruthy();
    expect(container.textContent).not.toContain("✓");
  });
});
