/* @vitest-environment jsdom */

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { InlineDiff } from "./InlineDiff";

describe("InlineDiff", () => {
  it("shows header-shaped changed lines and keeps newline markers out of line numbering", () => {
    const { container } = render(<InlineDiff patch={[
      "--- a/sample.txt", "+++ b/sample.txt", "@@ -1,2 +1,2 @@",
      "--- before", "+++ after", " unchanged", "\\ No newline at end of file", "",
    ].join("\n")} />);
    expect(container.querySelector(".inline-diff-line-removed .inline-diff-text")?.textContent).toBe("-- before");
    expect(container.querySelector(".inline-diff-line-added .inline-diff-text")?.textContent).toBe("++ after");
    expect(container.querySelector(".inline-diff-line-marker .inline-diff-number")?.textContent).toBe("");
    expect(container.querySelectorAll(".inline-diff-line-context")).toHaveLength(1);
  });

  it("keeps one visible line-number column ordered across concatenated patches", () => {
    const { container } = render(
      <InlineDiff
        patch={[
          "diff --git a/file.ts b/file.ts",
          "@@ -10 +10 @@",
          "-old one",
          "+new one",
          "@@ -10 +10 @@",
          "-old two",
          "+new two",
        ].join("\n")}
      />,
    );

    const numbers = [...container.querySelectorAll<HTMLElement>(".inline-diff-number")]
      .map((node) => Number(node.textContent))
      .filter((value) => Number.isFinite(value));
    expect(numbers).toEqual([10, 10, 11, 11]);
    expect(container.querySelectorAll(".inline-diff-line:first-child .inline-diff-number")).toHaveLength(1);
  });
});
