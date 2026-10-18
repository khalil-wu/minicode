// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ServerEvent } from "../protocol/events";
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", {
  configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
}));
const mocks = vi.hoisted(() => ({ fetch: vi.fn(), send: vi.fn(() => true), awaitResult: vi.fn(), confirm: vi.fn(), toast: vi.fn() }));
vi.mock("../protocol/api", async (load) => ({ ...await load<typeof import("../protocol/api")>(),
  apiBase: () => "http://settings.test", authHeaders: () => ({}), fetchWithTimeout: mocks.fetch,
}));
vi.mock("../protocol/ws-outbox", async (load) => ({ ...await load<typeof import("../protocol/ws-outbox")>(),
  sendClientCommand: mocks.send, sendClientCommandAwaitResult: mocks.awaitResult,
}));
vi.mock("./DialogService", () => ({ showConfirm: mocks.confirm }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../components/BrandIcon", () => ({ BrandIcon: () => null }));
import { PersonalizationTab } from "./PersonalizationTab";
import { FooterRow } from "../composer/FooterRow";
import { useAppStore } from "../stores";
import { handleCommandCatalogEvent } from "../chat/commandCatalogEvents";
import { handleRuntimeEvent } from "../chat/runtimeEvents";
import { handleSessionEvent } from "../chat/sessionEvents";

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};
const skill = (name: string, root: string) => ({ name, description: "workflow", source_level: "workspace", path: `${root}/.minicode/skills/${name}/SKILL.md`, icon: "./icon.svg" });
const buffers = { textStreamBuffer: { destroy: vi.fn(), flush: vi.fn(), push: vi.fn() }, thinkingStreamBuffer: { destroy: vi.fn(), flush: vi.fn(), push: vi.fn() } };
let caseNumber = 0;
beforeEach(() => {
  vi.resetAllMocks();
  mocks.send.mockReturnValue(true);
  mocks.confirm.mockResolvedValue(true);
  mocks.awaitResult.mockResolvedValue({ type: "command.result", command: "conversation.memory_mode.set", level: "success", message: "" });
  const root = `C:/settings-${++caseNumber}`;
  useAppStore.setState({
    conversationId: "A", workingDirectory: root, conversations: [{ id: "A", title: "A", workspaceRoot: root, updatedAt: "2026-10-03" }],
    messages: [], isStreaming: false, availableSkills: [], selectedSkills: [], selectedMentions: [], mentionResults: [], slashCommands: [],
    runtimeCapabilities: null, conversationAgentStates: {}, conversationWorkbenchStates: {}, conversationMessages: {}, conversationStreaming: {},
    currentModel: "model-a", currentProvider: "custom", permissionMode: "confirm", availableModels: ["model-a"],
  });
});
afterEach(cleanup);

describe("settings and selected catalog ownership", () => {
  it("cancels a task memory confirmation when its conversation changes", async () => {
    const confirmation = deferred<boolean>();
    mocks.confirm.mockReturnValue(confirmation.promise);
    useAppStore.setState((state) => ({ conversations: state.conversations.map((item) => ({ ...item, memoryPolluted: true, memoryMode: "polluted" })) }));
    render(<PersonalizationTab />);
    fireEvent.click(await screen.findByRole("button", { name: "重新启用" }));
    act(() => useAppStore.setState({ conversationId: "B" }));
    await act(async () => confirmation.resolve(true));
    expect(mocks.awaitResult).not.toHaveBeenCalled();
  });

  it("clears old workspace inputs and rejects same-conversation stale catalogs", () => {
    const originalRoot = useAppStore.getState().workingDirectory;
    useAppStore.setState({ availableSkills: [skill("old-skill", originalRoot)], selectedSkills: [{ name: "old-skill", path: `${originalRoot}/SKILL.md` }],
      selectedMentions: [{ path: "old.ts", name: "old.ts", kind: "file" }], runtimeCapabilities: { skills: [skill("old-skill", originalRoot)] } });
    useAppStore.getState().setWorkingDirectory(`${originalRoot}-B`);
    expect(useAppStore.getState().selectedSkills).toEqual([]);
    expect(useAppStore.getState().selectedMentions).toEqual([]);
    expect(useAppStore.getState().runtimeCapabilities).toBeNull();
    handleCommandCatalogEvent({ type: "skills.list", conversation_id: "A", workspace_root: originalRoot, skills: [skill("old-skill", originalRoot)] } as ServerEvent);
    handleRuntimeEvent({ type: "runtime.capabilities", conversation_id: "A", workspace_root: originalRoot, capabilities: { skills: [skill("old-skill", originalRoot)] } });
    expect(useAppStore.getState().availableSkills).toEqual([]);
    const root = `${originalRoot}-B`;
    handleCommandCatalogEvent({ type: "skills.list", conversation_id: "A", workspace_root: root, skills: [skill("new-skill", root)] } as ServerEvent);
    expect(useAppStore.getState().availableSkills[0].name).toBe("new-skill");
    expect(new URL(useAppStore.getState().availableSkills[0].icon!).searchParams.get("workspace_root")).toBe(root);
  });

  it("clears the old catalog on the actual conversation hydration path before applying the new snapshot", () => {
    const originalRoot = useAppStore.getState().workingDirectory;
    useAppStore.setState({ availableSkills: [skill("old-skill", originalRoot)], runtimeCapabilities: { skills: [skill("old-skill", originalRoot)] } });
    const root = `${originalRoot}-B`;
    handleSessionEvent({ type: "conversation.switched", conversation_id: "B", conversation: { id: "B", title: "B", workspace_root: root, messages: [] },
      session: { active_conversation_id: "B", workspace_root: root, capabilities: { skills: [skill("new-skill", root)] } } } as ServerEvent, buffers);
    expect(useAppStore.getState().workingDirectory).toBe(root);
    expect(useAppStore.getState().availableSkills.map((item) => item.name)).toEqual(["new-skill"]);
    expect(new URL(useAppStore.getState().availableSkills[0].icon!).searchParams.get("workspace_root")).toBe(root);
    handleCommandCatalogEvent({ type: "skills.list", conversation_id: "A", workspace_root: originalRoot, skills: [skill("old-skill", originalRoot)] } as ServerEvent);
    expect(useAppStore.getState().availableSkills[0].name).toBe("new-skill");
  });

  it("lets incoming same-scope snapshots override old effort support and catalogs", () => {
    const root = useAppStore.getState().workingDirectory;
    useAppStore.setState({ runtimeCapabilities: { skills: [skill("old-skill", root)], provider_capabilities: { model: "model-a", reasoning_effort_supported: true, reasoning_effort_levels: ["high"] } } });
    handleSessionEvent({ type: "conversation.switched", conversation_id: "A", conversation: { id: "A", title: "A", workspace_root: root, messages: [] },
      session: { active_conversation_id: "A", workspace_root: root, capabilities: { skills: [], provider_capabilities: { model: "model-a", reasoning_effort_supported: false, reasoning_effort_levels: [] } } } } as ServerEvent, buffers);
    expect(useAppStore.getState().runtimeCapabilities?.provider_capabilities?.reasoning_effort_supported).toBe(false);
    expect(useAppStore.getState().availableSkills).toEqual([]);
  });

  it("does not offer the old model's effort slider after the current model changes", () => {
    useAppStore.setState({ runtimeCapabilities: { provider_capabilities: { model: "old-model", reasoning_effort_supported: true, reasoning_effort_levels: ["low", "high"] } } });
    render(<FooterRow sendState="send" onSend={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "model-a" }));
    expect(screen.queryByRole("slider", { name: "推理强度" })).toBeNull();
    expect(screen.getByRole("listbox", { name: "选择模型" })).toBeTruthy();
  });
});
