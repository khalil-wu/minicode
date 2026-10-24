/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ confirm: vi.fn(), request: vi.fn(), toast: vi.fn(), list: vi.fn() }));
vi.mock("./DialogService", () => ({ showConfirm: mocks.confirm }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("../protocol/memory-settings", () => ({ currentPersonalizationHost: () => ({ id: "local", displayName: "Local", kind: "local" }),
  listPersonalizationHosts: mocks.list, personalizationRequest: mocks.request }));
import { PersonalizationTab } from "./PersonalizationTab";
import { useAppStore } from "../stores";

const memory = (enabled = true, disable = false) => ({ feature_enabled: enabled, memories: { generate_memories: enabled, use_memories: enabled, disable_on_external_context: disable }, path: "/config/settings.json", feature_locked: false, locked_fields: [] });
const instructions = (contents: string) => ({ instructions: contents, path: "C:/owned-config/INSTRUCTIONS.md", exists: true, max_bytes: 32768 });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
const selectRemote = async () => { fireEvent.click(screen.getByRole("button", { name: "选择记忆主机" })); fireEvent.click(await screen.findByRole("menuitemradio", { name: "Lab host" })); };

beforeEach(() => {
  mocks.confirm.mockReset(); mocks.toast.mockReset();
  mocks.list.mockReset().mockResolvedValue([{ id: "local", displayName: "Local", kind: "local" }, { id: "ssh-lab", displayName: "Lab host", kind: "ssh" }]);
  mocks.request.mockReset().mockImplementation(async (_host, action, payload) => action === "read-memory" ? memory()
    : action === "update-memory" ? memory(payload.enabled ?? true, payload.disable_on_external_context ?? false)
    : action === "read-instructions" ? instructions("original instruction") : action === "update-instructions" ? instructions(payload.instructions) : {});
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  useAppStore.setState({ conversationId: null, conversations: [], workingDirectory: "C:/home", draft: "unsent chat draft" });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("saves selected-host memory preferences without rewriting the current task mode", async () => {
  useAppStore.setState({ conversationId: "task", conversations: [{ id: "task", title: "Task", updatedAt: "", memoryMode: "polluted", memoryPolluted: true }] });
  render(<PersonalizationTab />);
  const toggle = await screen.findByRole("switch", { name: "启用 MiniCode 记忆" });
  expect(toggle.getAttribute("aria-checked")).toBe("true");
  fireEvent.click(toggle);
  await waitFor(() => expect(mocks.request).toHaveBeenCalledWith("local", "update-memory", { enabled: false }));
  expect(useAppStore.getState().conversations[0]).toMatchObject({ memoryMode: "polluted", memoryPolluted: true });
  expect(screen.queryByText("外部上下文已隔离")).toBeNull();
  expect(screen.queryByRole("button", { name: "重新启用" })).toBeNull();
});

it("uses the inverse external-context setting and keeps it disabled while the feature is off", async () => {
  mocks.request.mockImplementation(async (_host, action, payload) => action === "read-memory" ? memory(true, true) : memory(true, payload.disable_on_external_context));
  render(<PersonalizationTab />);
  const external = await screen.findByRole("switch", { name: "允许从使用工具的聊天中生成记忆" });
  expect(external.getAttribute("aria-checked")).toBe("false"); fireEvent.click(external);
  await waitFor(() => expect(mocks.request).toHaveBeenCalledWith("local", "update-memory", { disable_on_external_context: false }));
});

it("binds pending writes to the selected host across host and conversation changes", async () => {
  const saved = deferred<ReturnType<typeof memory>>();
  mocks.request.mockImplementation(async (_host, action) => action === "update-memory" ? saved.promise : memory());
  render(<PersonalizationTab />);
  fireEvent.click(await screen.findByRole("switch", { name: "启用 MiniCode 记忆" }));
  await selectRemote();
  await waitFor(() => expect(mocks.request).toHaveBeenCalledWith("ssh-lab", "read-memory"));
  act(() => useAppStore.setState({ conversationId: "other-task", workingDirectory: "C:/other", draft: "other authored draft" }));
  await act(async () => saved.resolve(memory(false)));
  expect(screen.getByRole("switch", { name: "启用 MiniCode 记忆" }).getAttribute("aria-checked")).toBe("true");
  expect(mocks.request.mock.calls.filter(call => call[1] === "update-memory")).toEqual([["local", "update-memory", { enabled: false }]]);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "other-task", workingDirectory: "C:/other", draft: "other authored draft" });
});

it("keeps feature and generate/use values separate in the displayed master switch", async () => {
  const payload = memory(false); payload.memories.generate_memories = true; payload.memories.use_memories = true;
  mocks.request.mockResolvedValue(payload);
  render(<PersonalizationTab />);
  expect((await screen.findByRole("switch", { name: "启用 MiniCode 记忆" })).getAttribute("aria-checked")).toBe("false");
  expect(screen.getByRole("switch", { name: "允许从使用工具的聊天中生成记忆" })).toHaveProperty("disabled", true);
});

it("opens selected-host instructions as a detail page and returns only after save succeeds", async () => {
  const saved = deferred<ReturnType<typeof instructions>>();
  mocks.request.mockImplementation(async (_host, action) => action === "read-memory" ? memory() : action === "update-instructions" ? saved.promise : instructions("original instruction"));
  render(<PersonalizationTab />);
  fireEvent.click(screen.getByRole("button", { name: "编辑所选主机指令" }));
  const editor = await screen.findByRole("textbox", { name: "自定义指令" });
  fireEvent.change(editor, { target: { value: "edited instruction" } }); fireEvent.keyDown(editor, { key: "s", ctrlKey: true });
  expect(screen.getByRole("button", { name: "保存中" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "返回" })).toHaveProperty("disabled", true);
  expect(mocks.request).toHaveBeenCalledWith("local", "update-instructions", { instructions: "edited instruction" });
  await act(async () => saved.resolve(instructions("edited instruction")));
  expect(await screen.findByRole("button", { name: "编辑所选主机指令" })).toBeTruthy();
});

it("keeps a failed instruction draft across hidden settings and task changes until explicit cancel", async () => {
  mocks.request.mockImplementation(async (_host, action) => { if (action === "update-instructions") throw new Error("read-only config"); return action === "read-memory" ? memory() : instructions("original instruction"); });
  const view = render(<PersonalizationTab />);
  fireEvent.click(screen.getByRole("button", { name: "编辑所选主机指令" }));
  fireEvent.change(await screen.findByRole("textbox", { name: "自定义指令" }), { target: { value: "keep unsaved instruction" } });
  fireEvent.click(screen.getByRole("button", { name: "保存", exact: true }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("read-only"));
  view.rerender(<PersonalizationTab active={false} />); act(() => useAppStore.setState({ conversationId: "another-task" })); view.rerender(<PersonalizationTab active />);
  expect(screen.getByRole("textbox", { name: "自定义指令" })).toHaveProperty("value", "keep unsaved instruction");
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(mocks.confirm).not.toHaveBeenCalled();
});

it("confirms deletion for the captured host without sending conversation memory-mode commands", async () => {
  mocks.confirm.mockResolvedValue(true);
  render(<PersonalizationTab />); await selectRemote();
  fireEvent.click(await screen.findByRole("button", { name: "删除", exact: true }));
  await waitFor(() => expect(mocks.request).toHaveBeenCalledWith("ssh-lab", "reset-memory", { confirmed: true }));
  expect(mocks.confirm).toHaveBeenCalledWith(expect.objectContaining({ message: "这会删除 Lab host 上的全部 MiniCode 记忆。" }));
  expect(mocks.request.mock.calls.some(call => String(call[1]).includes("memory_mode"))).toBe(false);
});

it("replaces stale controls with the actual host read error and retries the same owner", async () => {
  const view = render(<PersonalizationTab />);
  await screen.findByRole("switch", { name: "启用 MiniCode 记忆" });
  mocks.request.mockRejectedValueOnce(new Error("Selected host disconnected"));
  view.rerender(<PersonalizationTab active={false} />); view.rerender(<PersonalizationTab active />);
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("Selected host disconnected"));
  expect(screen.queryByRole("switch", { name: "启用 MiniCode 记忆" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByRole("switch", { name: "启用 MiniCode 记忆" });
  expect(mocks.request.mock.calls.at(-1)).toEqual(["local", "read-memory"]);
});
