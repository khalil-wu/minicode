/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  return { send: vi.fn(async () => true), command: vi.fn(() => true) };
});
vi.mock("../protocol/ws-outbox", async (load) => ({ ...await load<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: mocks.command,
  sendClientCommandAwaitResult: vi.fn(async (command, expectedCommand) => {
    mocks.command(command);
    return {
      type: "command.result", command: expectedCommand, client_command_id: command.client_command_id,
      level: "success", message: "", data: {
        conversation_id: command.conversation_id,
        ...(command.type === "user_message" ? {
          admitted: true, user_message_id: command.user_message_id, assistant_message_id: command.assistant_message_id,
        } : {}),
      },
    };
  }),
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
// These cases exercise the supported literal-input mode. Rich editing has its own document/IME suite.
beforeEach(() => useAppStore.setState({ composerPlainTextMode: true }));
vi.mock("./RichComposerEditor", async () => ({ RichComposerEditor: (await import("../../test/literal-composer-editor")).LiteralComposerEditor }));

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
const appendInput = (input: HTMLTextAreaElement, text: string) => {
  const value = input.value + text;
  fireEvent.change(input, { target: { value, selectionStart: value.length, selectionEnd: value.length } });
};
const choose = async (input: HTMLTextAreaElement, market: string) => {
  appendInput(input, `${input.value && !/\s$/.test(input.value) ? " " : ""}@same`);
  const option = await screen.findByRole("option", { name: new RegExp(`Same Plugin.*${market}`) });
  fireEvent.click(option);
};

it("keeps same-name plugin identities distinct from real main picker selection through outgoing context", async () => {
  render(<Composer />);
  const input = screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement;
  await choose(input, "market-a");
  await choose(input, "market-b");
  expect(useAppStore.getState().selectedMentions).toEqual([
    expect.objectContaining({ kind: "plugin", name: "Same Plugin", configName: "same@market-a", path: "plugin://same@market-a" }),
    expect.objectContaining({ kind: "plugin", name: "Same Plugin", configName: "same@market-b", path: "plugin://same@market-b" }),
  ]);
  appendInput(input, "Use both plugins");
  expect(input.value).toContain("plugin://same@market-a");
  expect(input.value).toContain("plugin://same@market-b");
  expect(input.value.endsWith("Use both plugins")).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Send main" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({
    contextRefs: [expect.objectContaining({ name: "Same Plugin", configName: "same@market-a", path: "plugin://same@market-a" }),
      expect.objectContaining({ name: "Same Plugin", configName: "same@market-b", path: "plugin://same@market-b" })],
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
  expect(useAppStore.getState().sideChats[sideId].contextRefs).toEqual([
    expect.objectContaining({ kind: "plugin", name: "Same Plugin", configName: "same@market-a", path: "plugin://same@market-a" }),
    expect.objectContaining({ kind: "plugin", name: "Same Plugin", configName: "same@market-b", path: "plugin://same@market-b" }),
  ]);
  expect(useAppStore.getState().selectedMentions).toEqual([]);
  appendInput(input, "Use both here");
  expect(input.value).toContain("plugin://same@market-a");
  expect(input.value).toContain("plugin://same@market-b");
  await waitFor(() => expect(screen.getByRole("button", { name: "发送" }).hasAttribute("disabled")).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({ conversationId: sideId,
    contextRefs: [expect.objectContaining({ name: "Same Plugin", configName: "same@market-a", path: "plugin://same@market-a" }),
      expect.objectContaining({ name: "Same Plugin", configName: "same@market-b", path: "plugin://same@market-b" })],
  })));
  expect(mocks.command).toHaveBeenCalledWith(expect.objectContaining({ type: "user_message", conversation_id: sideId,
    plugins: [{ config_name: "same@market-a", path: "plugin://same@market-a" }, { config_name: "same@market-b", path: "plugin://same@market-b" }],
  }));
});
