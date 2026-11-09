/* @vitest-environment jsdom */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useAppStore, waitComposerDraftRecovery, waitEditorDraftRecovery } from "../stores";
import { emptyComposerDraft } from "../stores/composer-drafts";
import { cacheEditorStateForWorkspace, clearEditorWorkspaceBufferCacheForTests, editorStateForWorkspace } from "../stores/shared-helpers";
import { openRegisteredSkillPreview } from "./openAttachmentPreview";
import { resetPreviewRequestScopesForTests } from "./previewRequestScope";
import { pushToast } from "../overlays/ToastContainer";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
const runtime = vi.hoisted(() => {
  const value = { apiBaseUrl: "http://127.0.0.1:9187", wsBaseUrl: "ws://127.0.0.1:9187", runtimeToken: "isolated-skill-preview-token" };
  vi.stubGlobal("__MINICODE_RUNTIME__", value);
  return value;
});
const target = { path: "C:/Installed plugin/skills/real-skill/SKILL.md", workspaceRoot: "C:/Original project", conversationId: "original-chat" };
const raw = "---\nname: real-skill\ndescription: Exact registered file\n---\n\n# Actual instructions\n\n保留原始内容\n";
const response = (path = target.path, rawContent = raw) => Response.json({ path, content: "Parsed body without original frontmatter", raw_content: rawContent });
const transport = vi.fn<typeof fetch>();
const dirtyEditor = { id: "editor-original", path: "C:/Original project/未保存.ts", content: "Original unsaved buffer", original: "Disk buffer", loading: false, error: null };
const hiddenEditor = { ...dirtyEditor, id: "hidden-editor", path: "C:/Hidden project/hidden.ts", content: "Hidden unsaved editor buffer" };
const hiddenDraft = { ...emptyComposerDraft(), draft: "Hidden unsent chat draft" };

beforeEach(async () => {
  vi.clearAllMocks();
  resetPreviewRequestScopesForTests();
  clearEditorWorkspaceBufferCacheForTests();
  vi.stubGlobal("__MINICODE_RUNTIME__", runtime);
  vi.stubGlobal("fetch", transport);
  transport.mockResolvedValue(response());
  cacheEditorStateForWorkspace("C:/Hidden project", [hiddenEditor], hiddenEditor.path, hiddenEditor.path);
  useAppStore.setState({ conversationId: target.conversationId, workingDirectory: target.workspaceRoot, appMode: "cowork",
    conversations: [{ id: target.conversationId, title: "Original task", updatedAt: "2026-10-20", workspaceRoot: target.workspaceRoot }],
    conversationWorkbenchStates: {}, previewArtifact: null, previewOwnerConversationId: null, livePreviewUrl: null,
    editorTabs: [dirtyEditor], activeTabPath: dirtyEditor.path, activeEditorPath: dirtyEditor.path, editorOpenRequests: [],
    panelSlots: [{ id: "chat", kind: "chat" }, { id: "existing-editor", kind: "editor" }], rightPanelOpen: false, rightPanelExpanded: false,
    draft: "Keep current unsent chat draft", attachments: [{ id: "image-original", name: "draft.png", type: "image/png", size: 12, status: "ready" }],
    workspaceComposerDrafts: { "C:/Hidden project": hiddenDraft }, pendingConversationSwitchId: null, pendingConversationCreateId: null,
    isConnected: true });
  await waitComposerDraftRecovery();
  await waitEditorDraftRecovery();
});
afterEach(() => { resetPreviewRequestScopesForTests(); vi.unstubAllGlobals(); });

it("opens the registered original Markdown in the owning chat File panel without touching workspaces or editor buffers", async () => {
  const before = useAppStore.getState();
  expect(openRegisteredSkillPreview(target)).toBe(true);
  expect(useAppStore.getState().previewArtifact).toMatchObject({ source: "skill", loading: true, sourcePath: target.path });
  await vi.waitFor(() => expect(useAppStore.getState().previewArtifact).toMatchObject({ source: "skill", content: raw, loading: false }));
  const [input, init] = transport.mock.calls[0];
  const url = new URL(String(input));
  expect(url.pathname).toBe("/api/skills/detail");
  expect(Object.fromEntries(url.searchParams)).toEqual({ skill_path: target.path, workspace_root: target.workspaceRoot });
  expect(new Headers(init?.headers).get("X-MiniCode-Token")).toBe(runtime.runtimeToken);
  expect(init?.cache).toBe("no-store");
  expect(init?.signal).toBeInstanceOf(AbortSignal);
  const after = useAppStore.getState();
  expect(after.previewArtifact).toMatchObject({ name: "SKILL.md", sourcePath: target.path, workspaceRoot: target.workspaceRoot, mediaType: "text/markdown", contentChars: raw.length });
  expect(after.previewArtifact).not.toHaveProperty("url");
  expect(after).toMatchObject({ conversationId: target.conversationId, workingDirectory: target.workspaceRoot, appMode: "cowork",
    previewOwnerConversationId: target.conversationId, rightStackTab: "preview", rightPanelOpen: true, draft: before.draft, attachments: before.attachments,
    activeEditorPath: dirtyEditor.path, activeTabPath: dirtyEditor.path, editorOpenRequests: [] });
  expect(after.editorTabs).toBe(before.editorTabs);
  expect(after.panelSlots).toBe(before.panelSlots);
  expect(after.workspaceComposerDrafts).toBe(before.workspaceComposerDrafts);
  expect(editorStateForWorkspace("C:/Hidden project").editorTabs).toEqual([hiddenEditor]);
  expect(transport).toHaveBeenCalledTimes(1);
});

it.each([
  [404, "Skill is not registered or its contents cannot be read."],
  [503, "Actual registered Skill read error"],
] as const)("keeps the actual %i read failure in the same preview instead of switching scope or showing a fake file", async (status, message) => {
  transport.mockResolvedValueOnce(Response.json({ detail: message }, { status }));
  openRegisteredSkillPreview(target);
  await vi.waitFor(() => expect(useAppStore.getState().previewArtifact).toMatchObject({ source: "skill", sourcePath: target.path, content: "", error: message, loading: false }));
  expect(useAppStore.getState()).toMatchObject({ conversationId: target.conversationId, workingDirectory: target.workspaceRoot, appMode: "cowork", draft: "Keep current unsent chat draft", editorTabs: [dirtyEditor] });
  expect(transport).toHaveBeenCalledTimes(1);
});

it("ignores an earlier Skill read after the owner selects another file", async () => {
  let finish!: (value: Response) => void;
  transport.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  openRegisteredSkillPreview(target);
  const oldSignal = transport.mock.calls[0][1]!.signal!;
  const second = { ...target, path: "C:/Other installed skill/SKILL.md" };
  transport.mockResolvedValueOnce(response(second.path, "# Newly selected Skill\n"));
  openRegisteredSkillPreview(second);
  await vi.waitFor(() => expect(useAppStore.getState().previewArtifact?.content).toBe("# Newly selected Skill\n"));
  expect(oldSignal.aborted).toBe(true);
  finish(response());
  await Promise.resolve(); await Promise.resolve();
  expect(useAppStore.getState().previewArtifact).toMatchObject({ sourcePath: second.path, content: "# Newly selected Skill\n" });
});

it("keeps a delayed read with its original chat instead of replacing the newly active chat preview or hidden drafts", async () => {
  let finish!: (value: Response) => void;
  transport.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  openRegisteredSkillPreview(target);
  const nextPreview = { artifactId: "new-owner-file", source: "local" as const, content: "New owner file", loadedAt: 1 };
  useAppStore.setState({ conversationId: "new-chat", workingDirectory: "C:/New project", draft: "New owner unsent draft", previewArtifact: nextPreview, previewOwnerConversationId: "new-chat" });
  finish(response());
  await vi.waitFor(() => expect(useAppStore.getState().conversationWorkbenchStates[target.conversationId]?.previewArtifact?.content).toBe(raw));
  expect(useAppStore.getState()).toMatchObject({ conversationId: "new-chat", workingDirectory: "C:/New project", draft: "New owner unsent draft", previewArtifact: nextPreview, previewOwnerConversationId: "new-chat", editorTabs: [dirtyEditor] });
  expect(useAppStore.getState().workspaceComposerDrafts["C:/Hidden project"]).toEqual(hiddenDraft);
});

it("rejects a stale explicit chat owner before any request or preview mutation", () => {
  useAppStore.setState({ conversationId: "new-chat" });
  const before = useAppStore.getState();
  expect(openRegisteredSkillPreview(target)).toBe(false);
  expect(transport).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toBe(before);
  expect(pushToast).toHaveBeenCalledExactlyOnceWith("聊天已切换，请回到原聊天后打开技能。", "error");
});

it("supports the existing unowned File panel without creating a task or workspace", async () => {
  useAppStore.setState({ conversationId: null, conversations: [], workingDirectory: "" });
  expect(openRegisteredSkillPreview({ ...target, workspaceRoot: "", conversationId: null })).toBe(true);
  await vi.waitFor(() => expect(useAppStore.getState().previewArtifact?.content).toBe(raw));
  expect(useAppStore.getState()).toMatchObject({ conversationId: null, workingDirectory: "", appMode: "cowork", pendingConversationCreateId: null, pendingConversationSwitchId: null, editorTabs: [dirtyEditor] });
  expect(new URL(String(transport.mock.calls[0][0])).searchParams.get("workspace_root")).toBe("");
});
