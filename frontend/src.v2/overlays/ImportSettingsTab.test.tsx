// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ImportSettingsTab } from "./ImportSettingsTab";

const send = vi.hoisted(() => vi.fn());
const runtime = vi.hoisted(() => {
  const value = { apiBaseUrl: "http://127.0.0.1:9187", wsBaseUrl: "ws://127.0.0.1:9187", runtimeToken: "isolated-import-runtime-token" };
  vi.stubGlobal("__MINICODE_RUNTIME__", value);
  return value;
});
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: send }));
const categories = ["sessions", "projects", "instructions", "settings", "skills", "plugins", "mcp", "agents", "hooks", "commands", "memory"];
const item = (id: string, type: string, supported = true, workspace = "") => ({ id, provider_id: "claude", item_type: type, title: id,
  source_path: `C:/source/.claude/${id}`, target_path: `C:/target/${id}`, workspace_root: workspace, supported, reason: supported ? "" : "Unsupported source hook type" });
const state = () => ({ providers: [{ id: "claude", name: "Claude Code" }, { id: "cursor", name: "Cursor" }], history: [], sync: { enabled: false, item_types: ["skills"] }, connected_provider_ids: [], item_types: categories });
let imported = false;
let syncEnabled = false;
let selection: string[] = [];
let connected = false;
let detectedItems: ReturnType<typeof item>[];

beforeEach(() => {
  imported = false; syncEnabled = false; selection = []; connected = false;
  detectedItems = [item("source-skill", "skills"), item("CLAUDE.md", "instructions"), item("legacy-hook", "hooks", false), item("project", "projects", true, "C:/project"), item("project-settings", "settings", true, "C:/project"), item("source-chat", "sessions", true, "C:/project")];
  vi.clearAllMocks();
  vi.stubGlobal("__MINICODE_RUNTIME__", runtime);
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  useAppStore.setState({ workingDirectory: "C:/project", recentWorkspaces: [], draft: "Keep the current chat draft", selectedMentions: [] });
  vi.stubGlobal("fetch", vi.fn(async (input, init) => {
    const headers = new Headers(init?.headers);
    if (headers.get("X-MiniCode-Token") !== runtime.runtimeToken) return new Response("Unauthorized", { status: 401 });
    if (init?.body !== undefined && headers.get("Content-Type") !== "application/json") return new Response("Expected JSON", { status: 415 });
    const url = new URL(String(input));
    const response = state();
    response.sync.enabled = syncEnabled;
    response.connected_provider_ids = connected ? ["claude"] : [] as never;
    if (imported) response.history = [{ id: "import-result", completed_at: "2026-10-19T00:00:00Z", automatic: false,
      results: detectedItems.filter(item => selection.includes(item.id)).map(item => ({ ...item, status: "success", message: "Imported actual content" })) }] as never;
    if (url.pathname.endsWith("/detect")) return Response.json({ detection_id: "detected-plan", items: imported ? [] : detectedItems, issues: [] });
    if (url.pathname.endsWith("/sync")) { const body = JSON.parse(init!.body as string); syncEnabled = body.enabled; response.sync = body; return Response.json(response); }
    if (init?.method === "POST") { selection = JSON.parse(init.body as string).item_ids; imported = true; connected = true; return Response.json({ results: detectedItems.filter(item => selection.includes(item.id)).map(item => ({ ...item, status: "success", message: "Imported actual content" })) }); }
    return Response.json(response);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("detects compatible local sources and sends only selected detected identities to the real import route", async () => {
  render(<ImportSettingsTab />);
  fireEvent.click(await screen.findByRole("button", { name: "导入 Claude Code" }));
  expect(screen.getByRole("dialog", { name: "从其他 AI 应用导入" })).toBeTruthy();
  expect(screen.getByRole("switch", { name: "Claude Code" }).getAttribute("aria-checked")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  const dialog = screen.getByRole("dialog", { name: "选择要导入的内容" });
  expect(within(dialog).queryByRole("checkbox", { name: "技能", exact: true })).toBeNull();
  fireEvent.click(within(dialog).getByRole("button", { name: "展开工具与配置详情" }));
  fireEvent.click(within(dialog).getByRole("checkbox", { name: "指令", exact: true }));
  fireEvent.click(within(dialog).getByRole("checkbox", { name: "项目", exact: true }));
  fireEvent.click(within(dialog).getByRole("checkbox", { name: "聊天会话", exact: true }));
  expect(within(dialog).getByRole("checkbox", { name: "Hook", exact: true })).toHaveProperty("disabled", true);
  fireEvent.focus(within(dialog).getByRole("button", { name: "将导入的内容 技能" }));
  expect(document.querySelector(".settings-import-tooltip")?.textContent).toContain("C:/source/.claude/source-skill → C:/target/source-skill");
  fireEvent.click(within(dialog).getByRole("button", { name: "导入", exact: true }));
  await waitFor(() => expect(selection).toEqual(["source-skill"]));
  await within(dialog).findByText("已导入 · source-skill");
  fireEvent.click(within(dialog.querySelector("footer")!).getByRole("button", { name: "关闭" }));
  await screen.findByText("已导入 1");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.queryByText("导出偏好")).toBeNull();
  expect(document.querySelector('input[type="file"]')).toBeNull();
  expect(useAppStore.getState().draft).toBe("Keep the current chat draft");
  expect(send).toHaveBeenCalledWith({ type: "conversation.list" }, { silent: true });
  expect(send).toHaveBeenCalledWith({ type: "skills.list" }, { silent: true });
  const post = vi.mocked(fetch).mock.calls.find(([url, init]) => new URL(String(url)).pathname === "/api/settings/import" && init?.method === "POST")!;
  expect(JSON.parse(post[1]!.body as string)).toEqual({ detection_id: "detected-plan", item_ids: ["source-skill"] });
  expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === "GET")).toBe(true);
  for (const [, init] of vi.mocked(fetch).mock.calls) expect(new Headers(init?.headers).get("X-MiniCode-Token")).toBe(runtime.runtimeToken);
});

it("keeps sync unavailable before a successful import and persists its actual consumer preferences afterwards", async () => {
  const view = render(<ImportSettingsTab />);
  await screen.findByRole("button", { name: "导入 Claude Code" });
  expect(screen.getByRole("switch", { name: "保持导入内容同步" })).toHaveProperty("disabled", true);
  connected = true;
  view.rerender(<ImportSettingsTab active={false} />);
  view.rerender(<ImportSettingsTab />);
  await waitFor(() => expect(screen.getByRole("switch", { name: "保持导入内容同步" })).toHaveProperty("disabled", false));
  fireEvent.click(screen.getByRole("switch", { name: "保持导入内容同步" }));
  await waitFor(() => expect(screen.getByRole("switch", { name: "保持导入内容同步" }).getAttribute("aria-checked")).toBe("true"));
  fireEvent.click(screen.getByRole("button", { name: "自定义" }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "内容" })).getByRole("checkbox", { name: "指令" }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "内容" })).getByRole("button", { name: "保存" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  const request = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes("/sync")).at(-1)!;
  expect(JSON.parse(request[1]!.body as string)).toEqual({ enabled: true, item_types: ["skills", "instructions"] });
});

it("does not claim a failed scan succeeded and retries the actual detection request", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(Response.json({ detail: "fixture scan error" }, { status: 500 }));
  render(<ImportSettingsTab />);
  expect((await screen.findByRole("alert")).textContent).toContain("fixture scan error");
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByRole("button", { name: "导入 Claude Code" });
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes("/detect"))).toHaveLength(2);
});

it("performs no hidden-page scan and ignores a retired workspace response", async () => {
  let resolve!: (response: Response) => void;
  const fetcher = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) => String(input).includes("/detect") && JSON.parse(init!.body as string).workspace_roots[0] === "C:/project"
    ? new Promise<Response>(done => { resolve = done; }) : fetcher(input, init));
  const view = render(<ImportSettingsTab active={false} />);
  expect(fetch).not.toHaveBeenCalled();
  view.rerender(<ImportSettingsTab />);
  await waitFor(() => expect(resolve).toBeDefined());
  act(() => useAppStore.setState({ workingDirectory: "C:/next-project" }));
  await screen.findByRole("button", { name: "导入 Claude Code" });
  await act(async () => resolve(Response.json({ detection_id: "retired", items: [item("old-workspace-only", "skills")], issues: [] })));
  fireEvent.click(screen.getByRole("button", { name: "导入 Claude Code" }));
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  fireEvent.click(screen.getByRole("button", { name: "展开工具与配置详情" }));
  expect(screen.queryByText("old-workspace-only")).toBeNull();
  expect(screen.getByRole("checkbox", { name: "技能" })).toBeTruthy();
});

it("opens provider selection from the active Import page and keeps each app connected to its own detected identities", async () => {
  const cursor = { ...item("cursor-settings", "settings"), provider_id: "cursor" };
  detectedItems.push(cursor);
  const view = render(<ImportSettingsTab active={false} />);
  expect(fetch).not.toHaveBeenCalled();
  view.rerender(<ImportSettingsTab />);
  fireEvent.click(await screen.findByRole("button", { name: "导入 Cursor" }));
  fireEvent.click(screen.getByRole("switch", { name: "Claude Code" }));
  fireEvent.click(await screen.findByRole("switch", { name: "Cursor" }));
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "工具与配置", exact: true }));
  fireEvent.click(screen.getByRole("checkbox", { name: "聊天会话", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "导入", exact: true }));
  await waitFor(() => expect(selection).toEqual(["project", "project-settings"]));
  expect(selection).not.toContain(cursor.id);
  expect(useAppStore.getState().workingDirectory).toBe("C:/project");
  expect(useAppStore.getState().draft).toBe("Keep the current chat draft");
  await within(screen.getByRole("dialog")).findByText("已导入 · project");
  fireEvent.click(within(screen.getByRole("dialog").querySelector("footer")!).getByRole("button", { name: "关闭" }));
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("cancels provider selection without importing or replacing the existing draft", async () => {
  render(<ImportSettingsTab />);
  fireEvent.click(await screen.findByRole("button", { name: "导入 Claude Code" }));
  fireEvent.click(screen.getByRole("switch", { name: "Claude Code" }));
  expect(screen.getByRole("button", { name: "继续" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(selection).toEqual([]);
  expect(useAppStore.getState().draft).toBe("Keep the current chat draft");
});

it("retains returned skipped and failed records in the dialog instead of reporting them as imports", async () => {
  const fetcher = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) => new URL(String(input)).pathname === "/api/settings/import" && init?.method === "POST"
    ? Promise.resolve(Response.json({ results: [{ ...item("source-skill", "skills"), status: "skipped", message: "Destination has local edits" }, { ...item("CLAUDE.md", "instructions"), status: "error", message: "Actual write failure" }] })) : fetcher(input, init));
  render(<ImportSettingsTab />);
  fireEvent.click(await screen.findByRole("button", { name: "导入 Claude Code" }));
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  fireEvent.click(screen.getByRole("button", { name: "导入", exact: true }));
  expect(await screen.findByText("已跳过 · source-skill")).toBeTruthy();
  expect(screen.getByText("导入失败 · CLAUDE.md")).toBeTruthy();
  expect(screen.getByText("Destination has local edits")).toBeTruthy();
  expect(screen.getByText("Actual write failure")).toBeTruthy();
  expect(screen.queryByText("已导入 · source-skill")).toBeNull();
});
