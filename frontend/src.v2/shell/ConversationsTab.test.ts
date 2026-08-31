import { describe, expect, it } from "vitest";
import type { ConversationMeta } from "../stores/types";
import { groupByWorkspace, isWorkspaceConversation, orderConversationTree } from "./ConversationsTab";

const conversation = (id: string, workspaceRoot: string): ConversationMeta & { sessionStatus: "idle" } => ({
  id,
  title: id,
  updatedAt: "2026-07-14T00:00:00.000Z",
  workspaceRoot,
  sessionStatus: "idle",
});

describe("groupByWorkspace", () => {
  it("keeps workspace tasks separate from ordinary tasks", () => {
    expect(isWorkspaceConversation(conversation("workspace", "C:\\repo"))).toBe(true);
    expect(isWorkspaceConversation({ ...conversation("worktree", ""), worktreePath: "C:\\repo\\.minicode\\worktrees\\conv_ab_cd" })).toBe(true);
    expect(isWorkspaceConversation(conversation("ordinary", "   "))).toBe(false);
  });

  it("keeps same-basename Windows workspaces in separate groups", () => {
    const groups = groupByWorkspace([
      conversation("client", "C:\\client\\app"),
      conversation("internal", "D:\\internal\\app"),
    ]);

    expect(groups.size).toBe(2);
    expect(Array.from(groups.values()).map((group) => group.label)).toEqual([
      "app — client",
      "app — internal",
    ]);
  });

  it("uses enough parent context to keep duplicate labels unique", () => {
    const groups = groupByWorkspace([
      conversation("one", "C:\\team\\app"),
      conversation("two", "D:\\team\\app"),
    ]);

    expect(new Set(Array.from(groups.values()).map((group) => group.label)).size).toBe(2);
    expect(Array.from(groups.values()).map((group) => group.label)).toEqual([
      "app — C:/team",
      "app — D:/team",
    ]);
  });

  it("normalizes Windows casing and separators without folding POSIX case", () => {
    expect(groupByWorkspace([
      conversation("one", "C:\\Repo\\App"),
      conversation("two", "c:/repo/app/"),
    ]).size).toBe(1);
    expect(groupByWorkspace([
      conversation("upper", "/work/Foo"),
      conversation("lower", "/work/foo"),
    ]).size).toBe(2);
  });

  it("folds real conversation worktree ids back into the project group", () => {
    const groups = groupByWorkspace([
      conversation("main", "C:\\repo"),
      conversation("isolated", "C:\\repo\\.minicode\\worktrees\\conv_abc_def"),
    ]);

    expect(groups.size).toBe(1);
    expect(Array.from(groups.values())[0]?.items).toHaveLength(2);
  });

  it("uses the effective protected worktree path when both workspace fields exist", () => {
    const groups = groupByWorkspace([
      { ...conversation("owner", "C:\\repo"), worktreePath: "C:\\repo\\.minicode\\worktrees\\conv_owner" },
      conversation("shared-clone", "C:\\repo\\.minicode\\worktrees\\conv_owner"),
    ]);

    expect(groups.size).toBe(1);
    expect(Array.from(groups.values())[0]?.items.map((item) => item.id)).toEqual(["owner", "shared-clone"]);
  });
});

describe("orderConversationTree", () => {
  it("orders parents before descendants and computes stable depth", () => {
    const root = conversation("root", "C:\\repo");
    const sibling = conversation("sibling", "C:\\repo");
    const child = { ...conversation("child", "C:\\repo"), parentConversationId: root.id, branchKind: "clone" };
    const grandchild = { ...conversation("grandchild", "C:\\repo"), parentConversationId: child.id, branchKind: "context_fork" };

    expect(orderConversationTree([grandchild, sibling, child, root]).map(({ id, treeDepth }) => [id, treeDepth])).toEqual([
      ["sibling", 0],
      ["root", 0],
      ["child", 1],
      ["grandchild", 2],
    ]);
  });

  it("keeps cyclic metadata visible instead of dropping rows", () => {
    const one = { ...conversation("one", ""), parentConversationId: "two" };
    const two = { ...conversation("two", ""), parentConversationId: "one" };

    expect(orderConversationTree([one, two]).map((item) => item.id)).toEqual(["one", "two"]);
  });
});
