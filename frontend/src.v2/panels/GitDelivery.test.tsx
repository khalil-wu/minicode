/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GitDelivery } from "./GitDelivery";
import { useAppStore } from "../stores";
import { showConfirm } from "../overlays/DialogService";

vi.mock("../overlays/DialogService", () => ({ showConfirm: vi.fn(async () => false) }));
const localStatus = { is_git_repo: true, branch: "feature", repo_root: "C:/project", remotes: ["origin", "backup"], upstream: "origin/feature", commits: [], detached: false };
const github = { eligible: true, available: true, authenticated: false, host: "github.com", message: "尚未连接。" };
beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() });
  useAppStore.setState({ workingDirectory: "C:/project", settingsOpen: false, settingsTab: "general" });
  vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => new Response(JSON.stringify(
    init?.method === "POST" ? { message: "已提交。" } : { ...localStatus, github },
  ))));
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });
const renderDelivery = () => render(<GitDelivery workspaceRoot="C:/project" stagedCount={2} onChanged={() => {}} />);
const expandPR = () => fireEvent.click(screen.getByText("创建草稿 PR", { selector: "summary" }));

it("keeps local commits and push available while GitHub authorization gates draft PR", async () => {
  renderDelivery();
  await screen.findByText("连接 GitHub 后可创建草稿 PR。");
  fireEvent.change(screen.getByRole("textbox", { name: "提交说明" }), { target: { value: "Fix editor" } });
  expect((screen.getByRole("button", { name: "提交已暂存的 2 个文件" }) as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(screen.getByText("分支与推送", { selector: "summary" }));
  expect((screen.getByRole("button", { name: "推送" }) as HTMLButtonElement).disabled).toBe(false);
  expandPR();
  expect((screen.getByRole("textbox", { name: "PR 标题" }) as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "创建草稿 PR" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "查看 GitHub 连接设置" }));
  expect(useAppStore.getState().settingsTab).toBe("workspaceGit");
  expect(useAppStore.getState().settingsOpen).toBe(true);
  expect(showConfirm).not.toHaveBeenCalled();
  expect(vi.mocked(fetch).mock.calls.every(([, init]) => init?.method !== "POST")).toBe(true);
});

it.each([
  { state: { ...github, eligible: false, message: "当前仓库使用 GitLab 远端。" }, explanation: "当前仓库使用 GitLab 远端。" },
  { state: { ...github, authenticated: null, message: "GitHub 连接状态待确认。" }, explanation: "GitHub 连接状态待确认。" },
  { state: { ...github, available: false }, explanation: "GitHub 连接组件未就绪。" },
])("explains unsupported or unconfirmed GitHub state before draft entry: $explanation", async ({ state, explanation }) => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ ...localStatus, github: state })));
  renderDelivery();
  await screen.findByText(explanation);
  expandPR();
  expect((screen.getByRole("textbox", { name: "PR 标题" }) as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "创建草稿 PR" }) as HTMLButtonElement).disabled).toBe(true);
});

it("uses the shared remote picker and preserves the selected push destination", async () => {
  renderDelivery();
  await screen.findByText("连接 GitHub 后可创建草稿 PR。");
  fireEvent.click(screen.getByText("分支与推送", { selector: "summary" }));
  fireEvent.click(screen.getByRole("button", { name: "推送远端，当前：origin" }));
  fireEvent.click(screen.getByRole("option", { name: "backup" }));
  expect(screen.getByRole("button", { name: "推送远端，当前：backup" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "推送" }));
  await waitFor(() => expect(showConfirm).toHaveBeenCalledWith(expect.objectContaining({ message: "将 feature 推送到 backup。" })));
  expect(vi.mocked(fetch).mock.calls.every(([, init]) => init?.method !== "POST")).toBe(true);
});

it("requires the original confirmation even when GitHub is ready for a draft PR", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ ...localStatus, github: { ...github, authenticated: true } })));
  renderDelivery();
  await waitFor(() => expect(screen.queryByText("正在读取 GitHub 连接状态…")).toBeNull());
  expandPR();
  fireEvent.change(screen.getByRole("textbox", { name: "PR 标题" }), { target: { value: "Improve editor" } });
  expect((screen.getByRole("button", { name: "创建草稿 PR" }) as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "创建草稿 PR" }));
  await waitFor(() => expect(showConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: "创建草稿 PR", message: "从 feature 向 main 创建草稿 PR：\nImprove editor" })));
  expect(vi.mocked(fetch).mock.calls.every(([, init]) => init?.method !== "POST")).toBe(true);
});
