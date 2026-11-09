/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { SkillsMarketplace } from "./SkillsMarketplace";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "./ToastContainer";
import { useSkillSettings } from "./useSkillSettings";
import { handleCommandCatalogEvent } from "../chat/commandCatalogEvents";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { writable: true, value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("../protocol/api", async original => ({
  ...await original<typeof import("../protocol/api")>(),
  apiBase: () => "http://test.local", authHeaders: (headers?: HeadersInit) => headers ?? {},
  LONG_HTTP_TIMEOUT_MS: 300_000, fetchWithTimeout: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, init),
  errorMessageFromResponseText: (text: string, fallback: string) => text || fallback,
  pluginAssetResourceUrlWithToken: () => "",
  fetchDraftModelSelection: vi.fn(async (workspaceRoot: string) => ({ current_model: "fixture-model", model: "fixture-model", provider: "custom",
    available_models: ["fixture-model"], models_source: "configured", available_permission_modes: ["auto"],
    config_version: "fixture", config_cwd: workspaceRoot, feature_flags: {}, composer_commands: [] })),
}));
vi.mock("../desktop/runtime", () => ({ desktop: () => undefined, isDesktop: () => false, pickDirectory: vi.fn() }));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("./DialogService", () => ({ showAlert: vi.fn(), showConfirm: vi.fn(async () => true), showPrompt: vi.fn(async () => "C:/skills/local") }));

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
const catalog = { skills: [{ name: "remote-review", title: "Remote Review", description: "Review repositories", installed: false }], source_status: { openai_skills: { ok: true } } };

const docs = { name: "docs", display_name: "文档处理", description: "处理文档", source_level: "user", path: "C:/skills/docs/SKILL.md", default_prompt: "Use the document workflow" };
const internal = { name: "internal", display_name: "内部技能", description: "模型调用", source_level: "builtin", user_invocable: false, path: "C:/skills/internal/SKILL.md" };
let docsEnabled = true;
let docsInstalled = true;
const settingsPayload = (root = "C:/marketplace") => ({ skills: [...(docsInstalled ? [{ ...docs, enabled: docsEnabled, uninstall_name: "docs" }] : []), { ...internal, enabled: true }], plugin_skills: [], available_skills: docsInstalled && docsEnabled ? [docs, internal] : [internal], workspace_root: root });
const rawSkill = "---\nname: docs\ndescription: Actual document workflow\n---\n# Document workflow\n\nFull original Skill instructions.\n";
const openSkill = async (name: string) => fireEvent.click((await screen.findAllByRole("button", { name: `查看技能详情 ${name}` }))[0]);
const openPublic = () => {
  fireEvent.click(screen.getByRole("button", { name: "添加", exact: true }));
  fireEvent.click(screen.getByRole("menuitem", { name: "浏览公开技能" }));
};

describe("Plugins and skills workspace", () => {
  beforeEach(() => {
    useAppStore.setState({ skillsMarketplaceOpen: true, skillsMarketplaceTab: "skills", skillsMarketplaceReturnTarget: "app", settingsOpen: false,
      workingDirectory: "C:/marketplace", conversationId: "catalog-owner", activeConversationDraftId: null, draft: "Keep draft", availableSkills: [docs, internal],
      marketplaceSkills: [], selectedSkills: [], conversationWorkbenchStates: {}, conversationDrafts: {}, homeConversationDraftIds: {}, workspaceComposerDrafts: {} });
    docsEnabled = true; docsInstalled = true;
    vi.mocked(sendClientCommand).mockImplementation(command => {
      if (command.type === "skills.list") {
        const store = useAppStore.getState(), root = command.workspace_root ?? store.workingDirectory;
        handleCommandCatalogEvent({ type: "skills.list", conversation_id: command.conversation_id ?? store.conversationId ?? "",
          workspace_root: root, skills: settingsPayload(root).available_skills });
      }
      return true;
    });
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname === "/api/skills/docs" && init?.method === "DELETE") { docsInstalled = false; return Promise.resolve(json({ removed: true })); }
      if (url.pathname === "/api/skills/settings") {
        if (init?.method === "PUT") docsEnabled = JSON.parse(String(init.body)).enabled;
        return Promise.resolve(json(settingsPayload(url.searchParams.get("workspace_root") || "C:/marketplace")));
      }
      if (url.pathname === "/api/skills/detail") return Promise.resolve(json({ ...docs, enabled: docsEnabled, content: "# Document workflow\n\nFull original Skill instructions.", raw_content: rawSkill }));
      if (url.pathname === "/api/skills/marketplace/remote-review") return Promise.resolve(json({ content: "# Remote Review\n\nRead the real review instructions.", raw_content: "---\nname: remote-review\n---\nRead the real review instructions." }));
      if (String(input).includes("/api/plugins/marketplaces")) return Promise.resolve(json({ marketplaces: [] }));
      if (String(input).split("?")[0].endsWith("/api/plugins")) return Promise.resolve(json({ plugins: [] }));
      return Promise.resolve(json(catalog));
    }));
  });
  afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); localStorage.removeItem("minicode.skillCreatorPrefillShown"); });

  it("renders the sidebar-compatible page and authentic local source filters", async () => {
    render(<SkillsMarketplace />);
    expect(screen.getByRole("main", { name: "插件与技能" })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(await within(screen.getByRole("region", { name: "已安装技能" })).findByText("文档处理")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "个人", exact: true }));
    expect(within(screen.getByRole("region", { name: "个人技能" })).getByText("文档处理")).toBeTruthy();
  });
  it("loads the public directory only when it is opened", () => {
    render(<SkillsMarketplace />);
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining("/api/skills/marketplace"), expect.anything());
    openPublic();
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/skills/marketplace"), expect.anything());
  });
  it("opens complete row details and keeps the Add menu state separate from row actions", async () => {
    render(<SkillsMarketplace />);
    await openSkill("docs");
    const detail = screen.getByRole("dialog", { name: "文档处理" });
    expect(await within(detail).findByText("Full original Skill instructions.")).toBeTruthy();
    fireEvent.click(within(detail).getByRole("button", { name: "关闭技能详情" }));
    expect(screen.getByRole("button", { name: "添加", exact: true }).getAttribute("aria-expanded")).toBe("false");
  });
  it("takes a selected skill to the composer even when opened from settings over a maximized editor", async () => {
    useAppStore.setState({ skillsMarketplaceReturnTarget: "settings", panelSlots: [
      { id: "chat", kind: "chat", focused: false }, { id: "editor", kind: "editor", focused: true, maximized: true },
    ] });
    const focus = vi.fn();
    window.addEventListener("composer:focus", focus, { once: true });
    render(<SkillsMarketplace />);
    await openSkill("docs");
    fireEvent.click(screen.getByRole("button", { name: "立即试用" }));
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(false);
    expect(useAppStore.getState().settingsOpen).toBe(false);
    expect(useAppStore.getState().selectedSkills[0].name).toBe("docs");
    expect(useAppStore.getState().conversationWorkbenchStates["catalog-owner"].draft).toBe("Keep draft");
    expect(useAppStore.getState().panelSlots.find((slot) => slot.id === "chat")?.focused).toBe(true);
    expect(useAppStore.getState().panelSlots.some((slot) => slot.maximized)).toBe(false);
    await waitFor(() => expect(focus).toHaveBeenCalledOnce());
  });
  it("displays the remote catalog and installs through the real skill API contract", async () => {
    render(<SkillsMarketplace />);
    openPublic();
    fireEvent.click(await screen.findByRole("button", { name: "安装技能 remote-review" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/skills/install", expect.objectContaining({ method: "POST", body: JSON.stringify({ skill_name: "remote-review" }) })));
    expect(sendClientCommand).toHaveBeenCalledWith(expect.objectContaining({ type: "skills.list" }), { silent: true });
  });
  it("opens the public Skill's actual instructions before installation", async () => {
    render(<SkillsMarketplace />);
    openPublic();
    fireEvent.click(await screen.findByRole("button", { name: "查看公开技能详情 remote-review" }));
    const detail = screen.getByRole("dialog", { name: "Remote Review" });
    expect(await within(detail).findByText("Read the real review instructions.")).toBeTruthy();
    fireEvent.click(within(detail).getByRole("button", { name: "安装", exact: true }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/skills/install", expect.objectContaining({ method: "POST", body: JSON.stringify({ skill_name: "remote-review" }) })));
  });
  it("keeps installed skills usable when the public catalog fails", async () => {
    const originalFetch = fetch;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input).includes("/api/skills/marketplace") ? Promise.resolve(json({ detail: "offline" }, 503)) : originalFetch(input, init)));
    render(<SkillsMarketplace />);
    openPublic();
    await screen.findByRole("alert");
    await openSkill("docs");
    fireEvent.click(screen.getByRole("button", { name: "立即试用" }));
    expect(useAppStore.getState().selectedSkills).toEqual([expect.objectContaining({ name: "docs", path: "C:/skills/docs/SKILL.md" })]);
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(false);
  });
  it("honors user_invocable and removes only personal skills", async () => {
    render(<SkillsMarketplace />);
    await openSkill("internal");
    expect((screen.getByRole("button", { name: "立即试用" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "卸载", exact: true })).toBeNull();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(true);
    await openSkill("docs");
    fireEvent.click(screen.getByRole("button", { name: "卸载", exact: true }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/skills/docs", expect.objectContaining({ method: "DELETE" })));
  });

  it("uninstalls owned Skill details by the backend directory identity and clears catalog installation state", async () => {
    const originalFetch = fetch;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (init?.method === "DELETE") { docsInstalled = false; return Promise.resolve(json({ removed: true })); }
      if (url.pathname === "/api/skills/settings") return Promise.resolve(json({ ...settingsPayload(),
        skills: docsInstalled ? [{ ...docs, enabled: true, uninstall_name: "actual-directory" }] : [] }));
      return originalFetch(input, init);
    }));
    useAppStore.setState({ marketplaceSkills: [{ name: "actual-directory", title: "Actual directory", description: "Installed workflow", triggers: [], installed: true }] });
    render(<SkillsMarketplace />);
    await openSkill("docs");
    const dialog = screen.getByRole("dialog", { name: "文档处理" });
    await within(dialog).findByText("Full original Skill instructions.");
    fireEvent.click(await within(dialog).findByRole("button", { name: "卸载", exact: true }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/skills/actual-directory", expect.objectContaining({ method: "DELETE" })));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryByRole("button", { name: "查看技能详情 docs" })).toBeNull();
    expect(useAppStore.getState().marketplaceSkills[0].installed).toBe(false);
    expect(useAppStore.getState().availableSkills.map(skill => skill.name)).toEqual(["internal"]);
    expect(useAppStore.getState().draft).toBe("Keep draft");
  });

  it("keeps external user roots readable without Uninstall in either the detail or row menu", async () => {
    const originalFetch = fetch;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => new URL(String(input)).pathname === "/api/skills/settings"
      ? Promise.resolve(json({ ...settingsPayload(), skills: [{ ...docs, enabled: true, uninstall_name: null }] })) : originalFetch(input, init)));
    render(<SkillsMarketplace />);
    await openSkill("docs");
    const dialog = screen.getByRole("dialog", { name: "文档处理" });
    await within(dialog).findByText("Full original Skill instructions.");
    expect(within(dialog).queryByRole("button", { name: "卸载", exact: true })).toBeNull();
    fireEvent.keyDown(dialog, { key: "Escape" });
  });
  it("supports the add menu and web-host local imports", async () => {
    render(<SkillsMarketplace />);
    fireEvent.click(screen.getByRole("button", { name: "添加", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "导入本地技能" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/skills/import", expect.objectContaining({ body: JSON.stringify({ source_path: "C:/skills/local" }) })));
  });
  it("preserves query and an in-flight installation across tab switches and page hiding", async () => {
    let finish!: (response: Response) => void;
    const originalFetch = fetch;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith("/api/skills/install")
      ? new Promise<Response>(resolve => { finish = resolve; }) : originalFetch(input, init)));
    render(<SkillsMarketplace />);
    openPublic();
    const install = await screen.findByRole("button", { name: "安装技能 remote-review" });
    fireEvent.change(screen.getByRole("textbox", { name: "搜索技能" }), { target: { value: "Review" } });
    fireEvent.click(install);
    fireEvent.click(screen.getByRole("tab", { name: "插件", exact: true }));
    act(() => useAppStore.setState({ skillsMarketplaceOpen: false }));
    await act(async () => finish(json({ installed: true })));
    expect(pushToast).toHaveBeenCalledWith("已安装技能：remote-review", "success");
    act(() => useAppStore.setState({ skillsMarketplaceOpen: true, skillsMarketplaceTab: "skills" }));
    expect((screen.getByRole("textbox", { name: "搜索技能" }) as HTMLInputElement).value).toBe("Review");
  });
  it("navigates product tabs with arrows and returns to the correct settings page", () => {
    useAppStore.setState({ skillsMarketplaceReturnTarget: "settings" });
    render(<SkillsMarketplace />);
    fireEvent.keyDown(screen.getByRole("tab", { name: "技能", exact: true }), { key: "ArrowLeft" });
    expect(useAppStore.getState().skillsMarketplaceTab).toBe("plugins");
    fireEvent.click(screen.getByRole("button", { name: "返回技能设置" }));
    expect(useAppStore.getState().settingsOpen).toBe(true);
    expect(useAppStore.getState().settingsTab).toBe("skills");
  });

  it("keeps the installed overview while filtering the source group", async () => {
    render(<SkillsMarketplace />);
    expect(await within(screen.getByRole("region", { name: "已安装技能" })).findByRole("button", { name: "查看技能详情 internal" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "系统", exact: true }));
    expect(within(screen.getByRole("region", { name: "系统技能" })).getByRole("button", { name: "查看技能详情 internal" })).toBeTruthy();
    expect(within(screen.getByRole("region", { name: "已安装技能" })).getByRole("button", { name: "查看技能详情 docs" })).toBeTruthy();
  });

  it("uses the directory inventory for a new draft and expands the overview and project group independently", async () => {
    const project = Array.from({ length: 8 }, (_, index) => ({ name: `project-${index}`, display_name: `Project ${index}`,
      description: `Project workflow ${index}`, path: `C:/Other/.agents/skills/${index}/SKILL.md`, source_level: "workspace", enabled: true,
      catalog_scope: { workspace_root: "C:/Other", group_id: "C:/Other", group_name: "Other" } }));
    const originalFetch = fetch;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => new URL(String(input)).pathname === "/api/skills/settings"
      ? Promise.resolve(json({ ...settingsPayload(), skills: [...settingsPayload().skills, ...project] })) : originalFetch(input, init)));
    useAppStore.setState({ activeConversationDraftId: "new-draft", availableSkills: [] });
    render(<SkillsMarketplace />);
    const overview = screen.getByRole("region", { name: "已安装技能" });
    await within(overview).findByRole("button", { name: "查看技能详情 project-0" });
    expect(within(overview).getAllByRole("article")).toHaveLength(6);
    fireEvent.click(within(overview).getByRole("button", { name: /^查看(?!技能详情)/ }));
    expect(within(overview).getAllByRole("article")).toHaveLength(10);
    fireEvent.click(screen.getByRole("button", { name: "Other", exact: true }));
    const group = screen.getByRole("region", { name: "Other技能" });
    expect(within(group).getAllByRole("article")).toHaveLength(6);
    fireEvent.click(within(group).getByRole("button", { name: /^查看(?!技能详情)/ }));
    expect(within(group).getAllByRole("article")).toHaveLength(8);
    expect(within(overview).getAllByRole("article")).toHaveLength(10);
  });

  it("opens a new skill creation draft while retaining the original draft", async () => {
    const creator = { name: "skill-creator", description: "Create a skill", source_level: "builtin", enabled: true,
      path: "C:/builtin/skill-creator/SKILL.md" };
    const originalFetch = fetch;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => new URL(String(input)).pathname === "/api/skills/settings"
      ? Promise.resolve(json({ ...settingsPayload(), skills: [...settingsPayload().skills, creator] })) : originalFetch(input, init)));
    useAppStore.setState({ conversationId: null, activeConversationDraftId: null, draft: "", appMode: "code" });
    const oldId = useAppStore.getState().startConversationDraft({ workspaceRoot: "C:/marketplace", bindWorkspace: true, appMode: "code" });
    useAppStore.getState().setDraft("Do not replace this draft");
    useAppStore.setState({ skillsMarketplaceOpen: true, skillsMarketplaceTab: "skills" });
    render(<SkillsMarketplace />);
    await screen.findAllByRole("button", { name: "查看技能详情 skill-creator" });
    fireEvent.click(screen.getByRole("button", { name: "添加", exact: true }));
    fireEvent.click(screen.getByRole("menuitem", { name: "创建技能" }));
    const state = useAppStore.getState();
    expect(state.activeConversationDraftId).not.toBe(oldId);
    expect(state.selectedSkills).toEqual([expect.objectContaining({ name: "skill-creator", path: creator.path })]);
    expect(state.draft).toBe("help me create a skill");
    expect(vi.mocked(sendClientCommand).mock.calls.some(([command]) => command.type === "user_message")).toBe(false);
    act(() => useAppStore.getState().selectConversationDraft(oldId));
    expect(useAppStore.getState().draft).toBe("Do not replace this draft");
  });

  it("reads and toggles another project's skill using that project's scope", async () => {
    let enabled = false;
    const project = { name: "other-review", description: "Other project", source_level: "workspace", enabled,
      path: "C:/Other/.agents/skills/review/SKILL.md", catalog_scope: { workspace_root: "C:/Other", group_id: "C:/Other", group_name: "Other" } };
    const originalFetch = fetch;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname === "/api/skills/detail") {
        expect(url.searchParams.get("workspace_root")).toBe("C:/Other");
        return Promise.resolve(json({ content: "Other instructions", raw_content: "Other instructions" }));
      }
      if (url.pathname === "/api/skills/settings") {
        if (init?.method === "PUT") {
          expect(JSON.parse(String(init.body))).toEqual({ path: project.path, enabled: true, workspace_root: "C:/Other" });
          enabled = true;
        }
        return Promise.resolve(json({ ...settingsPayload(), skills: [...settingsPayload().skills, { ...project, enabled }] }));
      }
      return originalFetch(input, init);
    }));
    render(<SkillsMarketplace />);
    await openSkill("other-review");
    const detail = screen.getByRole("dialog", { name: "other-review" });
    await within(detail).findByText("Other instructions");
    fireEvent.click(within(detail).getByRole("switch", { name: "启用 other-review" }));
    await waitFor(() => expect(within(detail).getByRole("switch", { name: "停用 other-review" })).toBeTruthy());
    expect(useAppStore.getState().draft).toBe("Keep draft");
  });

  it("opens the complete installed Skill body and keeps list filters", async () => {
    render(<SkillsMarketplace />);
    fireEvent.change(screen.getByRole("textbox", { name: "搜索技能" }), { target: { value: "文档" } });
    fireEvent.click(await screen.findByRole("button", { name: "查看技能详情 docs" }));
    const detail = screen.getByRole("dialog", { name: "文档处理" });
    expect(await within(detail).findByText("Full original Skill instructions.")).toBeTruthy();
    expect(within(detail).getByRole("button", { name: "立即试用" })).toBeTruthy();
    expect(within(detail).getByRole("button", { name: "更多操作" })).toBeTruthy();
    fireEvent.keyDown(detail, { key: "Escape" });
    expect(screen.getByRole("textbox", { name: "搜索技能" })).toHaveProperty("value", "文档");
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(true);
  });

  it("keeps a disabled installed user Skill readable and re-enableable before Try now", async () => {
    docsEnabled = false;
    useAppStore.setState({ availableSkills: [internal] });
    render(<SkillsMarketplace />);
    await openSkill("docs");
    const detail = screen.getByRole("dialog", { name: "文档处理" });
    expect(await within(detail).findByText("Full original Skill instructions.")).toBeTruthy();
    expect((within(detail).getByRole("button", { name: "立即试用" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(detail).getByRole("switch", { name: "启用 文档处理" }));
    await waitFor(() => expect((within(detail).getByRole("button", { name: "立即试用" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(within(detail).getByRole("button", { name: "立即试用" }));
    expect(useAppStore.getState().selectedSkills).toMatchObject([{ name: "docs", path: docs.path }]);
    expect(useAppStore.getState().draft).toBe("Use the document workflow");
    expect(useAppStore.getState().conversationWorkbenchStates["catalog-owner"].draft).toBe("Keep draft");
    expect(vi.mocked(sendClientCommand).mock.calls.some(([command]) => command.type === "user_message")).toBe(false);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each(["install", "import"] as const)("rebuilds both current consumers after a pending global %s and holds the button through its real read", async kind => {
    let finishWrite!: (value: Response) => void;
    let committed = false, writes = 0;
    const finishReads: ((value: Response) => void)[] = [];
    const reads: string[] = [];
    const installed = { name: "remote-review", description: "Installed instructions", path: "C:/skills/remote-review/SKILL.md", source_level: "user", enabled: true, uninstall_name: "remote-review" };
    const currentPayload = (root: string) => ({ ...settingsPayload(root),
      skills: [...settingsPayload(root).skills, ...(committed ? [installed] : [])],
      available_skills: [...settingsPayload(root).available_skills, ...(committed ? [installed] : []), { name: root + "-local", description: "Current project", path: root + "/local/SKILL.md" }] });
    vi.mocked(sendClientCommand).mockImplementation(command => {
      if (command.type === "skills.list") {
        const store = useAppStore.getState(), root = command.workspace_root ?? store.workingDirectory;
        handleCommandCatalogEvent({ type: "skills.list", conversation_id: command.conversation_id ?? store.conversationId ?? "",
          workspace_root: root, skills: currentPayload(root).available_skills });
      }
      return true;
    });
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (init?.method === "POST" && url.pathname === `/api/skills/${kind}`) { writes++; return new Promise<Response>(resolve => { finishWrite = resolve; }); }
      if (url.pathname === "/api/skills/settings") {
        const root = url.searchParams.get("workspace_root")!; reads.push(root);
        return committed ? new Promise<Response>(resolve => finishReads.push(resolve)) : Promise.resolve(json(currentPayload(root)));
      }
      if (url.pathname === "/api/skills/marketplace") return Promise.resolve(json({ ...catalog, skills: catalog.skills.map(skill => ({ ...skill, installed: committed })) }));
      if (url.pathname === "/api/plugins/marketplaces") return Promise.resolve(json({ marketplaces: [] }));
      if (url.pathname === "/api/plugins") return Promise.resolve(json({ plugins: [] }));
      return Promise.resolve(json({}));
    }));
    render(<SkillsMarketplace />);
    const sibling = renderHook(() => useSkillSettings(true));
    await waitFor(() => expect(sibling.result.current.skills).toHaveLength(2));
    let button: HTMLElement;
    if (kind === "install") {
      openPublic();
      button = await screen.findByRole("button", { name: "安装技能 remote-review" });
      fireEvent.click(button);
    } else {
      button = screen.getByRole("button", { name: "添加", exact: true });
      fireEvent.click(button); fireEvent.click(screen.getByRole("menuitem", { name: "导入本地技能" }));
    }
    await waitFor(() => expect(writes).toBe(1));
    act(() => useAppStore.setState({ workingDirectory: "C:/B", conversationId: "catalog-b" }));
    await waitFor(() => expect(reads.filter(root => root === "C:/B")).toHaveLength(2));
    const aReads = reads.filter(root => root === "C:/marketplace").length;
    committed = true;
    await act(async () => finishWrite(json({ installed: true })));
    await waitFor(() => expect(finishReads).toHaveLength(2));
    expect(button).toHaveProperty("disabled", true);
    fireEvent.click(button); expect(writes).toBe(1);
    await act(async () => { for (const finish of finishReads) finish(json(currentPayload("C:/B"))); });
    await waitFor(() => expect(sibling.result.current.skills.some(skill => skill.path === installed.path)).toBe(true));
    expect((await screen.findAllByRole("button", { name: "查看技能详情 remote-review" })).length).toBeGreaterThan(0);
    expect(reads.filter(root => root === "C:/B")).toHaveLength(4);
    expect(reads.filter(root => root === "C:/marketplace")).toHaveLength(aReads);
    expect(useAppStore.getState().availableSkills.some(skill => skill.name === "C:/B-local")).toBe(true);
    expect(useAppStore.getState().draft).toBe("Keep draft");
    expect(vi.mocked(sendClientCommand).mock.calls.filter(([command]) => command.type === "skills.list" && command.workspace_root === "C:/B")).toHaveLength(1);
    expect(writes).toBe(1);
  });
});
