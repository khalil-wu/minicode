// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ImportSettingsTab } from "./ImportSettingsTab";
import { subscribeExtensionSettingsChanges } from "../lib/extension-settings-events";

const send = vi.hoisted(() => vi.fn());
const openTarget = vi.hoisted(() => vi.fn(async () => true as const));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(), openManagedSettingsTarget: openTarget }));
const runtime = vi.hoisted(() => {
  const value = { apiBaseUrl: "http://127.0.0.1:9187", wsBaseUrl: "ws://127.0.0.1:9187", runtimeToken: "isolated-import-runtime-token" };
  vi.stubGlobal("__MINICODE_RUNTIME__", value);
  return value;
});
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: send }));
const categories = ["sessions", "projects", "instructions", "settings", "skills", "plugins", "mcp", "agents", "hooks", "commands", "memory"];
const item = (id: string, type: string, supported = true, workspace = "") => ({ id, provider_id: "claude", item_type: type, title: id,
  source_path: `C:/source/.claude/${id}`, target_path: `C:/target/${id}`, workspace_root: workspace, supported, reason: supported ? "" : "Unsupported source hook type" });
const receipt = (results: Array<ReturnType<typeof item> & { status: string; message: string }>, workspace: string, id = "import-result") => ({ results,
  history: { id, completed_at: "2026-10-22T00:00:00Z", automatic: false, workspace_root: workspace, results } });
const state = () => ({ providers: [{ id: "claude", name: "Claude Code" }, { id: "cursor", name: "Cursor" }], history: [], sync: { enabled: false, item_types: ["skills"] }, connected_provider_ids: [], item_types: categories, running_import: null, import_progress: null });
let imported = false;
let syncEnabled = false;
let selection: string[] = [];
let connected = false;
let operationWorkspace: string | undefined;
let detectedItems: ReturnType<typeof item>[];

beforeEach(() => {
  imported = false; syncEnabled = false; selection = []; connected = false;
  operationWorkspace = undefined;
  detectedItems = [item("source-skill", "skills"), item("CLAUDE.md", "instructions"), item("legacy-hook", "hooks", false), item("project", "projects", true, "C:/project"), item("project-settings", "settings", true, "C:/project"), item("source-chat", "sessions", true, "C:/project")];
  vi.clearAllMocks();
  openTarget.mockResolvedValue(true);
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
    if (imported) response.history = [{ id: "import-result", completed_at: "2026-10-19T00:00:00Z", automatic: false, workspace_root: operationWorkspace,
      results: detectedItems.filter(item => selection.includes(item.id)).map(item => ({ ...item, status: "success", message: "Imported actual content" })) }] as never;
    if (url.pathname.endsWith("/detect")) return Response.json({ detection_id: "detected-plan", items: imported ? [] : detectedItems, issues: [] });
    if (url.pathname.endsWith("/sync")) { const body = JSON.parse(init!.body as string); syncEnabled = body.enabled; response.sync = body; return Response.json(response); }
    if (init?.method === "POST") { const body = JSON.parse(init.body as string); selection = body.item_ids; operationWorkspace = body.workspace_root;
      imported = true; connected = true; return Response.json(receipt(detectedItems.filter(item => selection.includes(item.id)).map(item => ({ ...item, status: "success", message: "Imported actual content" })), operationWorkspace!)); }
    return Response.json(response);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("opens the General row's real two-step import and keeps no-data and hidden states truthful", async () => {
  const view = render(<ImportSettingsTab variant="general-row" active={false} />);
  expect(fetch).not.toHaveBeenCalled();
  view.rerender(<ImportSettingsTab variant="general-row" />);
  fireEvent.click(await screen.findByRole("button", { name: "导入", exact: true }));
  expect(screen.getByRole("dialog", { name: "从其他 AI 应用导入" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  expect(screen.getByRole("dialog", { name: "选择要导入的内容" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(selection).toEqual([]);
  expect(useAppStore.getState().draft).toBe("Keep the current chat draft");
  await waitFor(() => expect(screen.getByRole("button", { name: "导入", exact: true })).toHaveProperty("disabled", false));
  detectedItems = [];
  view.rerender(<ImportSettingsTab variant="general-row" active={false} />);
  view.rerender(<ImportSettingsTab variant="general-row" />);
  expect(await screen.findByRole("button", { name: "未检测到数据" })).toHaveProperty("disabled", true);
});

it.each([
  ["skills", "success", 1, 0],
  ["plugins", "success", 1, 1],
  ["skills", "skipped", 0, 0],
  ["plugins", "error", 0, 0],
  ["instructions", "success", 0, 0],
])("invalidates only committed extension kinds from the actual import receipt (%s/%s)", async (kind, status, skillEvents, pluginEvents) => {
  const selected = item("selected-source", String(kind));
  detectedItems = [selected];
  useAppStore.setState({ activeConversationDraftId: null });
  const originalFetch = fetch;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
    new URL(String(input)).pathname === "/api/settings/import" && init?.method === "POST"
      ? Promise.resolve(Response.json(receipt([{ ...selected, status: String(status), message: "Actual import outcome" }], "C:/project")))
      : originalFetch(input, init)));
  const skillsChanged = vi.fn(), pluginsChanged = vi.fn();
  const stopSkills = subscribeExtensionSettingsChanges("skills", {}, skillsChanged);
  const stopPlugins = subscribeExtensionSettingsChanges("plugins", {}, pluginsChanged);
  try {
    render(<ImportSettingsTab />);
    fireEvent.click(await screen.findByRole("button", { name: "从 Claude Code 导入" }));
    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "选择要导入的内容" })).getByRole("button", { name: "导入", exact: true }));
    await screen.findByText("Actual import outcome");
    expect(skillsChanged).toHaveBeenCalledTimes(Number(skillEvents));
    expect(pluginsChanged).toHaveBeenCalledTimes(Number(pluginEvents));
    expect(useAppStore.getState().draft).toBe("Keep the current chat draft");
  } finally { stopSkills(); stopPlugins(); }
});

it("shows actual completed result files and opens only a successful known file through the existing file action", async () => {
  imported = true; selection = ["CLAUDE.md"];
  useAppStore.setState({ settingsOpen: true });
  render(<ImportSettingsTab variant="general-row" />);
  fireEvent.click(await screen.findByRole("button", { name: "查看导入的文件" }));
  const dialog = screen.getByRole("dialog", { name: "已导入外部智能体配置" });
  const path = within(dialog).getByRole("link", { name: "C:/target/CLAUDE.md" });
  expect(within(dialog).getByText("用户配置")).toBeTruthy();
  act(() => useAppStore.setState({ workingDirectory: "C:/history-viewer" }));
  fireEvent.click(path);
  expect(openTarget).toHaveBeenCalledExactlyOnceWith({ kind: "import-item", importId: "import-result", itemId: "CLAUDE.md", workspaceRoot: "C:/history-viewer" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, workingDirectory: "C:/history-viewer", draft: "Keep the current chat draft" });
});

it("keeps skipped and failed history visible without labelling it as an imported setup or offering nonexistent files", async () => {
  imported = true;
  const fetcher = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const response = await fetcher(input, init);
    if (new URL(String(input)).pathname !== "/api/settings/import" || init?.method !== "GET") return response;
    const current = await response.json();
    current.history = [{ id: "failed-import", completed_at: "2026-10-22T00:00:00Z", automatic: false, results: [
      { ...item("CLAUDE.md", "instructions"), status: "skipped", message: "Destination contains local changes" },
      { ...item("failed-command.md", "commands"), status: "error", message: "Actual write error" },
    ] }];
    return Response.json(current);
  });
  render(<ImportSettingsTab variant="general-row" />);
  fireEvent.click(await screen.findByRole("button", { name: "查看导入的文件" }));
  const dialog = screen.getByRole("dialog", { name: "导入失败" });
  expect(screen.queryByText("已导入智能体配置")).toBeNull();
  expect(within(dialog).getByText("已跳过")).toBeTruthy();
  expect(within(dialog).getByText("Destination contains local changes")).toBeTruthy();
  expect(within(dialog).getByText("Actual write error")).toBeTruthy();
  expect(within(dialog).queryByRole("link")).toBeNull();
  expect(openTarget).not.toHaveBeenCalled();
});

it("detects compatible local sources and sends only selected detected identities to the real import route", async () => {
  render(<ImportSettingsTab />);
  fireEvent.click(await screen.findByRole("button", { name: "从 Claude Code 导入" }));
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
  const completed = await screen.findByRole("dialog", { name: "已导入外部智能体配置" });
  expect(within(completed).getByText("source-skill")).toBeTruthy();
  expect(within(completed).getByLabelText("已导入")).toBeTruthy();
  fireEvent.click(within(completed).getByRole("button", { name: "确定" }));
  await screen.findByText("已导入 1");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.queryByText("导出偏好")).toBeNull();
  expect(document.querySelector('input[type="file"]')).toBeNull();
  expect(useAppStore.getState().draft).toBe("Keep the current chat draft");
  expect(send).toHaveBeenCalledWith({ type: "conversation.list" }, { silent: true });
  expect(send).toHaveBeenCalledWith({ type: "skills.list" }, { silent: true });
  const post = vi.mocked(fetch).mock.calls.find(([url, init]) => new URL(String(url)).pathname === "/api/settings/import" && init?.method === "POST")!;
  expect(JSON.parse(post[1]!.body as string)).toEqual({ detection_id: "detected-plan", item_ids: ["source-skill"], workspace_root: "C:/project" });
  expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === "GET")).toBe(true);
  for (const [, init] of vi.mocked(fetch).mock.calls) expect(new Headers(init?.headers).get("X-MiniCode-Token")).toBe(runtime.runtimeToken);
});

it("projects detected provider identity below automatic sync and keeps its real import action", async () => {
  render(<ImportSettingsTab />);
  const importAction = await screen.findByRole("button", { name: "从 Claude Code 导入" });
  const syncHeading = screen.getByRole("heading", { name: "自动同步" });
  const importHeading = screen.getByRole("heading", { name: "从其他 AI 应用导入" });
  expect(syncHeading.compareDocumentPosition(importHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.getByText("同步已暂停，内容选择会保留")).toBeTruthy();
  const row = importAction.closest(".settings-row")!;
  expect(row.querySelector('[data-provider="claude"] svg')?.getAttribute("width")).toBe("40");
  expect(within(row as HTMLElement).getByText("Claude Code")).toBeTruthy();
  fireEvent.click(importAction);
  expect(screen.getByRole("dialog", { name: "从其他 AI 应用导入" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(selection).toEqual([]);
  expect(useAppStore.getState().draft).toBe("Keep the current chat draft");
});

it("keeps sync unavailable before a successful import and persists its actual consumer preferences afterwards", async () => {
  const view = render(<ImportSettingsTab />);
  await screen.findByRole("button", { name: "从 Claude Code 导入" });
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
  await screen.findByRole("button", { name: "从 Claude Code 导入" });
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
  await screen.findByRole("button", { name: "从 Claude Code 导入" });
  await act(async () => resolve(Response.json({ detection_id: "retired", items: [item("old-workspace-only", "skills")], issues: [] })));
  fireEvent.click(screen.getByRole("button", { name: "从 Claude Code 导入" }));
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
  fireEvent.click(await screen.findByRole("button", { name: "从 Cursor 导入" }));
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
  const completed = await screen.findByRole("dialog", { name: "已导入外部智能体配置" });
  expect(within(completed).getByText("project")).toBeTruthy();
  fireEvent.click(within(completed).getByRole("button", { name: "确定" }));
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("cancels provider selection without importing or replacing the existing draft", async () => {
  render(<ImportSettingsTab />);
  fireEvent.click(await screen.findByRole("button", { name: "从 Claude Code 导入" }));
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
    ? Promise.resolve(Response.json(receipt([{ ...item("source-skill", "skills"), status: "skipped", message: "Destination has local edits" }, { ...item("CLAUDE.md", "instructions"), status: "error", message: "Actual write failure" }], "C:/project"))) : fetcher(input, init));
  render(<ImportSettingsTab />);
  fireEvent.click(await screen.findByRole("button", { name: "从 Claude Code 导入" }));
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  fireEvent.click(screen.getByRole("button", { name: "导入", exact: true }));
  const completed = await screen.findByRole("dialog", { name: "导入失败" });
  expect(within(completed).getByText("source-skill")).toBeTruthy();
  expect(within(completed).getByText("CLAUDE.md")).toBeTruthy();
  expect(within(completed).getByText("已跳过")).toBeTruthy();
  expect(completed.querySelector('.settings-import-file-status[data-status="error"]')).toBeTruthy();
  expect(screen.getByText("Destination has local edits")).toBeTruthy();
  expect(screen.getByText("Actual write failure")).toBeTruthy();
  expect(completed.querySelector('.settings-import-file-status[data-status="success"]')).toBeNull();
});

it("replaces selection with the Source progress dialog for the actual request and blocks dismissal until its receipt", async () => {
  useAppStore.setState({ settingsOpen: true });
  const fetcher = vi.mocked(fetch).getMockImplementation()!;
  let release!: (response: Response) => void;
  vi.mocked(fetch).mockImplementation((input, init) => new URL(String(input)).pathname === "/api/settings/import" && init?.method === "POST"
    ? new Promise<Response>(resolve => { release = resolve; }) : fetcher(input, init));
  render(<ImportSettingsTab variant="general-row" />);
  fireEvent.click(await screen.findByRole("button", { name: "导入", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "选择要导入的内容" })).getByRole("button", { name: "导入", exact: true }));
  const pending = screen.getByRole("dialog", { name: "正在导入外部智能体配置" });
  expect(screen.queryByRole("dialog", { name: "选择要导入的内容" })).toBeNull();
  expect(within(pending).queryByRole("button")).toBeNull();
  expect(within(pending).queryByRole("link")).toBeNull();
  expect(screen.queryByText(/在后台继续|查看导入内容/)).toBeNull();
  const paths = [...pending.querySelectorAll(".settings-import-file-path")].map(element => element.textContent);
  expect(paths).toContain("C:/target/project-settings");
  fireEvent.keyDown(pending, { key: "Escape" });
  fireEvent.click(pending.closest(".mc-project-dialog-backdrop")!);
  expect(screen.getByRole("dialog")).toBe(pending);
  expect(vi.mocked(fetch).mock.calls.filter(([url, init]) => init?.method === "POST" && new URL(String(url)).pathname === "/api/settings/import")).toHaveLength(1);
  act(() => useAppStore.setState({ workingDirectory: "C:/next-project" }));
  await act(async () => release(Response.json(receipt([{ ...item("project-settings", "settings", true, "C:/project"), status: "success", message: "Actual project settings copied" }], "C:/project", "actual-post-import-id"))));
  const completed = await screen.findByRole("dialog", { name: "已导入外部智能体配置" });
  expect(completed).toBe(pending);
  expect(within(completed).getByText("C:/project")).toBeTruthy();
  expect(within(completed).queryByText("C:/next-project")).toBeNull();
  expect(within(completed).getByRole("link").textContent).toBe("C:/target/project-settings");
  openTarget.mockRejectedValueOnce(new Error("The original editor workspace is no longer active."));
  fireEvent.click(within(completed).getByRole("link"));
  expect(openTarget).toHaveBeenCalledExactlyOnceWith({ kind: "import-item", importId: "actual-post-import-id", itemId: "project-settings", workspaceRoot: "C:/project" });
  expect((await within(completed).findByRole("alert")).textContent).toBe("The original editor workspace is no longer active.");
  expect(useAppStore.getState().settingsOpen).toBe(true);
  fireEvent.click(within(completed).getByRole("button", { name: "确定" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(useAppStore.getState()).toMatchObject({ workingDirectory: "C:/next-project", draft: "Keep the current chat draft" });
});

it("opens a fast import using the actual POST history identity without waiting for a progress poll", async () => {
  useAppStore.setState({ settingsOpen: true });
  render(<ImportSettingsTab />);
  fireEvent.click(await screen.findByRole("button", { name: "从 Claude Code 导入" }));
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "选择要导入的内容" })).getByRole("button", { name: "导入", exact: true }));
  const completed = await screen.findByRole("dialog", { name: "已导入外部智能体配置" });
  fireEvent.click(within(completed).getByRole("link", { name: "C:/target/CLAUDE.md" }));
  expect(openTarget).toHaveBeenCalledExactlyOnceWith({ kind: "import-item", importId: "import-result", itemId: "CLAUDE.md", workspaceRoot: "C:/project" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(useAppStore.getState()).toMatchObject({ settingsOpen: false, workingDirectory: "C:/project", draft: "Keep the current chat draft" });
});

it("keeps the successful import dialog and original open error until the registered opener actually succeeds", async () => {
  imported = true; selection = ["CLAUDE.md"]; operationWorkspace = "C:/project";
  useAppStore.setState({ settingsOpen: true });
  let reject!: (reason: Error) => void;
  openTarget.mockImplementationOnce(() => new Promise<true>((_resolve, fail) => { reject = fail; }));
  render(<ImportSettingsTab variant="general-row" />);
  fireEvent.click(await screen.findByRole("button", { name: "查看导入的文件" }));
  const completed = screen.getByRole("dialog", { name: "已导入外部智能体配置" });
  fireEvent.click(within(completed).getByRole("link"));
  expect(screen.getByRole("dialog")).toBe(completed);
  expect(useAppStore.getState().settingsOpen).toBe(true);
  await act(async () => reject(new Error("Registered imported file was removed from disk.")));
  expect((await within(completed).findByRole("alert")).textContent).toBe("Registered imported file was removed from disk.");
  expect(screen.getByRole("dialog")).toBe(completed);
  expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, workingDirectory: "C:/project", draft: "Keep the current chat draft" });
  fireEvent.click(within(completed).getByRole("link"));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(useAppStore.getState().settingsOpen).toBe(false);
});

it("retains a restored import snapshot's real operation workspace and identity for home-file links", async () => {
  const fetcher = vi.mocked(fetch).getMockImplementation()!;
  const importedItem = item("restored-home.md", "instructions");
  let remote: Record<string, unknown> = { id: "restored-operation", detection_id: "restored-detection", workspace_root: "C:/original-operation",
    status: "running", items: [importedItem], results: [] };
  vi.mocked(fetch).mockImplementation((input, init) => new URL(String(input)).pathname === "/api/settings/import" && init?.method === "GET"
    ? Promise.resolve(Response.json({ ...state(), import_progress: remote })) : fetcher(input, init));
  useAppStore.setState({ workingDirectory: "C:/current-viewer", settingsOpen: true });
  const view = render(<ImportSettingsTab variant="general-row" />);
  await screen.findByRole("dialog", { name: "正在导入外部智能体配置" });
  remote = { ...remote, status: "success", results: [{ ...importedItem, status: "success", message: "Actual restored import" }] };
  view.rerender(<ImportSettingsTab variant="general-row" active={false} />);
  view.rerender(<ImportSettingsTab variant="general-row" />);
  const completed = await screen.findByRole("dialog", { name: "已导入外部智能体配置" });
  openTarget.mockRejectedValueOnce(new Error("Return to the original operation workspace."));
  fireEvent.click(within(completed).getByRole("link"));
  expect(openTarget).toHaveBeenCalledExactlyOnceWith({ kind: "import-item", importId: "restored-operation", itemId: importedItem.id, workspaceRoot: "C:/original-operation" });
  expect((await within(completed).findByRole("alert")).textContent).toBe("Return to the original operation workspace.");
  expect(useAppStore.getState()).toMatchObject({ settingsOpen: true, workingDirectory: "C:/current-viewer", draft: "Keep the current chat draft" });
});

it("retains the original request failure without fabricating per-item outcomes or a success action", async () => {
  const fetcher = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((input, init) => new URL(String(input)).pathname === "/api/settings/import" && init?.method === "POST"
    ? Promise.resolve(Response.json({ detail: "Original admission rejected" }, { status: 409 })) : fetcher(input, init));
  render(<ImportSettingsTab variant="general-row" />);
  fireEvent.click(await screen.findByRole("button", { name: "导入", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
  fireEvent.click(within(screen.getByRole("dialog", { name: "选择要导入的内容" })).getByRole("button", { name: "导入", exact: true }));
  const completed = await screen.findByRole("dialog", { name: "导入失败" });
  expect(within(completed).getByRole("alert").textContent).toContain("Original admission rejected");
  expect(screen.getAllByRole("alert")).toHaveLength(1);
  expect(completed.querySelector(".settings-import-file-status")).toBeNull();
  expect(within(completed).queryByRole("button")).toBeNull();
  expect(within(completed).queryByRole("link")).toBeNull();
  fireEvent.keyDown(completed, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(useAppStore.getState().draft).toBe("Keep the current chat draft");
});

it("restores actual running import items before detection completes and follows its own terminal snapshot", async () => {
  const fetcher = vi.mocked(fetch).getMockImplementation()!;
  const actualItem = item("other-window-skill", "skills", true, "C:/original-project");
  let remote: Record<string, unknown> = { id: "original-import", detection_id: "original-detection", status: "running", items: [actualItem], results: [] };
  let releaseDetection!: (response: Response) => void;
  let holdDetection = true;
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith("/detect") && holdDetection) return new Promise<Response>(resolve => { releaseDetection = resolve; });
    if (path === "/api/settings/import" && init?.method === "GET") return Response.json({ ...state(),
      running_import: remote.status === "running" ? { id: remote.id, item_types: ["skills"] } : null, import_progress: remote });
    return fetcher(input, init);
  });
  const view = render(<ImportSettingsTab variant="general-row" />);
  const pending = await screen.findByRole("dialog", { name: "正在导入外部智能体配置" });
  expect(within(pending).getByText("other-window-skill")).toBeTruthy();
  await act(async () => releaseDetection(Response.json({ detection_id: "current-detection", items: detectedItems, issues: [] })));
  holdDetection = false;
  remote = { ...remote, status: "error", results: [{ ...actualItem, status: "skipped", message: "Preserved actual local edits" }], setup_errors: ["Original refresh error"] };
  view.rerender(<ImportSettingsTab variant="general-row" active={false} />);
  view.rerender(<ImportSettingsTab variant="general-row" />);
  const completed = await screen.findByRole("dialog", { name: "导入失败" });
  expect(within(completed).getByText("已跳过")).toBeTruthy();
  expect(within(completed).getByRole("alert").textContent).toBe("Original refresh error");
  expect(within(completed).queryByRole("link")).toBeNull();
  fireEvent.click(completed.closest(".mc-project-dialog-backdrop")!);
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});
