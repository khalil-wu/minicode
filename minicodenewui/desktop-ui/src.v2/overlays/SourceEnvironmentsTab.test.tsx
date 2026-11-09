/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
const { fetchSettings } = vi.hoisted(() => ({ fetchSettings: vi.fn() }));
vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../protocol/api", async original => ({ ...await original<typeof import("../protocol/api")>(), apiBase: () => "http://environment.test", authHeaders: () => ({}), fetchWithTimeout: fetchSettings }));
import { SourceEnvironmentsTab } from "./SourceEnvironmentsTab";
import { useAppStore } from "../stores";

const root = "C:/project";
const path = `${root}/.minicode/environments/environment.toml`;
let saved: { config_path: string; revision: string; environment: Record<string, unknown> | null };
let saveFailure = false;
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

beforeEach(() => {
  vi.clearAllMocks();
  saveFailure = false;
  saved = { config_path: path, revision: "empty-revision", environment: null };
  useAppStore.setState({ workingDirectory: root, recentWorkspaces: [], projectEditTarget: null, draft: "untouched conversation draft", attachments: [] });
  fetchSettings.mockImplementation(async (input, init) => {
    const url = new URL(input);
    if (init?.method === "PUT") {
      if (url.pathname.endsWith("/selection")) return response({ selected_config_path: path });
      if (saveFailure) return response({ detail: "File changed outside MiniCode. Draft kept." }, 409);
      const body = JSON.parse(init.body);
      saved = { config_path: body.config_path, revision: "saved-revision", environment: body.environment };
      return response(saved);
    }
    if (url.searchParams.has("config_path")) return response(saved);
    if (url.searchParams.has("workspace_root")) return response({ workspace_root: root, configs: saved.environment ? [saved] : [], selected_config_path: saved.environment ? path : "", default_config_path: path, worktree_path: "" });
    return response({ projects: [{ path: root, name: "App" }] });
  });
});
afterEach(cleanup);

async function createEnvironment() {
  fireEvent.click(await screen.findByRole("button", { name: "创建项目环境 App" }));
  await screen.findByRole("heading", { name: "编辑本地环境" });
}

it("opens the Source project creation chain and saves all script platforms, action metadata and the real selection", async () => {
  render(<SourceEnvironmentsTab />);
  await createEnvironment();
  fireEvent.change(screen.getByRole("textbox", { name: "设置脚本 · default" }), { target: { value: "echo source setup" } });
  fireEvent.click(within(screen.getByRole("tablist", { name: "设置脚本" })).getByRole("tab", { name: "Windows" }));
  fireEvent.change(screen.getByRole("textbox", { name: "设置脚本 · win32" }), { target: { value: "Write-Output windows" } });
  fireEvent.click(screen.getByRole("button", { name: "变量" }));
  expect(within(screen.getByRole("dialog", { name: "设置脚本环境变量" })).getByText("CODEX_WORKTREE_PATH")).toBeTruthy();
  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.click(screen.getByRole("button", { name: "添加操作" }));
  fireEvent.change(screen.getByRole("textbox", { name: "操作名称 1" }), { target: { value: "Tests" } });
  expect(screen.getByRole("button", { name: "保存" })).toHaveProperty("disabled", true);
  fireEvent.change(screen.getByRole("textbox", { name: "操作脚本 1" }), { target: { value: "npm test" } });
  fireEvent.click(screen.getByRole("button", { name: "操作图标 1" }));
  fireEvent.click(screen.getByRole("menuitemradio", { name: "测试" }));
  fireEvent.click(within(screen.getByRole("group", { name: "操作平台 1" })).getByRole("button", { name: "Windows" }));
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(fetchSettings.mock.calls.some(([url, init]) => new URL(url).pathname.endsWith("/selection") && init?.method === "PUT")).toBe(true));
  const write = fetchSettings.mock.calls.find(([url, init]) => new URL(url).pathname === "/api/settings/local-environments" && init?.method === "PUT")!;
  expect(JSON.parse(write[1].body)).toMatchObject({ workspace_root: root, config_path: path, expected_revision: "empty-revision", environment: {
    setup: { script: "echo source setup", win32: { script: "Write-Output windows" } }, actions: [{ name: "Tests", command: "npm test", icon: "test", platform: "win32" }],
  } });
  expect(useAppStore.getState().draft).toBe("untouched conversation draft");
});

it("keeps the form draft across hidden Settings and an authoritative file conflict", async () => {
  const view = render(<SourceEnvironmentsTab />);
  await createEnvironment();
  const input = screen.getByRole("textbox", { name: "设置脚本 · default" });
  fireEvent.change(input, { target: { value: "unsaved setup contents" } });
  view.rerender(<SourceEnvironmentsTab active={false} />);
  const calls = fetchSettings.mock.calls.length;
  await act(async () => {});
  expect(fetchSettings).toHaveBeenCalledTimes(calls);
  view.rerender(<SourceEnvironmentsTab active />);
  expect(screen.getByRole("textbox", { name: "设置脚本 · default" })).toHaveProperty("value", "unsaved setup contents");
  saveFailure = true;
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Error: File changed outside MiniCode. Draft kept.");
  expect(screen.getByRole("textbox", { name: "设置脚本 · default" })).toHaveProperty("value", "unsaved setup contents");
  expect(fetchSettings.mock.calls.some(([url, init]) => new URL(url).pathname.endsWith("/selection") && init?.method === "PUT")).toBe(false);
});

it("shows the real project load before its empty state and opens project creation without changing drafts", async () => {
  let complete!: (response: Response) => void;
  fetchSettings.mockReturnValueOnce(new Promise<Response>(resolve => { complete = resolve; }));
  render(<SourceEnvironmentsTab />);
  expect(screen.getByRole("status").textContent).toContain("正在加载项目…");
  await act(async () => complete(response({ projects: [] })));
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.getByText("尚无项目。添加项目以配置本地环境。")).toBeTruthy();
  fireEvent.click(screen.getAllByRole("button", { name: "添加项目" })[1]);
  expect(useAppStore.getState().projectEditTarget).toBe("create");
  expect(useAppStore.getState().draft).toBe("untouched conversation draft");
});

it("retries an actual project-list failure without making the error into an empty list", async () => {
  fetchSettings.mockRejectedValueOnce(new Error("Project registry unavailable"));
  render(<SourceEnvironmentsTab />);
  expect((await screen.findByRole("alert")).textContent).toContain("Error: Project registry unavailable");
  expect(screen.getByText("无法加载项目")).toBeTruthy();
  expect(screen.getByText("Error: Project registry unavailable")).toBeTruthy();
  expect(screen.queryByText("尚无项目。添加项目以配置本地环境。")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  await screen.findByRole("button", { name: "创建项目环境 App" });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(fetchSettings).toHaveBeenCalledTimes(2);
});
