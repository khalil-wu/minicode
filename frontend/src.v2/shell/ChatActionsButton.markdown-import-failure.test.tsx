// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ChatActionsButton } from "./ChatActionsButton";
import { useAppStore, waitComposerDraftRecovery } from "../stores";
import type { DraftModelSelection } from "../protocol/api";

const mocks = vi.hoisted(() => {
  let fail!: () => void;
  const pending = new Promise<void>((_resolve, reject) => { fail = () => reject(new Error("Markdown chunk unavailable")); });
  return { pending, fail: () => fail(), started: vi.fn(), toast: vi.fn(), copy: vi.fn(), selection: vi.fn(async (workspaceRoot: string): Promise<DraftModelSelection> => ({
    provider: "fixture", model: "fixture-model", current_model: "fixture-model", model_selection_mode: "default",
    default_model_selection: null, available_model_options: [], available_models: ["fixture-model"], models_source: "configured",
    available_permission_modes: ["confirm"], config_version: "d10-copy-startup", config_cwd: workspaceRoot,
    feature_flags: {}, composer_commands: [],
  })) };
});
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, writable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../chat/conversationMarkdown", async () => { mocks.started(); await mocks.pending; return { loadConversationMarkdown: vi.fn() }; });
vi.mock("../protocol/api", async original => ({ ...await original<typeof import("../protocol/api")>(), fetchDraftModelSelection: mocks.selection }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../lib/clipboard", () => ({ copyText: mocks.copy }));
vi.mock("../lib/automations-navigation", async load => ({ ...await load<typeof import("../lib/automations-navigation")>(), loadThreadAutomations: async () => [] }));
afterEach(cleanup);

it("uses the original copy error feedback when the Markdown operation chunk cannot load", async () => {
  await waitComposerDraftRecovery();
  mocks.toast.mockClear();
  useAppStore.setState({ conversationId: "A", activeConversationDraftId: null, pendingConversationSwitchId: null,
    workingDirectory: "C:/A", isStreaming: false, draft: "Keep original draft", messages: [],
    conversations: [{ id: "A", title: "A title", workspaceRoot: "C:/A", updatedAt: "2026-10-26" }] });
  render(<ChatActionsButton />);
  expect(mocks.started).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "聊天操作" }));
  const copy = screen.getByRole("menuitem", { name: "复制", exact: true });
  copy.focus(); fireEvent.keyDown(copy, { key: "ArrowRight" });
  fireEvent.click(screen.getByRole("menuitem", { name: "复制为 Markdown" }));
  await waitFor(() => expect(mocks.started).toHaveBeenCalledTimes(1));
  await act(async () => mocks.fail());
  await waitFor(() => expect(mocks.toast).toHaveBeenCalledTimes(1));
  const error = await import("../chat/conversationMarkdown").then(() => { throw new Error("Expected operation import failure"); }, cause => cause as Error);
  expect(mocks.toast).toHaveBeenCalledExactlyOnceWith(error.message, "error");
  expect(mocks.copy).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "A", workingDirectory: "C:/A", draft: "Keep original draft" });
});
