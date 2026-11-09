import { describe, expect, it } from "vitest";

import type { WorkspaceTreeNode } from "../protocol/workspace";
import {
  joinWorkspacePath,
  parentTreePath,
  replaceNodeChildren,
} from "./fileTreeHelpers";


describe("desktop file-tree path space", () => {
  it("maps relative git and change paths into the absolute desktop tree", () => {
    const root = "C:\\repo";
    expect(joinWorkspacePath(root, "src/app.ts")).toBe("C:\\repo/src/app.ts");
    expect(parentTreePath(joinWorkspacePath(root, "src/app.ts"), root)).toBe("C:/repo/src");
  });

  it("replaces nested children across slash and drive-letter casing differences", () => {
    const tree: WorkspaceTreeNode = {
      name: "repo",
      path: "C:\\Repo",
      is_dir: true,
      children: [{ name: "src", path: "C:\\Repo\\src", is_dir: true, children: [] }],
    };
    const child: WorkspaceTreeNode = { name: "app.ts", path: "C:\\Repo\\src\\app.ts", is_dir: false };

    const next = replaceNodeChildren(tree, "c:/repo/src", [child]);

    expect(next.children?.[0]?.children).toEqual([child]);
  });
});
