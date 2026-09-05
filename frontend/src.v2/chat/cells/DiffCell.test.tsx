/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../../stores";
import { DiffCell } from "./DiffCell";
import type { DiffCellState } from "./cellTypes";

const cell: DiffCellState = {
  kind: "diff",
  id: "diff-all",
  status: "updated",
  files: [
    { path: "src/a.ts", patch: "diff --git a/src/a.ts b/src/a.ts\n@@ -1 +1 @@\n-oldA\n+newA", additions: 1, deletions: 1 },
    { path: "src/b.ts", patch: "diff --git a/src/b.ts b/src/b.ts\n@@ -4,0 +5 @@\n+newB", additions: 1, deletions: 0, changeType: "created" },
  ],
  summary: { added: 2, deleted: 1, modifiedFiles: 2 },
  collapsed: false,
  createdAt: 1,
};

describe("DiffCell", () => {
  beforeEach(() => useAppStore.setState({ workingDirectory: "", activeEditorPath: null }));
  afterEach(cleanup);

  it("renders a static final card with review and revert actions", () => {
    const { container } = render(<DiffCell cell={cell} />);
    expect(screen.getByText("已编辑 2 个文件")).toBeTruthy();
    expect(container.querySelectorAll(".diff-file-section")).toHaveLength(2);
    expect(screen.queryByText("oldA")).toBeNull();
    expect(screen.queryByText("newA")).toBeNull();
    expect(screen.queryByText("newB")).toBeNull();
    expect(screen.getByRole("button", { name: "审核" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "撤销" })).toBeTruthy();
    expect(container.querySelectorAll(".diff-file-toggle")).toHaveLength(0);
    expect(container.querySelectorAll(".diff-file-section")).toHaveLength(2);
    expect(container.querySelectorAll(".diff-file-section-header")).toHaveLength(2);
    expect(container.querySelectorAll(".diff-file-section-header > .diff-cell-file-path")).toHaveLength(2);
  });

  it("does not inline-expand file patches in the final card", () => {
    render(<DiffCell cell={cell} />);
    expect(screen.getByText("src/a.ts")).toBeTruthy();
    expect(screen.queryByText("oldA")).toBeNull();
    expect(screen.queryByText("newA")).toBeNull();
  });

  it("shows three files by default and can fold the remaining file rows again", () => {
    const files = Array.from({ length: 5 }, (_,index) => ({ ...cell.files[0], path: `src/${index}.ts` }));
    const { container } = render(<DiffCell cell={{ ...cell, files }} />);
    expect(container.querySelectorAll(".diff-file-section")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "再显示 2 个文件" }));
    expect(container.querySelectorAll(".diff-file-section")).toHaveLength(5);
    fireEvent.click(screen.getByRole("button", { name: "收起文件列表" }));
    expect(container.querySelectorAll(".diff-file-section")).toHaveLength(3);
  });

  it("opens the selected file's patch rather than the first file", () => {
    render(<DiffCell cell={cell} />);
    fireEvent.click(screen.getByRole("button", { name: "src/b.ts" }));
    expect(useAppStore.getState().diffReview).toMatchObject({ selectedPath: "src/b.ts", diff: cell.files[1].patch, mode: "view" });
  });

  it("shows rename evidence as one file section", () => {
    render(<DiffCell cell={{ ...cell, files: [{ path: "src/new.ts", oldPath: "src/old.ts", additions: 0, deletions: 0, changeType: "renamed" }], summary: { added: 0, deleted: 0, modifiedFiles: 1 } }} />);
    expect(screen.getByText("old.ts → new.ts")).toBeTruthy();
    expect(screen.queryByText("重命名")).toBeNull();
  });
});
