import { describe, expect, it } from "vitest";
import {
  normalizeWorkspacePath,
  normalizeWorkspaceRoot,
  workspacePathComparisonKey,
  workspacePathWithin,
  workspaceFilePathComparisonKey,
  workspaceFilePathsEqual,
  workspacePathsEqual,
  workspaceRootsEqual,
} from "./workspace-path";

describe("normalizeWorkspaceRoot", () => {
  it("normalizes Windows roots case-insensitively", () => {
    expect(normalizeWorkspaceRoot("C:\\Projects\\Demo\\")).toBe("c:/projects/demo");
    expect(normalizeWorkspaceRoot("C:/Projects/Demo")).toBe("c:/projects/demo");
  });

  it("preserves POSIX root case", () => {
    expect(normalizeWorkspaceRoot("/tmp/Project/")).toBe("/tmp/Project");
    expect(normalizeWorkspaceRoot("/tmp/project")).not.toBe(normalizeWorkspaceRoot("/tmp/Project"));
  });

  it("preserves UNC roots while normalizing separators", () => {
    expect(normalizeWorkspacePath("\\\\Server\\Share\\\\Project\\")).toBe("//Server/Share/Project");
    expect(normalizeWorkspaceRoot("\\\\Server\\Share\\Project")).toBe("//server/share/project");
  });
});

describe("workspace path comparisons", () => {
  it("uses Windows case-insensitive semantics for drive paths", () => {
    expect(workspacePathsEqual("C:\\Projects\\Demo", "c:/projects/demo")).toBe(true);
    expect(workspaceRootsEqual("C:\\Projects\\Demo", "c:/projects/demo/")).toBe(true);
    expect(workspacePathWithin("C:\\Projects\\Demo\\src\\app.ts", "c:/projects/demo")).toBe(true);
  });

  it("keeps POSIX comparisons case-sensitive", () => {
    expect(workspacePathsEqual("/tmp/Project", "/tmp/project")).toBe(false);
    expect(workspaceRootsEqual("/tmp/Project", "/tmp/project")).toBe(false);
    expect(workspacePathWithin("/tmp/project/file.txt", "/tmp/Project")).toBe(false);
    expect(workspacePathComparisonKey("/tmp/Project/file.txt")).toBe("/tmp/Project/file.txt");
  });

  it("does not confuse a sibling with a child", () => {
    expect(workspacePathWithin("/tmp/project-secret/file.txt", "/tmp/project")).toBe(false);
  });

  it("treats two missing workspace roots as the same identity", () => {
    expect(workspaceRootsEqual("", undefined)).toBe(true);
  });

  it("uses the owning Windows workspace for relative and absolute file identities", () => {
    const root = "C:/Projects/Demo";
    expect(workspaceFilePathComparisonKey("src\\App.ts", root)).toBe("c:/projects/demo/src/app.ts");
    expect(workspaceFilePathsEqual("src/App.ts", "c:/projects/demo/SRC/app.ts", root)).toBe(true);
  });

  it("keeps POSIX file identities case-sensitive even with a workspace root", () => {
    expect(workspaceFilePathsEqual("src/App.ts", "src/app.ts", "/tmp/project")).toBe(false);
  });
});
