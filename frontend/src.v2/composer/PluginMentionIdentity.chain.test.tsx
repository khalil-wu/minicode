/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  return { send: vi.fn(async () => true), command: vi.fn(() => true) };
});
vi.mock("../protocol/ws-outbox", async (load) => ({ ...await load<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: mocks.command,
  sendClientCommandAwaitResult: vi.fn(async (command) => ({ type: "command.result", level: "success", data: { conversation_id: command.conversation_id } })),
  sendConversationDeleteCommand: vi.fn(async () => true),
}));
vi.mock("../chat/sendChatMessage", async (load) => {
  const actual = await load<typeof import("../chat/sendChatMessage")>();
  mocks.send.mockImplementation(actual.sendChatMessage);
  return { ...actual, sendChatMessage: mocks.send };
});
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: "plugin-session", send: mocks.command }) }));
vi.mock("../desktop/runtime", async (load) => ({ ...await load<typeof import("../desktop/runtime")>(), isDesktop: () => false }));
vi.mock("../protocol/workspace", () => ({ listWorkspaceTree: vi.fn(async () => ({ children: [] })), searchWorkspaceFiles: vi.fn(async () => []) }));
vi.mock("./FooterRow", () => ({ FooterRow: ({ onSend }: { onSend: () => void }) => <button onClick={onSend}>Send main</button> }));
import { useAppStore } from "../stores";
import { Composer } from "./Composer";
import { SideChatPanel } from "../panels/SideChatPanel";

const plugins = [
  { id: "same@market-a", name: "same", displayName: "Same Plugin", marketplace: "market-a", shortDescription: "Shared functionality", enabled: true },
  { id: "same@market-b", name: "same", displayName: "Same Plugin", marketplace: "market-b", shortDescription: "Shared functionality", enabled: true },
  { id: "disabled@market-a", name: "disabled", marketplace: "market-a", enabled: false },
];
beforeEach(() => {
  vi.clearAllMocks();
  Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ plugins }))));
  useAppStore.setState({ conversationId: "plugin-main", workingDirectory: "C:/main", conversations: [],
    isConnected: true, isStreaming: false, currentModel: "gpt", draft: "", attachments: [], messages: [],
    selectedMentions: [], selectedSkills: [], availableSkills: [], slashCommands: [], slashPanelOpen: false, mentionPanelOpen: false,
    sideChats: {}, sideChatPendingContext: null, activeGoal: null, pendingApproval: null, approvalQueue: [], pendingAskUser: null,
    askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [], quotedMessage: null, followUpBehavior: "queue" });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const choose = async (input: HTMLTextAreaElement, market: string) => {
  fireEvent.change(input, { target: { value: "@same", selectionStart: 5, selectionEnd: 5 } });
  const option = await screen.findByRole("option", { name: new RegExp(`Same Plugin.*${market}`) });
  fireEvent.click(option);
};

it("keeps same-name plugin identities distinct from real main picker selection through outgoing context", async () => {
  render(<Composer />);
  const input = screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement;
  await choose(input, "market-a");
  await choose(input, "market-b");
  expect(useAppStore.getState().selectedMentions).toEqual([
    { kind: "plugin", name: "same@market-a", configName: "same@market-a", path: "plugin://same@market-a" },
    { kind: "plugin", name: "same@market-b", configName: "same@market-b", path: "plugin://same@market-b" },
  ]);
  fireEvent.change(input, { target: { value: "Use both plugins" } });
  fireEvent.click(screen.getByRole("button", { name: "Send main" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({
    contextRefs: [expect.objectContaining({ configName: "same@market-a" }), expect.objectContaining({ configName: "same@market-b" })],
  })));
  expect(mocks.command).toHaveBeenCalledWith(expect.objectContaining({ type: "user_message", conversation_id: "plugin-main",
    plugins: [{ config_name: "same@market-a", path: "plugin://same@market-a" }, { config_name: "same@market-b", path: "plugin://same@market-b" }],
  }));
});

it("keeps the same canonical plugin identities in side-owned outgoing context", async () => {
  render(<SideChatPanel />);
  await waitFor(() => expect(screen.getByRole("button", { name: "发送" }).hasAttribute("disabled")).toBe(true));
  const input = screen.getByRole("textbox", { name: "侧边对话消息" }) as HTMLTextAreaElement;
  await choose(input, "market-a");
  await choose(input, "market-b");
  const sideId = Object.keys(useAppStore.getState().sideChats)[0];
  expect(useAppStore.getState().sideChats[sideId].contextRefs.map((ref) => ref.path)).toEqual(["plugin://same@market-a", "plugin://same@market-b"]);
  expect(useAppStore.getState().selectedMentions).toEqual([]);
  fireEvent.change(input, { target: { value: "Use both here" } });
  await waitFor(() => expect(screen.getByRole("button", { name: "发送" }).hasAttribute("disabled")).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({ conversationId: sideId,
    contextRefs: [expect.objectContaining({ configName: "same@market-a" }), expect.objectContaining({ configName: "same@market-b" })],
  })));
  expect(mocks.command).toHaveBeenCalledWith(expect.objectContaining({ type: "user_message", conversation_id: sideId,
    plugins: [{ config_name: "same@market-a", path: "plugin://same@market-a" }, { config_name: "same@market-b", path: "plugin://same@market-b" }],
  }));
});
