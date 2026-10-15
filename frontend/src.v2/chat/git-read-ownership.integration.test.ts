/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { commandWithClientCommandId, registerWebSocketSender } from "../protocol/ws-outbox";
import type { ClientCommand, ServerEvent } from "../protocol/events";
import type { StreamBuffer } from "../lib/stream-buffer";
import { handleSessionEvent } from "./sessionEvents";
import { handleCommandResultEvent } from "./commandResultEvents";
import { handleDiffEvent } from "./diffEvents";
import { pushToast } from "../overlays/ToastContainer";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const initial = useAppStore.getState();
const packets: ClientCommand[] = [];
type GitReadCommand = ClientCommand & {
  type: "diff.git_working_tree" | "diff.git_staged";
  conversation_id: string;
  workspace: string;
  request_id: string;
};
let acceptPacket: (command: ClientCommand) => boolean;
const makeBuffer = (): StreamBuffer => ({ push: vi.fn(), flush: vi.fn(), destroy: vi.fn() });
const buffers = { textStreamBuffer: makeBuffer(), thinkingStreamBuffer: makeBuffer() };
const reads = () => packets.filter((packet): packet is GitReadCommand => packet.type === "diff.git_working_tree" || packet.type === "diff.git_staged");
const currentReads = () => reads().slice(-2);
const switched = (id: string, root: string, isHydrating = false) => handleSessionEvent({
  type: "conversation.switched", conversation_id: id, is_hydrating: isHydrating, context_pending: isHydrating,
  conversation: { id, title: id, workspace_root: root, messages: [], revision: 1, conversation_type: "main" },
  session: { active_conversation_id: id, workspace_root: root, active_stream_conversation_ids: [] },
} as ServerEvent, buffers);
const readSuccess = (packet: GitReadCommand, files = [{ path: "current.ts", patch: "+current", additions: 1, deletions: 0 }]) => ({
  type: packet.type, conversation_id: packet.conversation_id, workspace_root: packet.workspace,
  request_id: packet.request_id,
  client_command_id: packet.client_command_id, is_git_repo: true, files, untracked: [],
} as ServerEvent);
const readFailure = (packet: GitReadCommand, message = "Workspace owner rejected", extra: Record<string, unknown> = {}) => ({
  type: "command.result", command: packet.type, level: "error", message,
  client_command_id: packet.client_command_id, conversation_id: packet.conversation_id,
  workspace_root: packet.workspace, ...extra,
} as ServerEvent);

beforeEach(() => {
  vi.clearAllMocks();
  packets.length = 0;
  acceptPacket = () => true;
  useAppStore.setState({ ...initial, conversationId: "A", workingDirectory: "C:/A",
    pendingConversationSwitchId: null, pendingConversationCreateId: null,
    conversations: [
      { id: "A", title: "A", workspaceRoot: "C:/A", updatedAt: "" },
      { id: "B", title: "B", workspaceRoot: "C:/B", updatedAt: "" },
    ],
    conversationWorkbenchStates: {}, conversationMessages: {}, conversationStreaming: {}, conversationHydration: {},
    gitChanges: { workingTree: [], staged: [], untracked: [], loading: false },
    draft: "保留未发送内容", attachments: [], quotedMessage: null, selectedMentions: [], selectedSkills: [],
  });
  // Only the wire transport leaf is replaced; production allocation, outbox
  // waiters, store navigation and event consumers remain in the chain.
  registerWebSocketSender(command => {
    packets.push(commandWithClientCommandId(command));
    return acceptPacket(command);
  });
});
afterEach(() => registerWebSocketSender(null));

describe("automatic Git read navigation ownership", () => {
  it("invalidates old read IDs at switch intent and defers every read until the new owner is committed", () => {
    useAppStore.getState().requestGitChanges();
    const old = currentReads();
    useAppStore.getState().requestConversationSwitch("B");
    const pending = useAppStore.getState();
    expect(pending).toMatchObject({ conversationId: "A", workingDirectory: "C:/A", pendingConversationSwitchId: "B", draft: "保留未发送内容" });
    expect(pending.gitChanges.workingTreeRequestId).toBeUndefined();
    expect(pending.gitChanges.stagedRequestId).toBeUndefined();
    pending.requestGitChanges();
    expect(reads()).toHaveLength(2);
    for (const packet of old) {
      handleCommandResultEvent(readFailure(packet));
      handleDiffEvent(readSuccess(packet));
    }
    expect(useAppStore.getState().gitChanges.error).toBeUndefined();
    expect(useAppStore.getState().gitChanges.workingTree).toEqual([]);
    expect(pushToast).not.toHaveBeenCalled();
    switched("B", "C:/B", true);
    useAppStore.getState().requestGitChanges();
    expect(currentReads()).toEqual([
      expect.objectContaining({ type: "diff.git_working_tree", conversation_id: "B", workspace: "C:/B" }),
      expect.objectContaining({ type: "diff.git_staged", conversation_id: "B", workspace: "C:/B" }),
    ]);
    expect(currentReads()[0].client_command_id).not.toBe(old[0].client_command_id);
  });

  it("preserves current Git ownership through the second same-owner hydration event", () => {
    useAppStore.getState().requestConversationSwitch("B");
    switched("B", "C:/B", true);
    useAppStore.getState().requestGitChanges();
    const current = currentReads();
    const ids = useAppStore.getState().gitChanges;
    switched("B", "C:/B", false);
    expect(useAppStore.getState().gitChanges).toBe(ids);
    for (const packet of current) handleDiffEvent(readSuccess(packet));
    expect(useAppStore.getState().gitChanges).toMatchObject({ loading: false, isGitRepo: true,
      workingTree: [expect.objectContaining({ path: "current.ts" })], staged: [expect.objectContaining({ path: "current.ts" })] });
  });

  it("invalidates same-CID Git ownership when the authoritative workspace actually changes", () => {
    useAppStore.getState().requestGitChanges();
    const old = currentReads();
    switched("A", "C:/different");
    expect(useAppStore.getState().gitChanges.workingTreeRequestId).toBeUndefined();
    expect(useAppStore.getState().gitChanges.stagedRequestId).toBeUndefined();
    for (const packet of old) handleDiffEvent(readSuccess(packet));
    expect(useAppStore.getState().gitChanges.workingTree).toEqual([]);
    useAppStore.getState().requestGitChanges();
    expect(currentReads()[0]).toMatchObject({ conversation_id: "A", workspace: "C:/different" });
  });

  it("waits for the creation receipt even after the new conversation's authoritative page arrives", async () => {
    const creation = useAppStore.getState().createConversation({ bindWorkspace: true, workspaceRoot: "C:/B", appMode: "code" });
    const command = packets.find((packet): packet is Extract<ClientCommand, { type: "conversation.create" }> => packet.type === "conversation.create")!;
    const id = command.conversation_id!;
    useAppStore.getState().requestGitChanges();
    expect(reads()).toEqual([]);
    switched(id, "C:/B");
    expect(useAppStore.getState()).toMatchObject({ conversationId: id, pendingConversationSwitchId: null, pendingConversationCreateId: id });
    useAppStore.getState().requestGitChanges();
    expect(reads()).toEqual([]);
    handleCommandResultEvent({ type: "command.result", command: "conversation.create", level: "success", message: "Created",
      client_command_id: command.client_command_id, conversation_id: id, workspace_root: "C:/B",
      data: { conversation_id: id, workspace_root: "C:/B" },
    } as ServerEvent);
    expect(await creation).toBe(true);
    useAppStore.getState().requestGitChanges();
    expect(currentReads()[0]).toMatchObject({ conversation_id: id, workspace: "C:/B" });
  });

  it("resumes the original owner's reads and exact authored input after failed creation", async () => {
    const file = new File(["原文件🙂"], "原附件.txt", { type: "text/plain", lastModified: 23 });
    const attachments = [{ id: "original", name: file.name, type: file.type, size: file.size, status: "ready" as const, localFile: file }];
    useAppStore.setState({ attachments });
    useAppStore.getState().requestGitChanges();
    const old = currentReads();
    const creation = useAppStore.getState().createConversation({ bindWorkspace: true, workspaceRoot: "C:/B" });
    const command = packets.find((packet): packet is Extract<ClientCommand, { type: "conversation.create" }> => packet.type === "conversation.create")!;
    handleCommandResultEvent({ type: "command.result", command: "conversation.create", level: "error", message: "Actual creation failed",
      client_command_id: command.client_command_id, conversation_id: command.conversation_id, workspace_root: "C:/B",
    } as ServerEvent);
    expect(await creation).toBe(false);
    expect(useAppStore.getState()).toMatchObject({ conversationId: "A", workingDirectory: "C:/A", draft: "保留未发送内容", attachments,
      pendingConversationSwitchId: null, pendingConversationCreateId: null });
    const resumed = currentReads();
    expect(resumed[0]).toMatchObject({ conversation_id: "A", workspace: "C:/A" });
    expect(resumed[0].client_command_id).not.toBe(old[0].client_command_id);
    expect(pushToast).toHaveBeenCalledWith("Actual creation failed", "error", 4000);
  });

  it("does not requalify a late old result in the same event batch that refuses the new navigation", () => {
    useAppStore.getState().requestGitChanges();
    const old = currentReads();
    useAppStore.getState().requestConversationSwitch("B");
    handleCommandResultEvent({ type: "command.result", command: "conversation.switch", level: "error", message: "Actual switch failed",
      conversation_id: "B", workspace_root: "C:/B",
    } as ServerEvent);
    const resumed = currentReads();
    expect(resumed[0].client_command_id).not.toBe(old[0].client_command_id);
    for (const packet of old) { handleCommandResultEvent(readFailure(packet)); handleDiffEvent(readSuccess(packet)); }
    expect(useAppStore.getState().gitChanges).toMatchObject({ loading: true, workingTree: [], staged: [] });
    expect(useAppStore.getState().gitChanges.error).toBeUndefined();
    expect(pushToast).toHaveBeenCalledTimes(1);
    expect(pushToast).toHaveBeenCalledWith("Actual switch failed", "error", 7000);
    for (const packet of resumed) handleDiffEvent(readSuccess(packet));
    expect(useAppStore.getState().gitChanges.workingTree[0].path).toBe("current.ts");
  });

  it("resumes reads after synchronous transport refusal without waiting for a React pending render", () => {
    useAppStore.getState().requestGitChanges();
    const old = currentReads();
    acceptPacket = command => command.type !== "conversation.switch";
    useAppStore.getState().requestConversationSwitch("B");
    expect(useAppStore.getState().pendingConversationSwitchId).toBeNull();
    expect(currentReads()[0]).toMatchObject({ conversation_id: "A", workspace: "C:/A" });
    expect(currentReads()[0].client_command_id).not.toBe(old[0].client_command_id);
    handleDiffEvent(readSuccess(old[0]));
    expect(useAppStore.getState().gitChanges.workingTree).toEqual([]);
  });

  it.each([
    { conversation_id: "other", workspace_root: "C:/A" },
    { conversation_id: "A", workspace_root: "C:/other" },
  ])("keeps a result with the wrong owner metadata from using the current read ID: %j", extra => {
    useAppStore.getState().requestGitChanges();
    const current = currentReads()[0];
    handleCommandResultEvent(readFailure(current, "Actual error from a different scope", extra));
    expect(useAppStore.getState().gitChanges).toMatchObject({ loading: true, workingTree: [], staged: [] });
    expect(useAppStore.getState().gitChanges.error).toBeUndefined();
    expect(pushToast).not.toHaveBeenCalled();
  });

  it.each(["diff.git_working_tree", "diff.git_staged"])("keeps real current %s failures visible regardless of message wording", command => {
    useAppStore.getState().requestGitChanges();
    const current = currentReads().find(packet => packet.type === command)!;
    const message = "Command conversation owner is stale; switch back to the originating conversation and retry";
    const event = readFailure(current, message);
    handleCommandResultEvent(event);
    expect(useAppStore.getState().gitChanges).toMatchObject({ loading: false, error: message });
    expect(pushToast).toHaveBeenCalledWith(message, "error", 5000);
    expect(event).toMatchObject({ level: "error", message, client_command_id: current.client_command_id });
  });

  it("does not treat an actual current Git mutation failure as an obsolete automatic read", () => {
    useAppStore.getState().requestConversationSwitch("B");
    handleDiffEvent({ type: "diff.git_revert_file", conversation_id: "A", workspace_root: "C:/A", path: "actual.ts", ok: false,
      message: "Actual revert failed",
    } as ServerEvent);
    expect(pushToast).toHaveBeenCalledWith("Actual revert failed", "error", 3500);
  });
});
