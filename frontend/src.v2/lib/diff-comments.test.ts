import { describe, expect, it } from "vitest";
import {
  cloneDiffComments, diffCommentKey, diffCommentText, formatDiffComment, formatDiffCommentLines,
  formatDiffCommentsPrompt, mergeDiffComments, type DiffComment, type DiffCommentPosition,
} from "./diff-comments";

const comment = (patch: Partial<DiffComment> = {}): DiffComment => ({
  type: "comment", content: [{ content_type: "text", text: "保留这个分支" }],
  position: { path: "src/example.ts", side: "right", line: 206 }, ...patch,
});

describe("frozen desktop ordinary Diff comment context", () => {
  it.each<[Partial<DiffCommentPosition>, string]>([
    [{ line: 206 }, "206"],
    [{ line: 206, start_line: 203 }, "203-206"],
    [{ line: 203, start_line: 206 }, "203-206"],
    [{ line: 206, start_line: 103, start_side: "left" }, "L103-R206"],
    [{ side: "left", line: 103, start_line: 206, start_side: "right" }, "R206-L103"],
    [{ side: "left", line: 206, start_line: 206, start_side: "right" }, "R206-L206"],
  ])("formats actual file range %j", (position, expected) => {
    expect(formatDiffCommentLines({ path: "file.ts", side: "right", line: 1, ...position })).toBe(expected);
  });

  it("preserves path, workspace, text blocks and hunk bytes in the Source prompt", () => {
    const saved = comment({ content: [{ content_type: "text", text: "  保留" }, { content_type: "text", text: "\n换行  " }],
      position: { path: "src/example.ts", side: "right", line: 206, start_line: 103, start_side: "left" },
      localWorkspaceRoot: "C:\\Project", localDiffHunk: "@@ -103 +206 @@\r\n-old\r\n+new\n" });
    expect(diffCommentText(saved)).toBe("  保留\n换行  ");
    expect(formatDiffCommentsPrompt("请处理\n原请求  ", [saved])).toBe(
      "\n# Diff comments:\n\n## User Comment 1\nFile: src/example.ts\nWorkspace: C:\\Project\nSide: L-R\nLines: L103-R206\n"
      + "Diff hunk:\n```diff\n@@ -103 +206 @@\r\n-old\r\n+new\n\n```\nComment:\n  保留\n换行  \n\n## My request:\n请处理\n原请求  \n",
    );
  });

  it("keeps comment-only sends and exact numbering without adding blank hunks", () => {
    expect(formatDiffCommentsPrompt("", [comment({ localDiffHunk: " \n " }), comment({ position: { path: "old.ts", side: "left", line: 987 } })])).toBe(
      "\n# Diff comments:\n\n## User Comment 1\nFile: src/example.ts\nSide: R\nLines: 206\nComment:\n保留这个分支\n"
      + "\n## User Comment 2\nFile: old.ts\nSide: L\nLines: 987\nComment:\n保留这个分支\n\n## My request:\n\n",
    );
    expect(formatDiffComment(comment({ localWorkspaceRoot: "" }), 4)).toContain("Workspace: \n");
    expect(formatDiffCommentsPrompt("ordinary\n", [])).toBe("ordinary\n");
  });

  it("uses Source comment identity, including text, range, side and normalized workspace", () => {
    const saved = comment({ localWorkspaceRoot: "C:\\Project\\" });
    const key = diffCommentKey(saved);
    expect(key).toBe("win32:/mnt/c/project|src/example.ts|right|206|保留这个分支");
    expect(diffCommentKey({ ...saved, localWorkspaceRoot: "/C:/project", localDiffHunk: "changed patch" })).toBe(key);
    expect(diffCommentKey({ ...saved, localWorkspaceRoot: "\\\\?\\C:\\PROJECT" })).toBe(key);
    expect(diffCommentKey({ ...saved, position: { ...saved.position, side: "left" } })).not.toBe(key);
    expect(diffCommentKey({ ...saved, content: [{ content_type: "text", text: "different" }] })).not.toBe(key);
    expect(diffCommentKey({ ...saved, position: { ...saved.position, path: "src/Example.ts" } })).not.toBe(key);
  });

  it("retains WSL Linux path case while matching Windows mount aliases", () => {
    const key = (root: string) => diffCommentKey(comment({ localWorkspaceRoot: root }));
    expect(key("\\\\wsl$\\Ubuntu\\home\\Me")).toBe(key("//wsl.localhost/ubuntu/home/Me/"));
    expect(key("//wsl$/Ubuntu/home/Me")).not.toBe(key("//wsl$/Ubuntu/home/me"));
    expect(key("//wsl$/Ubuntu/mnt/C/Project")).toBe(key("C:/project"));
    expect(key("\\\\?\\UNC\\Server\\Share\\Project")).toBe(key("//server/share/project"));
  });

  it("clones submitted snapshots and merges existing-first by real Source keys", () => {
    const first = comment(), same = { ...comment(), localDiffHunk: "another hunk" }, second = comment({
      content: [{ content_type: "text", text: "second" }],
    });
    const submitted = cloneDiffComments([first]);
    first.content[0].text = "edited after submit";
    first.position.line = 300;
    expect(submitted[0]).toEqual(comment());
    const merged = mergeDiffComments([submitted[0]], [same, second]);
    expect(merged).toEqual([submitted[0], second]);
    expect(merged[0]).not.toBe(submitted[0]);
    expect(merged[0].content).not.toBe(submitted[0].content);
    expect(merged[0].position).not.toBe(submitted[0].position);
  });
});
