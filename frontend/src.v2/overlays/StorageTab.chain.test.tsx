// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { StorageTab } from "./StorageTab";

const session = vi.hoisted(() => ({ id: "storage-review-session" }));
const runtime = vi.hoisted(() => {
  const value = { apiBaseUrl: "http://127.0.0.1:9187", wsBaseUrl: "ws://127.0.0.1:9187", runtimeToken: "storage-fixture-token" };
  vi.stubGlobal("__MINICODE_RUNTIME__", value);
  return value;
});
const confirm = vi.hoisted(() => vi.fn(async () => false));
const command = vi.hoisted(() => vi.fn());
const toast = vi.hoisted(() => ({ push: vi.fn(() => "storage-pending-toast"), dismiss: vi.fn() }));
vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => ({ sessionId: session.id }) }));
vi.mock("./DialogService", () => ({ showConfirm: confirm }));
vi.mock("./ToastContainer", () => ({ pushToast: toast.push, dismissToast: toast.dismiss }));
vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommandAwaitResult: (...args: unknown[]) => command(...args),
  commandResultSucceeded: (event: { level?: string }) => event.level !== "error",
  LONG_COMMAND_RESULT_TIMEOUT_MS: 300_000,
}));

const usage = { path: "C:/storage-fixture", total_bytes: 10_000, categories: [
  { name: "chats", bytes: 3000 }, { name: "archived", bytes: 4000 }, { name: "subagents", bytes: 1000 },
  { name: "unclassified", bytes: 0 }, { name: "worktrees", bytes: 0 }, { name: "other", bytes: 2000 },
] };
const review = () => {
  const chat = (id: string, age: number, archived: boolean, size: number, descendants: number) => ({
    id, title: id, archived, bytes: size, subagent_count: descendants,
    updated_at: new Date(Date.now() - age * 86_400_000).toISOString(), snapshot: { revision: 7, runs: [], subagents: [] },
  });
  const inventory = [chat("Archived parent", 200, true, 1234, 2), chat("Unarchived parent", 200, false, 2048, 0), chat("Recent archive", 50, true, 3456, 1)];
  return { cutoff: new Date(Date.now() - 90 * 86_400_000).toISOString(), days: 90, archived_only: true,
    chats: inventory.slice(0, 1), inventory };
};

beforeEach(() => {
  vi.clearAllMocks();
  confirm.mockResolvedValue(false);
  command.mockResolvedValue({ type: "command.result", level: "info", data: { deleted: true } });
  vi.stubGlobal("__MINICODE_RUNTIME__", runtime);
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  useAppStore.setState({ isConnected: true, draft: "Keep this unsent draft", conversationId: "current-chat" });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    expect(new Headers(init?.headers).get("X-MiniCode-Token")).toBe("storage-fixture-token");
    const path = new URL(String(input)).pathname;
    if (path === "/api/settings/storage") return Response.json(usage);
    if (path === "/api/settings/storage/review") return Response.json(review());
    throw new Error(`Unexpected storage fixture request: ${input}`);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const readyReview = async () => {
  const button = screen.getByRole("button", { name: "查看" });
  await waitFor(() => expect(button).toHaveProperty("disabled", false));
  button.focus(); fireEvent.click(button);
  return screen.findByRole("dialog", { name: "查看旧聊天" });
};

it("loads a complete inventory before opening, projects real tree bytes, and filters without another request", async () => {
  const fetcher = vi.mocked(fetch).getMockImplementation()!;
  let finishUsage!: (value: Response) => void;
  let finishReview!: (value: Response) => void;
  vi.mocked(fetch).mockImplementation((input, init) => new URL(String(input)).pathname.endsWith("/review")
    ? new Promise(resolve => { finishReview = resolve; })
    : new Promise(resolve => { finishUsage = resolve; }));
  render(<StorageTab />);
  const open = screen.getByRole("button", { name: "查看" });
  expect(open).toHaveProperty("disabled", true);
  await act(async () => finishUsage(Response.json(usage)));
  open.focus(); fireEvent.click(open);
  expect(open.getAttribute("aria-busy")).toBe("true");
  expect(screen.queryByRole("dialog")).toBeNull();
  await act(async () => finishReview(Response.json(review())));
  const dialog = screen.getByRole("dialog", { name: "查看旧聊天" });
  expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: "取消" }));
  expect(within(dialog).getByRole("checkbox", { name: "Archived parent" })).toHaveProperty("checked", true);
  expect(within(dialog).getByText(/已选聊天：1 · 子智能体：2/)).toBeTruthy();
  const size = new Intl.NumberFormat("zh-CN", { style: "unit", unit: "kilobyte", unitDisplay: "short", maximumFractionDigits: 1 }).format(1234 / 1024);
  expect(within(dialog).getByText(size)).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("checkbox", { name: "Archived parent" }));
  expect(within(dialog).getByRole("button", { name: "删除所选" })).toHaveProperty("disabled", true);
  fireEvent.click(within(dialog).getByRole("button", { name: /^包含，当前/ }));
  fireEvent.click(screen.getByRole("option", { name: "所有聊天" }));
  expect(within(dialog).getAllByRole("checkbox")).toHaveLength(2);
  expect(within(dialog).getAllByRole("checkbox").every(box => (box as HTMLInputElement).checked)).toBe(true);
  fireEvent.click(within(dialog).getByRole("button", { name: /^最后更新，当前/ }));
  fireEvent.click(screen.getByRole("option", { name: "超过 30 天" }));
  expect(within(dialog).getAllByRole("checkbox")).toHaveLength(3);
  expect(vi.mocked(fetch).mock.calls.filter(([input]) => String(input).includes("/review"))).toHaveLength(1);
  fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog")).toBeNull(); expect(document.activeElement).toBe(open);
  expect(useAppStore.getState().draft).toBe("Keep this unsent draft");
  vi.mocked(fetch).mockImplementation(fetcher);
});

it("drops an inventory response after leaving the page and opens only the next active read", async () => {
  const fetcher = vi.mocked(fetch).getMockImplementation()!;
  let finish!: (value: Response) => void;
  let signal!: AbortSignal;
  vi.mocked(fetch).mockImplementation((input, init) => new URL(String(input)).pathname.endsWith("/review")
    ? new Promise(resolve => { finish = resolve; signal = init!.signal as AbortSignal; }) : fetcher(input, init));
  const view = render(<StorageTab />);
  await waitFor(() => expect(screen.getByRole("button", { name: "查看" })).toHaveProperty("disabled", false));
  fireEvent.click(screen.getByRole("button", { name: "查看" }));
  view.rerender(<StorageTab active={false} />);
  expect(signal.aborted).toBe(true);
  await act(async () => finish(Response.json(review())));
  expect(screen.queryByRole("dialog")).toBeNull();
  vi.mocked(fetch).mockImplementation(fetcher);
  view.rerender(<StorageTab />);
  expect(await readyReview()).toBeTruthy();
});

it("keeps a failed inventory closed and supports an actual retry and Close focus restoration", async () => {
  const fetcher = vi.mocked(fetch).getMockImplementation()!;
  let failed = false;
  vi.mocked(fetch).mockImplementation((input, init) => {
    if (!failed && new URL(String(input)).pathname.endsWith("/review")) { failed = true; return Promise.resolve(new Response("Original inventory read failure", { status: 409 })); }
    return fetcher(input, init);
  });
  render(<StorageTab />);
  const open = screen.getByRole("button", { name: "查看" });
  await waitFor(() => expect(open).toHaveProperty("disabled", false));
  fireEvent.click(open);
  expect((await screen.findByRole("alert")).textContent).toContain("Original inventory read failure");
  expect(screen.queryByRole("dialog")).toBeNull();
  const dialog = await readyReview();
  fireEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
  expect(screen.queryByRole("dialog")).toBeNull(); expect(document.activeElement).toBe(open);
});

it("closes on confirmed deletion, keeps the real snapshot contract, and reports changed chats as skipped", async () => {
  confirm.mockResolvedValue(true);
  let finish!: (value: unknown) => void;
  command.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  render(<StorageTab />);
  const dialog = await readyReview();
  fireEvent.click(within(dialog).getByRole("button", { name: "删除所选" }));
  await waitFor(() => expect(command).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("button", { name: "查看" })).toHaveProperty("disabled", true);
  expect(command.mock.calls[0][0]).toMatchObject({ type: "conversation.delete", conversation_id: "Archived parent", storage_review: { revision: 7 }, client_resource_cleanup: false });
  expect(toast.push).toHaveBeenCalledWith("正在删除聊天…", "info", 0);
  await act(async () => finish({ type: "command.result", level: "warning", data: { deleted: false, reason: "storage_review_changed" } }));
  await waitFor(() => expect(screen.getByRole("button", { name: "查看" })).toHaveProperty("disabled", false));
  expect(toast.dismiss).toHaveBeenCalledWith("storage-pending-toast");
  expect(toast.push).toHaveBeenCalledWith("已删除：0。已变化并跳过：1。未完成：0。", "success");
});
