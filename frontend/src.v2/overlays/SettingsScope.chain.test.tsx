// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), { status });
const personalization = (instructions = "old") => ({ instructions, path: "C:/user/INSTRUCTIONS.md", exists: true, max_bytes: 32768 });
const source = (path: string) => ({ path, scope: "workspace", source_kind: "agent_instruction", label: "Project instructions" });
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
  mocks.fetch.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url.includes("/api/settings/personalization")) return response(personalization(init?.method === "PUT" ? JSON.parse(String(init.body)).instructions : "old"));
    if (url.includes("/api/guidelines")) return response({ blocks: [source(`${new URL(url).searchParams.get("workspace_dir")}/AGENTS.md`)] });
    throw new Error(`Unexpected request: ${url}`);
  });
});
afterEach(cleanup);

describe("settings and selected catalog ownership", () => {
  it("loads project instruction sources for the active workspace", async () => {
    const root = useAppStore.getState().workingDirectory;
    render(<PersonalizationTab />);
    await screen.findByText(`${root}/AGENTS.md`);
    expect(mocks.fetch.mock.calls.some(([url]) => new URL(String(url)).searchParams.get("workspace_dir") === root)).toBe(true);
  });

  it("preserves new instruction edits typed while the saved baseline is pending", async () => {
    const save = deferred<Response>();
    const normal = mocks.fetch.getMockImplementation()!;
    mocks.fetch.mockImplementation((url, init) => init?.method === "PUT" ? save.promise : normal(url, init));
    render(<PersonalizationTab />);
    const input = await screen.findByRole("textbox", { name: "自定义指令" });
    fireEvent.change(input, { target: { value: "first edit" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    fireEvent.change(input, { target: { value: "newer edit" } });
    await act(async () => save.resolve(response(personalization("first edit"))));
    expect((input as HTMLTextAreaElement).value).toBe("newer edit");
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("replaces A sources with B and rejects A's delayed response", async () => {
    const originalRoot = useAppStore.getState().workingDirectory;
    const old = deferred<Response>();
    const normal = mocks.fetch.getMockImplementation()!;
    mocks.fetch.mockImplementation((url, init) => new URL(String(url)).searchParams.get("workspace_dir") === originalRoot ? old.promise : normal(url, init));
    render(<PersonalizationTab />);
    await screen.findByRole("textbox", { name: "自定义指令" });
    act(() => useAppStore.getState().setWorkingDirectory(`${originalRoot}-B`));
    await screen.findByText(`${originalRoot}-B/AGENTS.md`);
    await act(async () => old.resolve(response({ blocks: [source(`${originalRoot}/AGENTS.md`)] })));
    expect(screen.queryByText(`${originalRoot}/AGENTS.md`)).toBeNull();
  });

  it("shows a source-load error without reporting that no instructions exist", async () => {
    const normal = mocks.fetch.getMockImplementation()!;
    mocks.fetch.mockImplementation((url, init) => String(url).includes("guidelines") ? response("source denied", 503) : normal(url, init));
    render(<PersonalizationTab />);
    await screen.findByRole("button", { name: "重试读取指令来源" });
    expect(screen.getByRole("alert").textContent).toContain("source denied");
    expect(screen.queryByText("暂无指令文件。")).toBeNull();
    expect(screen.getByRole("textbox", { name: "自定义指令" })).toBeTruthy();
  });

  it("loads fresh source evidence after saving instead of sharing an old in-flight read", async () => {
    const old = deferred<Response>();
    let sourceRequests = 0;
    const normal = mocks.fetch.getMockImplementation()!;
    mocks.fetch.mockImplementation((url, init) => String(url).includes("guidelines")
      ? ++sourceRequests === 1 ? old.promise : Promise.resolve(response({ blocks: [source("C:/user/INSTRUCTIONS.md")] }))
      : normal(url, init));
    render(<PersonalizationTab />);
    const input = await screen.findByRole("textbox", { name: "自定义指令" });
    fireEvent.change(input, { target: { value: "new instructions" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findAllByText("C:/user/INSTRUCTIONS.md");
    await waitFor(() => expect(sourceRequests).toBe(2));
    await act(async () => old.resolve(response({ blocks: [] })));
    expect(screen.queryByText("暂无指令文件。")).toBeNull();
  });

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
