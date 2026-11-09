// @vitest-environment jsdom
import { loadConversationMenu } from "./ConversationMenu";

// These action suites use a ready interaction module; cold ownership is covered separately.
beforeAll(async () => { await loadConversationMenu(); });
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeAll, afterEach, expect, it, vi } from "vitest";
import { ChatActionsButton } from "./ChatActionsButton";
import { ConversationsTab } from "./ConversationsTab";
import { useAppStore, waitComposerDraftRecovery } from "../stores";
import type { DraftModelSelection } from "../protocol/api";

const mocks = vi.hoisted(() => {
  let release!: () => void;
  const ready = new Promise<void>(resolve => { release = resolve; });
  return { ready, release: () => release(), started: vi.fn(), load: vi.fn(), copy: vi.fn(), toast: vi.fn(), send: vi.fn(), selection: vi.fn(async (workspaceRoot: string): Promise<DraftModelSelection> => ({
    provider: "fixture", model: "fixture-model", current_model: "fixture-model", model_selection_mode: "default",
    default_model_selection: null, available_model_options: [], available_models: ["fixture-model"], models_source: "configured",
    available_permission_modes: ["confirm"], config_version: "d10-copy-startup", config_cwd: workspaceRoot,
    feature_flags: {}, composer_commands: [],
  })) };
});
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, writable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../chat/conversationMarkdown", async () => {
  mocks.started(); await mocks.ready;
  return { loadConversationMarkdown: mocks.load };
});
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, desktop: () => undefined, revealPath: vi.fn() }));
vi.mock("../workspace/openWorkspaceFolder", () => ({ openWorkspaceFolder: vi.fn() }));
vi.mock("../protocol/api", async original => ({ ...await original<typeof import("../protocol/api")>(), fetchDraftModelSelection: mocks.selection }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../lib/clipboard", () => ({ copyText: mocks.copy }));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: mocks.send, sendClientCommandAwaitResult: vi.fn() }));
vi.mock("../lib/automations-navigation", async load => ({ ...await load<typeof import("../lib/automations-navigation")>(),
  loadThreadAutomations: async () => [] }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("loads Markdown only for the two copy actions and retains each captured owner while the chunk is pending", async () => {
  await waitComposerDraftRecovery();
  mocks.toast.mockClear();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  const aMessages = [{ id: "a-user", role: "user" as const, content: "A original text", timestamp: 1, artifacts: [] }];
  const cMessages = [{ id: "c-user", role: "user" as const, content: "C background text", timestamp: 2, artifacts: [] }];
  const cHistory = { hasMore: true, beforeMessageId: "c-user", loading: false };
  localStorage.removeItem("minicode.sidebar.conversations.state");
  useAppStore.setState({ conversationId: "A", activeConversationDraftId: null, pendingConversationSwitchId: null,
    appMode: "cowork", isStreaming: false, isConnected: false, workingDirectory: "C:/A", draft: "Keep A draft",
    messages: aMessages, conversationMessages: { C: cMessages }, conversationHistoryPages: { C: cHistory },
    conversationStreaming: {}, conversationHydration: {}, runtimeSession: null, sidebarOrder: null,
    conversations: [
      { id: "A", title: "A title", workspaceRoot: "C:/A", updatedAt: "2026-10-26" },
      { id: "C", title: "C background title", workspaceRoot: "C:/C", updatedAt: "2026-10-25", messageCount: 3 },
    ],
    recentWorkspaces: [
      { path: "C:/A", name: "A", projectType: "node", lastOpened: 1 },
      { path: "C:/C", name: "C", projectType: "node", lastOpened: 2 },
    ],
  });
  mocks.load.mockImplementation(async (owner: { id: string }) => `# Captured ${owner.id}\n`);
  mocks.copy.mockResolvedValue(true);
  render(<><ChatActionsButton /><ConversationsTab conversationId="A" showRecent={false} onSetConfirmDialog={vi.fn()} /></>);
  expect(mocks.started).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "聊天操作" }));
  const copy = screen.getByRole("menuitem", { name: "复制", exact: true });
  copy.focus(); fireEvent.keyDown(copy, { key: "ArrowRight" });
  fireEvent.click(screen.getByRole("menuitem", { name: "复制为 Markdown" }));
  await waitFor(() => expect(mocks.started).toHaveBeenCalledTimes(1));
  const row = screen.getByText("C background title").closest('[data-session-row="true"]')!;
  fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "会话操作" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "复制", exact: true }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "复制为 Markdown" }));
  expect(mocks.load).not.toHaveBeenCalled();
  expect(mocks.copy).not.toHaveBeenCalled();
  const bMessages = [{ id: "b-user", role: "user" as const, content: "B current text", timestamp: 3, artifacts: [] }];
  act(() => useAppStore.setState({ conversationId: "B", workingDirectory: "C:/B", messages: bMessages, draft: "Keep B draft" }));
  await act(async () => mocks.release());
  await waitFor(() => expect(mocks.copy).toHaveBeenCalledTimes(2));
  expect(mocks.load).toHaveBeenCalledWith(expect.objectContaining({ id: "A", title: "A title", workspaceRoot: "C:/A", messages: aMessages }));
  expect(mocks.load).toHaveBeenCalledWith(expect.objectContaining({ id: "C", title: "C background title", workspaceRoot: "C:/C", messages: cMessages, historyPage: cHistory, messageCount: 3 }));
  expect(mocks.copy).toHaveBeenCalledWith("# Captured A\n", "复制为 Markdown");
  expect(mocks.copy).toHaveBeenCalledWith("# Captured C\n", "复制为 Markdown");
  expect(useAppStore.getState()).toMatchObject({ conversationId: "B", workingDirectory: "C:/B", draft: "Keep B draft", messages: bMessages });
  expect(mocks.toast).not.toHaveBeenCalled();
});
