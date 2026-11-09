/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { registerWebSocketSender } from "../protocol/ws-outbox";
import type { ClientCommand, ServerEvent } from "../protocol/events";
import { handleDiffEvent } from "./diffEvents";
import { handleCommandResultEvent } from "./commandResultEvents";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
const initial = useAppStore.getState();
const packets: ClientCommand[] = [];
beforeEach(() => {
  packets.length = 0;
  localStorage.removeItem("hideDiffWhitespace");
  useAppStore.setState({ ...initial, conversationId: "A", workingDirectory: "C:/A", pendingConversationSwitchId: null, pendingConversationCreateId: null,
    conversations: [{ id: "A", title: "A", workspaceRoot: "C:/A", updatedAt: "" }, { id: "B", title: "B", workspaceRoot: "C:/B", updatedAt: "" }],
    gitChanges: { workingTree: [{ path: "same.ts", additions: 10, deletions: 4 }], staged: [{ path: "same.ts", additions: 8, deletions: 2 }], untracked: [], loading: false },
    draft: "保留草稿", attachments: [] });
  registerWebSocketSender(command => { packets.push(command); return true; });
});
afterEach(() => registerWebSocketSender(null));
const reply = (command: ClientCommand, data: Record<string, unknown> = {}) => ({
  type: command.type, source: command.source, request_id: command.request_id,
  client_command_id: command.client_command_id, conversation_id: "A", workspace_root: "C:/A", environment_id: "captured-local", cwd_uri: "file:///C:/A",
  snapshot_generation: "real-generation", hide_whitespace: false, is_git_repo: true, status: "success", untracked_files: [],
  files: [{ path: "same.ts", patch: "@@ -1 +1 @@\n-head\n+disk\n", additions: 1, deletions: 1 }], ...data,
} as ServerEvent);

it("projects the real single comparison without summing staged and unstaged metadata", () => {
  useAppStore.getState().requestGitComparison({ source: "uncommitted" });
  const command = packets.at(-1)!;
  expect(command).toMatchObject({ type: "diff.git_working_tree", source: "uncommitted", conversation_id: "A", workspace: "C:/A" });
  handleDiffEvent(reply(command));
  expect(useAppStore.getState().gitChanges.comparison).toMatchObject({ source: "uncommitted", loading: false,
    files: [{ path: "same.ts", additions: 1, deletions: 1 }],
    snapshot: { source: "uncommitted", conversationId: "A", environmentId: "captured-local", generation: "real-generation" } });
  expect(useAppStore.getState().gitChanges.workingTree[0].additions).toBe(10);
  expect(useAppStore.getState().gitChanges.staged[0].additions).toBe(8);
});

it("rejects an older scope and reuses explicit staged/unstaged reads without a duplicate comparison command", () => {
  useAppStore.getState().requestGitComparison({ source: "branch", baseBranch: "origin/release" });
  const old = packets.at(-1)!;
  useAppStore.getState().requestGitComparison({ source: "staged" });
  const staged = packets.at(-1)!;
  handleDiffEvent(reply(old));
  expect(useAppStore.getState().gitChanges.comparison?.files).toEqual([]);
  handleDiffEvent(reply(staged));
  expect(useAppStore.getState().gitChanges.comparison?.source).toBe("staged");
  packets.length = 0;
  useAppStore.getState().requestGitChanges();
  expect(packets.map(packet => packet.type)).toEqual(["diff.git_working_tree", "diff.git_staged"]);
  expect(useAppStore.getState().gitChanges.comparison?.requestId).toBe(packets[1].request_id);
  handleDiffEvent(reply(packets[1]));
  expect(useAppStore.getState().gitChanges.comparison?.snapshot?.source).toBe("staged");
});

it("retains Source's expected missing branch state and ignores old errors after a newer selection", () => {
  useAppStore.getState().requestGitComparison({ source: "branch" });
  const branch = packets.at(-1)!;
  handleDiffEvent(reply(branch, { status: "expected", reason: "default_branch_unavailable", files: [] }));
  expect(useAppStore.getState().gitChanges.comparison).toMatchObject({ source: "branch", status: "expected", reason: "default_branch_unavailable", files: [] });
  expect(packets).toHaveLength(1);
  useAppStore.getState().requestGitComparison({ source: "uncommitted" });
  handleCommandResultEvent({ type: "command.result", command: branch.type, level: "error", message: "late original error",
    client_command_id: branch.client_command_id, data: { conversation_id: "A", workspace_root: "C:/A" } } as ServerEvent);
  expect(useAppStore.getState().gitChanges.comparison?.error).toBeUndefined();
  const active = packets.at(-1)!;
  handleCommandResultEvent({ type: "command.result", command: active.type, level: "error", message: "actual comparison error",
    client_command_id: active.client_command_id, data: { conversation_id: "A", workspace_root: "C:/A" } } as ServerEvent);
  expect(useAppStore.getState().gitChanges.comparison).toMatchObject({ source: "uncommitted", loading: false, error: "actual comparison error" });
});

it("retires comparison ownership at navigation intent and does not send new Git work during pending navigation", () => {
  useAppStore.getState().requestGitComparison({ source: "commit", commitSha: "real-commit" });
  const old = packets.at(-1)!;
  useAppStore.getState().requestConversationSwitch("B");
  expect(useAppStore.getState().gitChanges.comparison).toBeUndefined();
  const count = packets.length;
  useAppStore.getState().requestGitComparison({ source: "branch" });
  expect(packets).toHaveLength(count);
  handleDiffEvent(reply(old));
  expect(useAppStore.getState().gitChanges.comparison).toBeUndefined();
  expect(useAppStore.getState().draft).toBe("保留草稿");
});
