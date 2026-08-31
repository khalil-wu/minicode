import { describe, expect, it } from "vitest";
import { branchDisplayName, canonicalWorkspacePath, isInternalConversationName, workspaceDisplayName } from "./workspace-display";

describe("workspace display helpers", () => {
  it("hides internal conversation worktree names in labels", () => {
    expect(workspaceDisplayName("C:/repo/.minicode/worktrees/conv_56593501d39d")).toBe("Current workspace");
    expect(workspaceDisplayName("C:/repo/MiniCode")).toBe("MiniCode");
  });

  it("hides generated minicode branches", () => {
    expect(branchDisplayName("minicode/conv_56593501d39d")).toBe("isolated session");
    expect(branchDisplayName("feature/ui-polish")).toBe("feature/ui-polish");
  });

  it("uses the main workspace when creating ordinary sessions from an isolated worktree", () => {
    expect(canonicalWorkspacePath("C:/repo/.minicode/worktrees/conv_56593501d39d")).toBe("C:/repo");
    expect(canonicalWorkspacePath("C:\\repo\\.minicode\\worktrees\\conv_56593501d39d")).toBe("C:\\repo");
    expect(canonicalWorkspacePath("C:/repo/MiniCode")).toBe("C:/repo/MiniCode");
  });

  it("recognizes generated conversation ids with multiple underscore segments", () => {
    expect(isInternalConversationName("conv_abc_def")).toBe(true);
    expect(canonicalWorkspacePath("C:\\repo\\.minicode\\worktrees\\conv_abc_def")).toBe("C:\\repo");
  });
});
