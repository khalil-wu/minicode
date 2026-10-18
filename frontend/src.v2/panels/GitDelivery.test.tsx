/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { GitDelivery } from "./GitDelivery";
import { useAppStore } from "../stores";
import { showConfirm } from "../overlays/DialogService";
import { bindWorkspaceOwnerRuntime } from "../protocol/workspace-owner";

vi.mock("../overlays/DialogService", () => ({ showConfirm: vi.fn(async () => false) }));
const localStatus = { is_git_repo: true, branch: "feature", repo_root: "C:/project", remotes: ["origin", "backup"], upstream: "origin/feature", commits: [], detached: false,
  preferences: { branch_prefix: "codex/", always_force_push: false, create_draft_pull_requests: true, pull_request_merge_method: "merge", commit_instructions: "", pull_request_instructions: "" } };
const github = { eligible: true, available: true, authenticated: false, host: "github.com", message: "尚未连接。" };
beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() });
  useAppStore.setState({ workingDirectory: "C:/project", conversationId: null, settingsOpen: false, settingsTab: "general" });
  vi.stubGlobal("fetch", vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => new Response(JSON.stringify(
    init?.method === "POST" ? { message: "已提交。" } : { ...localStatus, github },
  ))));
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); bindWorkspaceOwnerRuntime(() => undefined); });
const renderDelivery = () => render(<GitDelivery workspaceRoot="C:/project" stagedCount={2} onChanged={() => {}} />);
const expandPR = () => fireEvent.click(screen.getByText("创建草稿 PR", { selector: "summary" }));

it("loads only while active and retains a hidden commit draft", async () => {
  const { rerender } = render(<GitDelivery workspaceRoot="C:/project" active={false} stagedCount={2} onChanged={() => {}} />);
  expect(fetch).not.toHaveBeenCalled();
  rerender(<GitDelivery workspaceRoot="C:/project" active stagedCount={2} onChanged={() => {}} />);
  await screen.findByText("连接 GitHub 后可创建草稿 PR。");
  fireEvent.change(screen.getByRole("textbox", { name: "提交说明" }), { target: { value: "Keep the commit draft" } });
  rerender(<GitDelivery workspaceRoot="C:/project" active={false} stagedCount={2} onChanged={() => {}} refreshKey={1} />);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect((screen.getByRole("textbox", { name: "提交说明" }) as HTMLTextAreaElement).value).toBe("Keep the commit draft");
  rerender(<GitDelivery workspaceRoot="C:/project" active stagedCount={2} onChanged={() => {}} refreshKey={1} />);
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  expect((screen.getByRole("textbox", { name: "提交说明" }) as HTMLTextAreaElement).value).toBe("Keep the commit draft");
});

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

const targetStatus = (conversation = "one", registration = "original") => ({ ...localStatus,
  session_id: "old-session", conversation_id: conversation, environment_id: "target", registration_id: registration,
  github: { ...github, remote: true, message: "请在所选设备连接 GitHub 后刷新。" } });
function targetOwner() {
  useAppStore.setState({ conversationId: "one" });
  bindWorkspaceOwnerRuntime(() => ({ session_id: "current-session", conversation_id: useAppStore.getState().conversationId ?? "", workspace_root: "C:/project" }));
}

it("keeps target GitHub authorization on the target device", async () => {
  targetOwner();
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(targetStatus())));
  renderDelivery();
  await screen.findByText("请在所选设备连接 GitHub 后刷新。");
  expect(screen.queryByRole("button", { name: "查看 GitHub 连接设置" })).toBeNull();
});

it("posts the accepted target registration and environment", async () => {
  targetOwner();
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify(targetStatus())));
  const changed = vi.fn();
  render(<GitDelivery workspaceRoot="C:/project" stagedCount={2} onChanged={changed} />);
  await screen.findByText("请在所选设备连接 GitHub 后刷新。");
  fireEvent.change(screen.getByRole("textbox", { name: "提交说明" }), { target: { value: "目标提交\n\n真实正文" } });
  fireEvent.click(screen.getByRole("button", { name: "提交已暂存的 2 个文件" }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  const [url, init] = vi.mocked(fetch).mock.calls.find(([, options]) => options?.method === "POST")!;
  expect(new URL(String(url)).searchParams.get("environment_id")).toBe("target");
  expect(new URL(String(url)).searchParams.get("conversation_id")).toBe("one");
  expect(new URL(String(url)).searchParams.get("session_id")).toBe("current-session");
  expect(JSON.parse(String(init?.body))).toMatchObject({ message: "目标提交\n\n真实正文", expected_registration_id: "original", expected_branch: "feature" });
});

it("does not push after confirmation switches to another conversation at the same cwd", async () => {
  targetOwner();
  let confirm!: (value: boolean) => void;
  vi.mocked(showConfirm).mockImplementationOnce(() => new Promise((resolve) => { confirm = resolve; }));
  vi.mocked(fetch).mockImplementation(async (_url, init) => new Response(JSON.stringify(init?.method === "POST" ? { message: "pushed" } : targetStatus(useAppStore.getState().conversationId ?? ""))));
  renderDelivery();
  await screen.findByText("请在所选设备连接 GitHub 后刷新。");
  fireEvent.click(screen.getByText("分支与推送", { selector: "summary" }));
  fireEvent.click(screen.getByRole("button", { name: "推送" }));
  await waitFor(() => expect(showConfirm).toHaveBeenCalled());
  await act(async () => { useAppStore.setState({ conversationId: "two" }); });
  await screen.findByText("请在所选设备连接 GitHub 后刷新。");
  await act(async () => { confirm(true); });
  expect(vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
});

it("does not clear a new owner's draft when an old commit response arrives", async () => {
  targetOwner();
  let finish!: (value: Response) => void;
  let signal: AbortSignal | null | undefined;
  vi.mocked(fetch).mockImplementation(async (_url, init) => {
    if (init?.method === "POST") { signal = init.signal; return new Promise((resolve) => { finish = resolve; }); }
    return new Response(JSON.stringify(targetStatus(useAppStore.getState().conversationId ?? "")));
  });
  const changed = vi.fn();
  render(<GitDelivery workspaceRoot="C:/project" stagedCount={2} onChanged={changed} />);
  await screen.findByText("请在所选设备连接 GitHub 后刷新。");
  fireEvent.change(screen.getByRole("textbox", { name: "提交说明" }), { target: { value: "original draft" } });
  fireEvent.click(screen.getByRole("button", { name: "提交已暂存的 2 个文件" }));
  await waitFor(() => expect(signal).toBeTruthy());
  await act(async () => { useAppStore.setState({ conversationId: "two" }); });
  await screen.findByText("请在所选设备连接 GitHub 后刷新。");
  fireEvent.change(screen.getByRole("textbox", { name: "提交说明" }), { target: { value: "new owner's unsaved draft" } });
  expect(signal?.aborted).toBe(true);
  await act(async () => { finish(new Response(JSON.stringify({ message: "old commit finished" }))); });
  expect((screen.getByRole("textbox", { name: "提交说明" }) as HTMLTextAreaElement).value).toBe("new owner's unsaved draft");
  expect(screen.queryByText("old commit finished")).toBeNull(); expect(changed).not.toHaveBeenCalled();
});

it("restores each workspace's draft without overwriting the hidden workspace", async () => {
  localStorage.setItem("minicode.git.draft:C:/another", JSON.stringify({ message: "another preserved draft" }));
  const { rerender } = renderDelivery();
  await screen.findByText("连接 GitHub 后可创建草稿 PR。");
  fireEvent.change(screen.getByRole("textbox", { name: "提交说明" }), { target: { value: "project preserved draft" } });
  await act(async () => { useAppStore.setState({ workingDirectory: "C:/another" }); });
  rerender(<GitDelivery workspaceRoot="C:/another" stagedCount={2} onChanged={() => {}} />);
  expect((screen.getByRole("textbox", { name: "提交说明" }) as HTMLTextAreaElement).value).toBe("another preserved draft");
  await act(async () => { useAppStore.setState({ workingDirectory: "C:/project" }); });
  rerender(<GitDelivery workspaceRoot="C:/project" stagedCount={2} onChanged={() => {}} />);
  expect((screen.getByRole("textbox", { name: "提交说明" }) as HTMLTextAreaElement).value).toBe("project preserved draft");
  expect(JSON.parse(localStorage.getItem("minicode.git.draft:C:/another")!).message).toBe("another preserved draft");
});

it("uses saved force-push and draft preferences only after explicit confirmation", async () => {
  vi.mocked(fetch).mockImplementation(async (_url, init) => new Response(JSON.stringify(init?.method === "POST" ? { message: "Done" } : {
    ...localStatus, github: { ...github, authenticated: true },
    preferences: { ...localStatus.preferences, always_force_push: true, create_draft_pull_requests: false },
  })));
  vi.mocked(showConfirm).mockResolvedValue(true);
  renderDelivery();
  fireEvent.click(await screen.findByText("创建 PR", { selector: "summary" }));
  fireEvent.click(screen.getByText("分支与推送", { selector: "summary" }));
  expect(vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "推送" }));
  await waitFor(() => expect(showConfirm).toHaveBeenCalledWith(expect.objectContaining({ message: "使用 --force-with-lease 将 feature 推送到 origin。" })));
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1));
  const push = vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === "POST")!;
  expect(JSON.parse(String(push[1]?.body))).toMatchObject({ action: "push", force_with_lease: true });
  await waitFor(() => expect((screen.getByRole("textbox", { name: "PR 标题" }) as HTMLInputElement).disabled).toBe(false));
  fireEvent.change(screen.getByRole("textbox", { name: "PR 标题" }), { target: { value: "Explicit PR title" } });
  fireEvent.click(screen.getByRole("button", { name: "创建 PR" }));
  await waitFor(() => expect(showConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: "创建 PR" })));
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(2));
  const create = vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "POST")[1];
  expect(JSON.parse(String(create[1]?.body))).toMatchObject({ action: "create_pr", expected_branch: "feature" });
});

it("preserves edits and rejects an old owner's generated message", async () => {
  targetOwner();
  let finish!: (value: Response) => void;
  let signal: AbortSignal | null | undefined;
  vi.mocked(fetch).mockImplementation(async (_url, init) => {
    if (init?.method === "POST") { signal = init.signal; return new Promise((resolve) => { finish = resolve; }); }
    return new Response(JSON.stringify(targetStatus(useAppStore.getState().conversationId ?? "")));
  });
  renderDelivery();
  await screen.findByText("请在所选设备连接 GitHub 后刷新。");
  const input = screen.getByRole("textbox", { name: "提交说明" });
  fireEvent.change(input, { target: { value: "Original draft" } });
  fireEvent.click(screen.getByRole("button", { name: "生成提交说明" }));
  await waitFor(() => expect(signal).toBeTruthy());
  const [url, init] = vi.mocked(fetch).mock.calls.find(([, options]) => options?.method === "POST")!;
  expect(new URL(String(url)).pathname).toBe("/api/workspace/git/generate-message");
  expect(new URL(String(url)).searchParams.get("environment_id")).toBe("target");
  expect(JSON.parse(String(init?.body))).toMatchObject({ expected_registration_id: "original", expected_branch: "feature" });
  fireEvent.change(input, { target: { value: "Edit while generating" } });
  await act(async () => { finish(new Response(JSON.stringify({ message: "Generated message" }))); });
  expect((input as HTMLTextAreaElement).value).toBe("Edit while generating");
  fireEvent.click(screen.getByRole("button", { name: "生成提交说明" }));
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(2));
  await act(async () => { useAppStore.setState({ conversationId: "two" }); });
  await screen.findByText("请在所选设备连接 GitHub 后刷新。");
  fireEvent.change(input, { target: { value: "New owner draft" } });
  expect(signal?.aborted).toBe(true);
  await act(async () => { finish(new Response(JSON.stringify({ message: "Old owner result" }))); });
  expect((input as HTMLTextAreaElement).value).toBe("New owner draft");
});
