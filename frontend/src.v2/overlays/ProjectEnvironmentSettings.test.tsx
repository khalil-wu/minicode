/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ProjectEnvironmentSettings } from "./ProjectEnvironmentSettings";
import { showConfirm } from "./DialogService";
import { useAppStore } from "../stores";
import { bindWorkspaceOwnerRuntime } from "../protocol/workspace-owner";

vi.mock("./DialogService", () => ({ showConfirm: vi.fn(async () => true) }));
const root = "C:/project";
const status = (conversation = "one", registration = "original") => ({
  backend_python: "/target/python", python_version: "3.11", project_python: null, node: "/target/node",
  commands: { node_dependencies: ["/target/npm", "install"] }, display_commands: { node_dependencies: "/target/npm install" },
  session_id: "old-transport", conversation_id: conversation, environment_id: "target", registration_id: registration,
});
let session = "current-transport";
beforeEach(() => {
  session = "current-transport";
  useAppStore.setState({ workingDirectory: root, conversationId: "one" });
  bindWorkspaceOwnerRuntime(() => ({ session_id: session, conversation_id: useAppStore.getState().conversationId ?? "", workspace_root: root }));
  vi.mocked(showConfirm).mockResolvedValue(true);
  vi.stubGlobal("fetch", vi.fn(async (_url, init?: RequestInit) => new Response(init?.method === "POST"
    ? '{"output":"actual target output"}\n{"exit_code":0}\n' : JSON.stringify(status(useAppStore.getState().conversationId ?? "")))));
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); bindWorkspaceOwnerRuntime(() => undefined); });

it("sends the status registration and environment with the current transport session", async () => {
  render(<ProjectEnvironmentSettings workspaceRoot={root} />);
  await screen.findByText("/target/npm install");
  session = "reconnected-transport";
  fireEvent.click(screen.getByRole("button", { name: "运行" }));
  await screen.findByText(/actual target output/);
  const [url, init] = vi.mocked(fetch).mock.calls.find(([, options]) => options?.method === "POST")!;
  expect(new URL(String(url)).searchParams.get("environment_id")).toBe("target");
  expect(new URL(String(url)).searchParams.get("conversation_id")).toBe("one");
  expect(new URL(String(url)).searchParams.get("session_id")).toBe("reconnected-transport");
  expect(JSON.parse(String(init?.body))).toEqual({ action: "node_dependencies", expected_registration_id: "original" });
});

it("does not run a confirmed old action after the conversation changes at the same cwd", async () => {
  let confirm!: (value: boolean) => void;
  vi.mocked(showConfirm).mockImplementationOnce(() => new Promise((resolve) => { confirm = resolve; }));
  render(<ProjectEnvironmentSettings workspaceRoot={root} />);
  await screen.findByText("/target/npm install");
  fireEvent.click(screen.getByRole("button", { name: "运行" }));
  await waitFor(() => expect(showConfirm).toHaveBeenCalled());
  await act(async () => { useAppStore.setState({ conversationId: "two" }); });
  await screen.findByText("/target/npm install");
  await act(async () => { confirm(true); });
  expect(vi.mocked(fetch).mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
});

it("ignores a late status from another owner at the same cwd", async () => {
  let resolveOld!: (value: Response) => void;
  vi.mocked(fetch).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
  render(<ProjectEnvironmentSettings workspaceRoot={root} />);
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ ...status("two", "new"), backend_python: "/new-target/python" })));
  await act(async () => { useAppStore.setState({ conversationId: "two" }); });
  await screen.findByText("/new-target/python");
  await act(async () => { resolveOld(new Response(JSON.stringify({ ...status(), backend_python: "/old-target/python" }))); });
  expect(screen.queryByText("/old-target/python")).toBeNull();
  expect(screen.getByText("/new-target/python")).toBeTruthy();
});

it("aborts the old setup and does not append its late stream to the new owner", async () => {
  let stream!: ReadableStreamDefaultController<Uint8Array>;
  let signal: AbortSignal | null | undefined;
  vi.mocked(fetch).mockImplementation(async (_url, init) => {
    if (init?.method !== "POST") return new Response(JSON.stringify(status(useAppStore.getState().conversationId ?? "")));
    signal = init.signal;
    return new Response(new ReadableStream({ start(controller) { stream = controller; } }));
  });
  render(<ProjectEnvironmentSettings workspaceRoot={root} />);
  await screen.findByText("/target/npm install");
  fireEvent.click(screen.getByRole("button", { name: "运行" }));
  await waitFor(() => expect(signal).toBeTruthy());
  await act(async () => { stream.enqueue(new TextEncoder().encode('{"output":"old output"}\n')); });
  await screen.findByText("old output");
  await act(async () => { useAppStore.setState({ conversationId: "two" }); });
  await screen.findByText("/target/npm install");
  expect(signal?.aborted).toBe(true);
  await act(async () => { stream.enqueue(new TextEncoder().encode('{"output":"late old output"}\n{"exit_code":0}\n')); stream.close(); });
  expect(screen.queryByText(/old output/)).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});
