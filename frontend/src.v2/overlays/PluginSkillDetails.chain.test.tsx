/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { PluginsTab } from "./PluginsTab";
import { useAppStore } from "../stores";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "./ToastContainer";

vi.mock("../protocol/api", async importOriginal => ({
  ...await importOriginal<typeof import("../protocol/api")>(),
  apiBase: () => "http://test.local", authHeaders: (headers?: HeadersInit) => headers ?? {},
  fetchWithTimeout: (url: string, init?: RequestInit) => fetch(url, init),
  errorMessageFromResponseText: (text: string) => text,
}));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));

const path = "C:/plugins/notes/skills/review/SKILL.md";
const skill = { name: "notes:review", display_name: "Review notes", description: "Read complete notes", source_level: "plugin", path, enabled: true };
const otherSkill = { ...skill, path: "C:/plugins/other/skills/review/SKILL.md", display_name: "Other plugin skill" };
const plugin = { id: "notes@local", name: "notes", displayName: "Notes", enabled: true, path: "C:/plugins/notes", skill_count: 1, mcp_server_count: 0 };
const markdownBody = "# Review notes\n\n## Complete instructions\n\nRead **every** section.\n\nFinal body paragraph.";
const rawMarkdown = `---\r\nname: notes:review\r\ndescription: Original source description\r\ncustom: preserve-this\r\n---\r\n${markdownBody.replace(/\n/g, "\r\n")}\r\n`;
const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), { status });
const originalRuntime = window.__MINICODE_RUNTIME__;
const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
let enabled: boolean;
let bodyError: boolean;
let toggleError: boolean;
let skillPrompt: string;
const settings = (workspaceRoot = "C:/project") => ({ skills: [], plugin_skills: [{ ...skill, enabled, default_prompt: skillPrompt }, otherSkill], available_skills: enabled ? [skill, otherSkill] : [otherSkill], workspace_root: workspaceRoot });

beforeEach(() => {
  enabled = true; bodyError = false; toggleError = false; skillPrompt = "";
  delete window.__MINICODE_RUNTIME__;
  vi.clearAllMocks();
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  useAppStore.setState({ workingDirectory: "C:/project", conversationId: "owner", draft: "Keep draft", selectedSkills: [], selectedMentions: [],
    availableSkills: [skill, otherSkill], attachments: [{ id: "keep", name: "Keep.png", type: "image/png", size: 12, status: "ready", dataUrl: "data:image/png;base64,AA==" }],
    mcpServers: [], panelSlots: [{ id: "chat", kind: "chat", label: "Chat", focused: true }], settingsOpen: true, skillsMarketplaceOpen: true });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    if (url.pathname === "/api/plugins/marketplaces") return response({ marketplaces: [] });
    if (url.pathname === "/api/plugins") return response({ plugins: [plugin] });
    if (url.pathname === "/api/skills/settings") {
      if (init?.method === "PUT") {
        if (toggleError) return new Response("Actual configuration veto", { status: 400 });
        const body = JSON.parse(String(init.body));
        expect(body.path).toBe(path);
        expect(body.workspace_root).toBe("C:/project");
        enabled = body.enabled;
      }
      return response(settings(url.searchParams.get("workspace_root") || "C:/project"));
    }
    if (url.pathname === "/api/skills/detail") {
      expect(url.searchParams.get("skill_path")).toBe(path);
      expect(url.searchParams.get("workspace_root")).toBe("C:/project");
      return bodyError ? new Response("Actual Skill read failure", { status: 404 }) : response({ ...skill, content: markdownBody, raw_content: rawMarkdown });
    }
    throw new Error(`Unexpected request: ${url}`);
  }));
});
afterEach(() => {
  cleanup(); vi.unstubAllGlobals();
  if (originalRuntime) window.__MINICODE_RUNTIME__ = originalRuntime;
  else delete window.__MINICODE_RUNTIME__;
  if (originalClipboard) Object.defineProperty(navigator, "clipboard", originalClipboard);
  else Reflect.deleteProperty(navigator, "clipboard");
});

const openPlugin = async () => {
  render(<PluginsTab />);
  fireEvent.click(await screen.findByRole("button", { name: "查看插件详情 notes@local" }));
  await screen.findByRole("button", { name: "查看技能详情 notes:review" });
};

const openSkill = async () => {
  await openPlugin();
  fireEvent.click(screen.getByRole("button", { name: "查看技能详情 notes:review" }));
  return screen.findByRole("dialog", { name: "Review notes" });
};

it("opens exact Skill contents and keeps disabled entries visible, readable and re-enableable", async () => {
  await openPlugin();
  expect(screen.queryByText("Other plugin skill")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查看技能详情 notes:review" }));
  const dialog = await screen.findByRole("dialog", { name: "Review notes" });
  expect(await within(dialog).findByRole("heading", { name: "Complete instructions" })).toBeTruthy();
  expect(within(dialog).getByText("Final body paragraph.")).toBeTruthy();
  expect(within(dialog).queryByRole("button", { name: "卸载", exact: true })).toBeNull();
  expect(within(dialog).getAllByRole("heading", { name: "Review notes" }).length).toBe(1);
  fireEvent.click(within(dialog).getByRole("switch", { name: "停用 Review notes" }));
  await within(dialog).findByRole("switch", { name: "启用 Review notes" });
  expect(screen.getByRole("button", { name: "查看技能详情 notes:review" })).toBeTruthy();
  expect((within(dialog).getByRole("button", { name: "立即试用" }) as HTMLButtonElement).disabled).toBe(true);
  expect(useAppStore.getState().availableSkills.some(item => item.path === path)).toBe(false);
  expect(within(dialog).getByText("Final body paragraph.")).toBeTruthy();
  fireEvent.click(within(dialog).getByRole("switch", { name: "启用 Review notes" }));
  await within(dialog).findByRole("switch", { name: "停用 Review notes" });
  fireEvent.click(within(dialog).getByRole("button", { name: "立即试用" }));
  expect(useAppStore.getState().selectedSkills).toMatchObject([{ name: "notes:review", path }]);
  expect(useAppStore.getState().draft).toBe("Keep draft");
  expect(useAppStore.getState().attachments[0].name).toBe("Keep.png");
  expect(useAppStore.getState().conversationId).toBe("owner");
  expect(useAppStore.getState().skillsMarketplaceOpen).toBe(false);
  expect(sendClientCommand).not.toHaveBeenCalled();
});

it("shows actual read failures and retries the same registered Skill", async () => {
  bodyError = true;
  await openPlugin();
  fireEvent.click(screen.getByRole("button", { name: "查看技能详情 notes:review" }));
  const dialog = await screen.findByRole("dialog", { name: "Review notes" });
  expect(await within(dialog).findByRole("alert")).toHaveProperty("textContent", "Actual Skill read failure重试");
  fireEvent.click(within(dialog).getByRole("button", { name: "更多操作" }));
  expect((screen.getByRole("menuitem", { name: "复制 Markdown" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
  expect(screen.getByRole("dialog", { name: "Review notes" })).toBeTruthy();
  bodyError = false;
  fireEvent.click(within(dialog).getByRole("button", { name: "重试" }));
  expect(await within(dialog).findByText("Final body paragraph.")).toBeTruthy();
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByRole("region", { name: "插件详情" })).toBeTruthy();
});

it("prefills the Skill's actual default prompt with its exact selection without submitting", async () => {
  skillPrompt = "  Review the full notes  ";
  await openPlugin();
  fireEvent.click(screen.getByRole("button", { name: "查看技能详情 notes:review" }));
  const dialog = await screen.findByRole("dialog", { name: "Review notes" });
  fireEvent.click(within(dialog).getByRole("button", { name: "立即试用" }));
  expect(useAppStore.getState().draft).toBe("Keep draft\nReview the full notes");
  expect(useAppStore.getState().selectedSkills).toMatchObject([{ name: "notes:review", path }]);
  expect(sendClientCommand).not.toHaveBeenCalled();
});

it("keeps the Skill reference already present in its default prompt without adding another selection", async () => {
  skillPrompt = `Review [$notes:review](${path}) carefully.`;
  await openPlugin();
  fireEvent.click(screen.getByRole("button", { name: "查看技能详情 notes:review" }));
  const dialog = await screen.findByRole("dialog", { name: "Review notes" });
  fireEvent.click(within(dialog).getByRole("button", { name: "立即试用" }));
  expect(useAppStore.getState().draft).toBe(`Keep draft\n${skillPrompt}`);
  expect(useAppStore.getState().selectedSkills).toEqual([]);
  expect(sendClientCommand).not.toHaveBeenCalled();
});

it("keeps the effective switch state after a real configuration failure", async () => {
  toggleError = true;
  await openPlugin();
  fireEvent.click(screen.getByRole("switch", { name: "停用 Review notes" }));
  await waitFor(() => expect(pushToast).toHaveBeenCalledWith("Actual configuration veto", "error"));
  expect(screen.getByRole("switch", { name: "停用 Review notes" }).getAttribute("aria-checked")).toBe("true");
  expect(useAppStore.getState().availableSkills.some(item => item.path === path)).toBe(true);
});

it("can try a plugin without starter prompts by prefilling its exact mention and keeping the draft", async () => {
  await openPlugin();
  fireEvent.click(screen.getByRole("button", { name: "立即试用" }));
  expect(useAppStore.getState().selectedMentions).toMatchObject([{ kind: "plugin", configName: "notes@local", path: "plugin://notes@local" }]);
  expect(useAppStore.getState().draft).toBe("Keep draft");
  expect(useAppStore.getState().conversationId).toBe("owner");
  expect(sendClientCommand).not.toHaveBeenCalled();
});

it("prefills the first nonblank plugin prompt and renders only real starter prompts", async () => {
  const originalFetch = fetch;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => new URL(String(input)).pathname === "/api/plugins"
    ? Promise.resolve(response({ plugins: [{ ...plugin, defaultPrompt: ["   ", "  Summarize notes  ", "Ask followups"] }] })) : originalFetch(input, init)));
  await openPlugin();
  expect(screen.getByRole("button", { name: "Summarize notes" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Ask followups" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "立即试用" }));
  expect(useAppStore.getState().draft).toBe("Keep draft\nSummarize notes");
  expect(useAppStore.getState().selectedMentions).toMatchObject([{ kind: "plugin", configName: "notes@local" }]);
  expect(sendClientCommand).not.toHaveBeenCalled();
});

it("does not apply a prior workspace's pending Skill mutation to the current executable catalog", async () => {
  await openPlugin();
  const originalFetch = fetch;
  let finish!: (response: Response) => void;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => init?.method === "PUT"
    ? new Promise<Response>(resolve => { finish = resolve; }) : originalFetch(input, init)));
  fireEvent.click(screen.getByRole("switch", { name: "停用 Review notes" }));
  const currentSkill = { name: "current", path: "C:/current/SKILL.md", description: "Current workspace" };
  act(() => useAppStore.setState({ workingDirectory: "C:/other", availableSkills: [currentSkill] }));
  enabled = false;
  await act(async () => finish(response(settings())));
  expect(useAppStore.getState().availableSkills).toEqual([currentSkill]);
});

it("shows the Source More actions and copies the original Markdown with its frontmatter", async () => {
  const writeText = vi.fn(async () => {});
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  const dialog = await openSkill();
  await within(dialog).findByText("Final body paragraph.");
  fireEvent.click(within(dialog).getByRole("button", { name: "更多操作" }));
  expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual(["打开", "在文件资源管理器中显示", "复制 Markdown"]);
  expect((screen.getByRole("menuitem", { name: "打开" }) as HTMLButtonElement).disabled).toBe(false);
  expect(screen.queryByRole("menuitem", { name: "复制路径" })).toBeNull();
  expect((screen.getByRole("menuitem", { name: "在文件资源管理器中显示" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("menuitem", { name: "复制 Markdown" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledWith(rawMarkdown));
  expect(pushToast).toHaveBeenCalledWith("已复制 SKILL.md", "success", 2000);
  expect(screen.queryByRole("menu")).toBeNull();
  expect(screen.getByRole("dialog", { name: "Review notes" })).toBeTruthy();
});

it("opens the exact registered Skill in the original conversation's side panel and keeps drafts and editor mode", async () => {
  const editorTabs = [{ id: "unsaved", path: "C:/project/unsaved.ts", content: "unsaved buffer", original: "old", loading: false }];
  useAppStore.setState({ appMode: "cowork", editorTabs, previewArtifact: null, conversationWorkbenchStates: {} });
  const dialog = await openSkill();
  fireEvent.click(within(dialog).getByRole("button", { name: "更多操作" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "打开" }));
  await waitFor(() => expect(useAppStore.getState().previewArtifact?.content).toBe(rawMarkdown));
  const state = useAppStore.getState();
  expect(state.previewArtifact).toMatchObject({ source: "skill", sourcePath: path, workspaceRoot: "C:/project", loading: false });
  expect(state.conversationWorkbenchStates.owner.previewArtifact?.content).toBe(rawMarkdown);
  expect(state.rightPanelOpen).toBe(true);
  expect(state.rightStackTab).toBe("preview");
  expect(state.workingDirectory).toBe("C:/project");
  expect(state.conversationId).toBe("owner");
  expect(state.appMode).toBe("cowork");
  expect(state.editorTabs).toEqual(editorTabs);
  expect(state.draft).toBe("Keep draft");
  expect(state.attachments[0].name).toBe("Keep.png");
  expect(state.settingsOpen).toBe(false);
  expect(state.skillsMarketplaceOpen).toBe(false);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(sendClientCommand).not.toHaveBeenCalled();
});

it("reveals the registered Skill through the native catalog bridge without closing its detail", async () => {
  const reveal = vi.fn(async () => true);
  window.__MINICODE_RUNTIME__ = { desktop: { platformInfo: { isDesktop: true }, skills: { reveal } } } as never;
  const dialog = await openSkill();
  fireEvent.click(within(dialog).getByRole("button", { name: "更多操作" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "在文件资源管理器中显示" }));
  await waitFor(() => expect(reveal).toHaveBeenCalledWith({ path, workspaceRoot: "C:/project" }));
  expect(screen.getByRole("dialog", { name: "Review notes" })).toBeTruthy();
  expect(useAppStore.getState().workingDirectory).toBe("C:/project");
});

it("surfaces the native catalog rejection and keeps remote-host reveal disabled", async () => {
  const reveal = vi.fn(async () => { throw new Error("Skill is no longer registered"); });
  window.__MINICODE_RUNTIME__ = { desktop: { platformInfo: { isDesktop: true }, skills: { reveal } } } as never;
  const dialog = await openSkill();
  fireEvent.click(within(dialog).getByRole("button", { name: "更多操作" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "在文件资源管理器中显示" }));
  await waitFor(() => expect(pushToast).toHaveBeenCalledWith("Skill is no longer registered", "error"));
  expect(screen.getByRole("dialog", { name: "Review notes" })).toBeTruthy();
  act(() => {
    window.__MINICODE_RUNTIME__ = { ...window.__MINICODE_RUNTIME__, remoteHost: { id: "remote" } } as never;
    useAppStore.setState({ resolvedTheme: useAppStore.getState().resolvedTheme === "dark" ? "light" : "dark" });
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "更多操作" }));
  expect((screen.getByRole("menuitem", { name: "打开" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("menuitem", { name: "在文件资源管理器中显示" }) as HTMLButtonElement).disabled).toBe(true);
});

it("retains clipboard failures as errors without claiming that Markdown was copied", async () => {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn(async () => { throw new Error("Clipboard permission denied"); }) } });
  const dialog = await openSkill();
  await within(dialog).findByText("Final body paragraph.");
  fireEvent.click(within(dialog).getByRole("button", { name: "更多操作" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "复制 Markdown" }));
  await waitFor(() => expect(pushToast).toHaveBeenCalledWith("复制SKILL.md失败：Clipboard permission denied", "error", 3000));
  expect(pushToast).not.toHaveBeenCalledWith("已复制 SKILL.md", "success", 2000);
});

it("reads a disabled plugin's real Skill inventory without exposing invocation or enable controls", async () => {
  const originalFetch = fetch;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => new URL(String(input)).pathname === "/api/plugins"
    ? Promise.resolve(response({ plugins: [{ ...plugin, enabled: false }] })) : originalFetch(input, init)));
  await openPlugin();
  expect(screen.queryByRole("switch", { name: "停用 Review notes" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查看技能详情 notes:review" }));
  const dialog = screen.getByRole("dialog", { name: "Review notes" });
  expect(await within(dialog).findByText("Final body paragraph.")).toBeTruthy();
  expect(within(dialog).queryByRole("button", { name: "立即试用" })).toBeNull();
  expect(within(dialog).queryByRole("button", { name: "卸载", exact: true })).toBeNull();
  expect(within(dialog).queryByRole("switch")).toBeNull();
  expect(within(dialog).queryByRole("button", { name: "更多操作" })).toBeNull();
  fireEvent.click(within(dialog).getByRole("button", { name: "关闭", exact: true }));
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("loads and renders the actual inventory even when the declared plugin count is zero", async () => {
  const originalFetch = fetch;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => new URL(String(input)).pathname === "/api/plugins"
    ? Promise.resolve(response({ plugins: [{ ...plugin, skill_count: 0 }] })) : originalFetch(input, init)));
  await openPlugin();
  expect(screen.getByRole("button", { name: "查看技能详情 notes:review" })).toBeTruthy();
  expect(screen.getByRole("region", { name: "技能" }).textContent).toContain("技能 · 1");
});

it("renders declared local plugin information and follows its real website action", async () => {
  const originalFetch = fetch;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => new URL(String(input)).pathname === "/api/plugins"
    ? Promise.resolve(response({ plugins: [{ ...plugin, developerName: "Local author", category: "Productivity", version: "1.2.3", capabilities: ["Read", "Write"],
      websiteUrl: "https://docs.example/", privacyPolicyUrl: "https://docs.example/privacy", termsOfServiceUrl: "https://docs.example/terms" }] })) : originalFetch(input, init)));
  const open = vi.spyOn(window, "open").mockImplementation(() => ({} as Window));
  try {
    await openPlugin();
    const info = screen.getByRole("region", { name: "信息" });
    expect(info.textContent).toContain("Read, Write");
    expect(info.textContent).toContain("Productivity");
    expect(info.textContent).toContain("Local author");
    expect(info.textContent).toContain("1.2.3");
    expect(within(info).getAllByRole("link").map(link => link.getAttribute("href"))).toEqual([
      "https://docs.example/", "https://docs.example/privacy", "https://docs.example/terms",
    ]);
    fireEvent.click(within(info).getByRole("link", { name: "https://docs.example/" }));
    expect(open).toHaveBeenCalledWith("https://docs.example/", "_blank", "noopener,noreferrer");
    expect(within(info).queryByText("安装范围")).toBeNull();
  } finally { open.mockRestore(); }
});
