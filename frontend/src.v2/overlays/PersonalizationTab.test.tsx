/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PersonalizationTab } from "./PersonalizationTab";
import { useAppStore } from "../stores";

const { confirm, command, toast } = vi.hoisted(() => ({ confirm: vi.fn(), command: vi.fn(), toast: vi.fn() }));
vi.mock("./DialogService", () => ({ showConfirm: confirm }));
vi.mock("./ToastContainer", () => ({ pushToast: toast }));
vi.mock("../protocol/api", async importOriginal => ({
  ...await importOriginal<typeof import("../protocol/api")>(),
  apiBase: () => "http://personalization.test",
  authHeaders: () => ({}),
  fetchWithTimeout: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, init),
}));
vi.mock("../protocol/ws-outbox", async importOriginal => ({
  ...await importOriginal<typeof import("../protocol/ws-outbox")>(),
  sendClientCommandAwaitResult: command,
}));

const instructions = (contents: string) => new Response(JSON.stringify({
  instructions: contents, path: "C:/owned-config/INSTRUCTIONS.md", exists: true, max_bytes: 32768,
}), { headers: { "content-type": "application/json" } });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  confirm.mockReset();
  command.mockReset();
  toast.mockClear();
  useAppStore.setState({ conversationId: null, conversations: [] });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("opens local instructions as a detail page and returns only after the explicit save succeeds", async () => {
  const saved = deferred<Response>();
  const fetcher = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => Promise.resolve(
    init?.method === "PUT" ? saved.promise : instructions("original instruction"),
  ));
  vi.stubGlobal("fetch", fetcher);
  render(<PersonalizationTab />);
  expect(screen.queryByRole("textbox", { name: "自定义指令" })).toBeNull();
  expect(fetcher).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "编辑本机指令" }));
  const editor = await screen.findByRole("textbox", { name: "自定义指令" });
  fireEvent.change(editor, { target: { value: "edited instruction" } });
  fireEvent.keyDown(editor, { key: "s", ctrlKey: true });
  expect(screen.getByRole("button", { name: "保存中" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "返回" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "取消" })).toHaveProperty("disabled", true);
  expect(editor).toHaveProperty("disabled", true);
  const put = fetcher.mock.calls.find(([, init]) => init?.method === "PUT");
  expect(JSON.parse(String(put?.[1]?.body))).toEqual({ instructions: "edited instruction" });
  await act(async () => { saved.resolve(instructions("edited instruction")); });
  expect(await screen.findByRole("button", { name: "编辑本机指令" })).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "自定义指令" })).toBeNull();
});

it("keeps a failed instruction draft across hidden settings and conversation changes until explicit cancel", async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method === "PUT"
    ? new Response("The configuration volume is read-only", { status: 503 }) : instructions("original instruction"));
  vi.stubGlobal("fetch", fetcher);
  const view = render(<PersonalizationTab />);
  fireEvent.click(screen.getByRole("button", { name: "编辑本机指令" }));
  const editor = await screen.findByRole("textbox", { name: "自定义指令" });
  fireEvent.change(editor, { target: { value: "keep this unsaved instruction" } });
  fireEvent.click(screen.getByRole("button", { name: "保存", exact: true }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("read-only"));
  view.rerender(<PersonalizationTab active={false} />);
  act(() => useAppStore.setState({ conversationId: "another-task" }));
  view.rerender(<PersonalizationTab active />);
  expect(screen.getByRole("textbox", { name: "自定义指令" })).toHaveProperty("value", "keep this unsaved instruction");
  expect(fetcher.mock.calls.filter(([, init]) => !init?.method)).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(confirm).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "编辑本机指令" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "自定义指令" })).toHaveProperty("value", "original instruction"));
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
});

it("confirms Back before discarding and keeps an edited draft when a newer file read completes", async () => {
  const refreshed = deferred<Response>();
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(() => ++reads === 2 ? refreshed.promise : Promise.resolve(instructions("original instruction"))));
  render(<PersonalizationTab />);
  fireEvent.click(screen.getByRole("button", { name: "编辑本机指令" }));
  await screen.findByRole("textbox", { name: "自定义指令" });
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  expect(confirm).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "编辑本机指令" }));
  fireEvent.change(screen.getByRole("textbox", { name: "自定义指令" }), { target: { value: "current draft" } });
  await act(async () => { refreshed.resolve(instructions("changed on disk")); });
  expect(screen.getByRole("textbox", { name: "自定义指令" })).toHaveProperty("value", "current draft");
  confirm.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  expect(screen.getByRole("textbox", { name: "自定义指令" })).toHaveProperty("value", "current draft");
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  expect(await screen.findByRole("button", { name: "编辑本机指令" })).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "自定义指令" })).toBeNull();
});

it("retains memory isolation causes and binds pending operations and real errors to their original task", async () => {
  const result = deferred<{ level: string; message: string }>();
  command.mockReturnValueOnce(result.promise);
  confirm.mockResolvedValueOnce(true);
  useAppStore.setState({ conversationId: "isolated-task", conversations: [
    { id: "isolated-task", title: "Isolated", updatedAt: "2026-10-18", memoryMode: "polluted", memoryPolluted: true,
      memoryPollutionSources: ["web_search", "mcp__github__search_issues"] },
    { id: "other-task", title: "Other", updatedAt: "2026-10-18", memoryMode: "disabled", memoryPolluted: false },
  ] });
  render(<PersonalizationTab />);
  expect(screen.getByText("mcp__github__search_issues")).toBeTruthy();
  expect(screen.getByRole("switch", { name: "长期记忆生成" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "重新启用" }));
  await waitFor(() => expect(command).toHaveBeenCalledWith({
    type: "conversation.memory_mode.set", conversation_id: "isolated-task", memory_mode: "enabled",
  }, "conversation.memory_mode.set"));
  act(() => useAppStore.setState({ conversationId: "other-task" }));
  expect(screen.getByRole("switch", { name: "长期记忆生成" })).toHaveProperty("disabled", false);
  act(() => useAppStore.setState({ conversationId: "isolated-task" }));
  expect(screen.getByRole("button", { name: "恢复中" })).toHaveProperty("disabled", true);
  act(() => useAppStore.setState({ conversationId: "other-task" }));
  await act(async () => { result.resolve({ level: "error", message: "memory permission denied" }); });
  expect(screen.queryByRole("alert")).toBeNull();
  act(() => useAppStore.setState({ conversationId: "isolated-task" }));
  expect(screen.getByRole("alert")).toHaveProperty("textContent", "memory permission denied");
  expect(screen.getByRole("button", { name: "重新启用" })).toHaveProperty("disabled", false);
});
