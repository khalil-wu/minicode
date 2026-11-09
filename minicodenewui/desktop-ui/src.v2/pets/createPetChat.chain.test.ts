/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { createPetChat } from "./createPetChat";
import { handlePeripheralEvent } from "../chat/peripheralEvents";

const mocks = vi.hoisted(() => ({ command: vi.fn(), send: vi.fn(), desktop: vi.fn(() => null as unknown) }));
vi.mock("../hooks/useWebSocket", async load => ({ ...await load<Record<string, unknown>>(), getWebSocket: () => ({ sessionId: "pet-session" }) }));
vi.mock("../desktop/runtime", async load => ({ ...await load<Record<string, unknown>>(), desktop: () => mocks.desktop() }));
vi.mock("../protocol/api", async load => ({ ...await load<Record<string, unknown>>(), apiBase: () => "http://test.local", authHeaders: () => ({}), fetchWithTimeout: (url: string, init?: RequestInit) => fetch(url, init) }));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<Record<string, unknown>>(), sendClientCommand: (...args: unknown[]) => mocks.send(...args), sendClientCommandAwaitResult: (...args: unknown[]) => mocks.command(...args) }));

beforeEach(() => {
  mocks.desktop.mockReturnValue(null);
  mocks.command.mockReset().mockImplementation(async (command: { type: string; conversation_id: string }) => {
    useAppStore.setState(state => ({ conversations: [...state.conversations, { id: command.conversation_id, title: "New chat", updatedAt: "2026-10-19" }] }));
    useAppStore.getState().applyConversationSwitched({ conversationId: command.conversation_id });
    return { type: "command.result", command: command.type, level: "success", message: "", data: {} };
  });
  useAppStore.setState({ conversationId: "original", pendingConversationCreateId: null, pendingConversationSwitchId: null,
    conversations: [{ id: "original", title: "Original", updatedAt: "2026-10-19", workspaceRoot: "C:/demo" }], workingDirectory: "C:/demo",
    draft: "Keep this unsent message", attachments: [{ id: "file", name: "keep.txt", size: 4, type: "text/plain", status: "ready" }],
    quotedMessage: null, selectedMentions: [], selectedSkills: [], messages: [], isStreaming: false,
    conversationMessages: {}, conversationStreaming: {}, conversationWorkbenchStates: {}, workspaceComposerDrafts: {},
    settingsOpen: true, appMode: "cowork", editorTabs: [], activeTabPath: null, activeEditorPath: null });
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ skill: { name: "hatch-pet", path: "C:/private/skills/hatch-pet/SKILL.md" }, prompt: "Create my pet" }), { status: 200 })));
});
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });

it("prepares a real Skill and new projectless draft while retaining the old chat's attachments", async () => {
  await createPetChat();
  expect(mocks.command).toHaveBeenCalledWith(expect.objectContaining({ type: "conversation.create", workspace_root: undefined }), "conversation.create");
  expect(useAppStore.getState()).toMatchObject({ draft: "Create my pet", settingsOpen: false,
    selectedSkills: [{ kind: "skill", name: "hatch-pet", path: "C:/private/skills/hatch-pet/SKILL.md" }] });
  useAppStore.getState().applyConversationSwitched({ conversationId: "original" });
  expect(useAppStore.getState().draft).toBe("Keep this unsent message");
  expect(useAppStore.getState().attachments.map(file => file.name)).toEqual(["keep.txt"]);
});

it("retains the original draft when preparation fails and does not create a pretend chat", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("Image profile is missing", { status: 409 })));
  await expect(createPetChat()).rejects.toThrow("Image profile is missing");
  expect(mocks.command).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "original", draft: "Keep this unsent message", settingsOpen: true });
});

it("refreshes and selects only a live publication event, preserving replay without side effects", async () => {
  const pets = { refresh: vi.fn(async () => {}), update: vi.fn(async () => {}) };
  mocks.desktop.mockReturnValue({ pets });
  const event = { type: "pet.published" as const, pet_id: "custom:actual-pet", conversation_id: "original" };
  expect(handlePeripheralEvent(event)).toBe(true);
  await vi.waitFor(() => expect(pets.update).toHaveBeenCalledWith({ selectedPetId: "custom:actual-pet", petVisible: true }));
  handlePeripheralEvent({ ...event, replayed: true });
  expect(pets.refresh).toHaveBeenCalledTimes(1);
});
