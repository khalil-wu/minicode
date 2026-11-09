/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { HooksTab } from "./HooksTab";
import { useAppStore } from "../stores";

const mocks = vi.hoisted(() => ({ fetch: vi.fn() }));
vi.mock("../protocol/api", async original => ({ ...await original<typeof import("../protocol/api")>(),
  apiBase: () => "http://hooks.test", authHeaders: () => ({}), fetchWithTimeout: mocks.fetch }));

const hook = { key: "user:pre_tool_use:0", event: "pre_tool_use", handler_type: "command", matcher: "*", command: "node hook.js",
  prompt: null, url: null, source_path: "C:/settings.json", source: "user", plugin_id: null, enabled: true, is_managed: false,
  current_hash: "trusted-hash", trust_status: "trusted", timeout_seconds: 30 };
const snapshot = (enabled = true) => ({ hooks: [{ ...hook, enabled }], warnings: [] });
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const deferred = () => { let resolve!: (value: Response) => void; const promise = new Promise<Response>(done => { resolve = done; }); return { promise, resolve }; };
const refresh = () => screen.getByRole("button", { name: "重新加载钩子" });
const toggle = () => screen.getByRole("switch", { name: "pre_tool_use: node hook.js" });

beforeEach(() => {
  mocks.fetch.mockReset();
  useAppStore.setState({ draft: "Keep authored draft", workingDirectory: "C:/project-A" });
});
afterEach(cleanup);

it.each([false, true])("keeps committed hook state after a prior refresh settles (read fails=%s)", async failRead => {
  const oldRead = deferred();
  let reads = 0;
  mocks.fetch.mockImplementation(async (_url, init) => init?.method === "PUT" ? response(snapshot(false))
    : ++reads === 1 ? response(snapshot()) : oldRead.promise);
  const view = render(<HooksTab />);
  await screen.findByRole("switch");
  const details = view.container.querySelector("details")!;
  details.open = true;
  fireEvent.click(refresh());
  const signal = mocks.fetch.mock.calls[1][1].signal as AbortSignal;
  expect(refresh()).toHaveProperty("disabled", true);
  fireEvent.click(toggle());
  expect(signal.aborted).toBe(true);
  await waitFor(() => expect(toggle().getAttribute("aria-checked")).toBe("false"));
  await act(async () => oldRead.resolve(failRead ? response({ detail: "Obsolete refresh failure" }, 503) : response(snapshot())));
  expect(toggle().getAttribute("aria-checked")).toBe("false");
  expect(toggle()).toHaveProperty("disabled", false);
  expect(refresh()).toHaveProperty("disabled", false);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(view.container.querySelector("details")).toBe(details);
  expect(details.open).toBe(true);
  expect(useAppStore.getState().draft).toBe("Keep authored draft");
  expect(mocks.fetch).toHaveBeenCalledTimes(3);
  expect(JSON.parse(mocks.fetch.mock.calls[2][1].body)).toEqual({ workspace_root: "", updates: [{ key: hook.key, enabled: false }] });
});

it.each([false, true])("retires the read started when a pending save's hidden page reopens (read fails=%s)", async failRead => {
  const write = deferred(), reopenedRead = deferred();
  let reads = 0;
  mocks.fetch.mockImplementation(async (_url, init) => init?.method === "PUT" ? write.promise
    : ++reads === 1 ? response(snapshot()) : reopenedRead.promise);
  const view = render(<HooksTab />);
  await screen.findByRole("switch");
  fireEvent.click(toggle());
  view.rerender(<HooksTab active={false} />);
  act(() => useAppStore.setState({ workingDirectory: "C:/project-B" }));
  expect(mocks.fetch).toHaveBeenCalledTimes(2);
  view.rerender(<HooksTab active />);
  expect(mocks.fetch).toHaveBeenCalledTimes(3);
  expect(toggle()).toHaveProperty("disabled", true);
  const signal = mocks.fetch.mock.calls[2][1].signal as AbortSignal;
  await act(async () => write.resolve(response(snapshot(false))));
  expect(signal.aborted).toBe(true);
  await act(async () => reopenedRead.resolve(failRead ? response({ detail: "Old reopened read failed" }, 503) : response(snapshot())));
  expect(toggle().getAttribute("aria-checked")).toBe("false");
  expect(toggle()).toHaveProperty("disabled", false);
  expect(refresh()).toHaveProperty("disabled", false);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(useAppStore.getState().draft).toBe("Keep authored draft");
});

it("retains an actual write failure and permits a fresh read after cancelling overlapping refresh work", async () => {
  const oldRead = deferred();
  let reads = 0;
  mocks.fetch.mockImplementation(async (_url, init) => init?.method === "PUT" ? response({ detail: "Hook config is read-only" }, 403)
    : ++reads === 2 ? oldRead.promise : response(snapshot()));
  render(<HooksTab />);
  await screen.findByRole("switch");
  fireEvent.click(refresh());
  fireEvent.click(toggle());
  expect((await screen.findByRole("alert")).textContent).toBe("Error: Hook config is read-only");
  await act(async () => oldRead.resolve(response({ detail: "Obsolete read error" }, 503)));
  expect(screen.getByRole("alert").textContent).toBe("Error: Hook config is read-only");
  expect(toggle().getAttribute("aria-checked")).toBe("true");
  expect(refresh()).toHaveProperty("disabled", false);
  fireEvent.click(refresh());
  await waitFor(() => expect(refresh()).toHaveProperty("disabled", false));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(mocks.fetch.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
});

it.each([false, true])("settles a reopened read error with the subsequent write outcome (write fails=%s)", async failWrite => {
  const write = deferred(), reopenedRead = deferred();
  let reads = 0;
  mocks.fetch.mockImplementation(async (_url, init) => init?.method === "PUT" ? write.promise
    : ++reads === 1 ? response(snapshot()) : reopenedRead.promise);
  const view = render(<HooksTab />);
  await screen.findByRole("switch");
  fireEvent.click(toggle());
  view.rerender(<HooksTab active={false} />);
  view.rerender(<HooksTab active />);
  await act(async () => reopenedRead.resolve(response({ detail: "Refresh failed during save" }, 503)));
  expect(screen.getByRole("alert").textContent).toBe("Error: Refresh failed during save");
  expect(toggle()).toHaveProperty("disabled", true);
  await act(async () => write.resolve(failWrite ? response({ detail: "Hook config write failed" }, 403) : response(snapshot(false))));
  if (failWrite) expect(screen.getByRole("alert").textContent).toBe("Error: Hook config write failed");
  else expect(screen.queryByRole("alert")).toBeNull();
  expect(toggle().getAttribute("aria-checked")).toBe(String(failWrite));
  expect(toggle()).toHaveProperty("disabled", false);
  expect(refresh()).toHaveProperty("disabled", false);
  expect(useAppStore.getState().draft).toBe("Keep authored draft");
});

it("keeps hidden pages idle and ignores their late load while reading afresh on return", async () => {
  const oldRead = deferred();
  mocks.fetch.mockResolvedValueOnce(response(snapshot())).mockReturnValueOnce(oldRead.promise).mockResolvedValue(response(snapshot(false)));
  const view = render(<HooksTab active={false} />);
  expect(mocks.fetch).not.toHaveBeenCalled();
  view.rerender(<HooksTab active />);
  await screen.findByRole("switch");
  fireEvent.click(refresh());
  const signal = mocks.fetch.mock.calls[1][1].signal as AbortSignal;
  view.rerender(<HooksTab active={false} />);
  expect(signal.aborted).toBe(true);
  await act(async () => oldRead.resolve(response(snapshot(false))));
  expect(toggle().getAttribute("aria-checked")).toBe("true");
  view.rerender(<HooksTab active />);
  await waitFor(() => expect(toggle().getAttribute("aria-checked")).toBe("false"));
  expect(mocks.fetch).toHaveBeenCalledTimes(3);
});
