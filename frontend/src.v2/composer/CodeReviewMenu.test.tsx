/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ options: vi.fn() }));
vi.mock("../protocol/code-review", () => ({ fetchCodeReviewOptions: mocks.options }));
import { CodeReviewMenu } from "./CodeReviewMenu";
import type { CodeReviewOptions } from "../protocol/code-review-types";

const owner = { conversationId: "review-owner", cwd: "file:///C:/real-owner", input: "/review", selectedCommand: "review" };
const options: CodeReviewOptions = { cwd: owner.cwd, gitRoot: owner.cwd, currentBranch: "feature", branches: ["release/actual"],
  defaultBaseBranch: null, comparisonStatus: "expected", reason: "default_branch_unavailable" };
beforeEach(() => { mocks.options.mockReset().mockResolvedValue(options); });
afterEach(cleanup);

describe("Code review uses the real Source base choices", () => {
  it("keeps missing default-base status and waits for an explicit real branch choice", async () => {
    const selected = vi.fn();
    render(<CodeReviewMenu owner={owner} pending={false} onClose={() => {}} onSelect={selected} />);
    expect(await screen.findByText("release/actual")).toBeTruthy();
    expect(screen.getByRole("status").getAttribute("data-comparison-reason")).toBe("default_branch_unavailable");
    expect(screen.getByRole("status").textContent).toBe("选择分支");
    expect(screen.queryByText("main")).toBeNull();
    expect(selected).not.toHaveBeenCalled();
    expect(mocks.options).toHaveBeenCalledWith("review-owner", owner.cwd);
    fireEvent.click(screen.getByRole("menuitem", { name: "release/actual" }));
    expect(selected).toHaveBeenCalledWith("base-branch", options, "release/actual");
  });
  it("does not manufacture a base action when the actual repository has no other refs", async () => {
    mocks.options.mockResolvedValueOnce({ ...options, branches: [] });
    const selected = vi.fn();
    render(<CodeReviewMenu owner={owner} pending={false} onClose={() => {}} onSelect={selected} />);
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("选择分支"));
    expect(screen.getAllByRole("menuitem")).toHaveLength(1);
    expect(selected).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("menuitem", { name: "审查未提交的更改" }));
    expect(selected).toHaveBeenCalledWith("uncommitted", { ...options, branches: [] });
  });
});
