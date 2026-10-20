/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { SkillsMarketplace } from "./SkillsMarketplace";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "./ToastContainer";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { writable: true, value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("../protocol/api", async original => ({
  ...await original<typeof import("../protocol/api")>(),
  apiBase: () => "http://test.local", authHeaders: (headers?: HeadersInit) => headers ?? {},
  LONG_HTTP_TIMEOUT_MS: 300_000, fetchWithTimeout: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, init),
  errorMessageFromResponseText: (text: string, fallback: string) => text || fallback,
  pluginAssetResourceUrlWithToken: () => "",
}));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, pickDirectory: vi.fn() }));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("./DialogService", () => ({ showAlert: vi.fn(), showConfirm: vi.fn(async () => true), showPrompt: vi.fn(async () => "C:/skills/local") }));

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
const catalog = { skills: [{ name: "remote-review", title: "Remote Review", description: "Review repositories", installed: false }], source_status: { openai_skills: { ok: true } } };

const docs = { name: "docs", display_name: "文档处理", description: "处理文档", source_level: "user", path: "C:/skills/docs/SKILL.md", default_prompt: "Use the document workflow" };
const internal = { name: "internal", display_name: "内部技能", description: "模型调用", source_level: "builtin", user_invocable: false, path: "C:/skills/internal/SKILL.md" };
let docsEnabled = true;
const settingsPayload = (root = "C:/marketplace") => ({ skills: [{ ...docs, enabled: docsEnabled }], plugin_skills: [], available_skills: docsEnabled ? [docs, internal] : [internal], workspace_root: root });
const rawSkill = "---\nname: docs\ndescription: Actual document workflow\n---\n# Document workflow\n\nFull original Skill instructions.\n";

describe("Plugins and skills workspace", () => {
  beforeEach(() => {
    useAppStore.setState({ skillsMarketplaceOpen: true, skillsMarketplaceTab: "skills", skillsMarketplaceReturnTarget: "app", settingsOpen: false,
      workingDirectory: "C:/marketplace", conversationId: "catalog-owner", draft: "Keep draft", availableSkills: [docs, internal],
      marketplaceSkills: [], selectedSkills: [] });
    docsEnabled = true;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname === "/api/skills/settings") {
        if (init?.method === "PUT") docsEnabled = JSON.parse(String(init.body)).enabled;
        return Promise.resolve(json(settingsPayload(url.searchParams.get("workspace_root") || "C:/marketplace")));
      }
      if (url.pathname === "/api/skills/detail") return Promise.resolve(json({ ...docs, enabled: docsEnabled, content: "# Document workflow\n\nFull original Skill instructions.", raw_content: rawSkill }));
      if (String(input).includes("/api/plugins/marketplaces")) return Promise.resolve(json({ marketplaces: [] }));
      if (String(input).split("?")[0].endsWith("/api/plugins")) return Promise.resolve(json({ plugins: [] }));
      return Promise.resolve(json(catalog));
    }));
  });
  afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });

  it("renders the sidebar-compatible page and authentic local source filters", async () => {
    render(<SkillsMarketplace />);
    expect(screen.getByRole("main", { name: "插件与技能" })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(within(screen.getByRole("region", { name: "已安装技能" })).getByText("文档处理")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "个人", exact: true }));
    expect(within(screen.getByRole("region", { name: "个人技能" })).getByText("文档处理")).toBeTruthy();
  });
  it("loads the public directory only when it is opened", () => {
    render(<SkillsMarketplace />);
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining("/api/extensions/marketplace"), expect.anything());
    fireEvent.click(screen.getByRole("button", { name: "公开", exact: true }));
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/extensions/marketplace"), expect.anything());
  });
  it("opens complete row details and keeps the Add menu state separate from row actions", async () => {
    render(<SkillsMarketplace />);
    fireEvent.click(screen.getByRole("button", { name: "查看技能详情 docs" }));
    const detail = screen.getByRole("dialog", { name: "文档处理" });
    expect(await within(detail).findByText("Full original Skill instructions.")).toBeTruthy();
    fireEvent.click(within(detail).getByRole("button", { name: "关闭技能详情" }));
    fireEvent.click(screen.getByRole("button", { name: "管理技能 docs" }));
    expect(screen.getByRole("button", { name: "添加", exact: true }).getAttribute("aria-expanded")).toBe("false");
  });
  it("takes a selected skill to the composer even when opened from settings over a maximized editor", async () => {
    useAppStore.setState({ skillsMarketplaceReturnTarget: "settings", panelSlots: [
      { id: "chat", kind: "chat", focused: false }, { id: "editor", kind: "editor", focused: true, maximized: true },
    ] });
    const focus = vi.fn();
    window.addEventListener("composer:focus", focus, { once: true });
    render(<SkillsMarketplace />);
    fireEvent.click(screen.getByRole("button", { name: "管理技能 docs" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "用于下一条消息" }));
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(false);
    expect(useAppStore.getState().settingsOpen).toBe(false);
    expect(useAppStore.getState().selectedSkills[0].name).toBe("docs");
    expect(useAppStore.getState().panelSlots.find((slot) => slot.id === "chat")?.focused).toBe(true);
    expect(useAppStore.getState().panelSlots.some((slot) => slot.maximized)).toBe(false);
    await waitFor(() => expect(focus).toHaveBeenCalledOnce());
  });
  it("displays the remote catalog and installs through the real skill API contract", async () => {
    render(<SkillsMarketplace />);
    fireEvent.click(screen.getByRole("button", { name: "公开", exact: true }));
    fireEvent.click(await screen.findByRole("button", { name: "安装技能 remote-review" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/skills/install", expect.objectContaining({ method: "POST", body: JSON.stringify({ skill_name: "remote-review" }) })));
    expect(sendClientCommand).toHaveBeenCalledWith({ type: "skills.list" }, { silent: true });
  });
  it("keeps installed skills usable when the public catalog fails", async () => {
    const originalFetch = fetch;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input).includes("/api/extensions/marketplace") ? Promise.resolve(json({ detail: "offline" }, 503)) : originalFetch(input, init)));
    render(<SkillsMarketplace />);
    fireEvent.click(screen.getByRole("button", { name: "公开", exact: true }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "管理技能 docs" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "用于下一条消息" }));
    expect(useAppStore.getState().selectedSkills).toEqual([expect.objectContaining({ name: "docs", path: "C:/skills/docs/SKILL.md" })]);
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(false);
  });
  it("honors user_invocable and removes only personal skills", async () => {
    render(<SkillsMarketplace />);
    fireEvent.click(screen.getAllByRole("button", { name: "管理技能 internal" })[0]);
    expect((screen.getByRole("menuitem", { name: "用于下一条消息" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("menuitem", { name: "卸载技能" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "管理技能 docs" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "卸载技能" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith("http://test.local/api/skills/docs", expect.objectContaining({ method: "DELETE" })));
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
    fireEvent.click(screen.getByRole("button", { name: "公开", exact: true }));
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

  it("shows each installed skill once when filtering its source", () => {
    render(<SkillsMarketplace />);
    expect(screen.getAllByRole("button", { name: "查看技能详情 internal" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "系统", exact: true }));
    expect(screen.getAllByRole("button", { name: "查看技能详情 internal" })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "查看技能详情 docs" })).toBeNull();
  });

  it("opens the complete installed Skill body and keeps list filters", async () => {
    render(<SkillsMarketplace />);
    fireEvent.change(screen.getByRole("textbox", { name: "搜索技能" }), { target: { value: "文档" } });
    fireEvent.click(screen.getByRole("button", { name: "查看技能详情 docs" }));
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
    fireEvent.click(await screen.findByRole("button", { name: "查看技能详情 docs" }));
    const detail = screen.getByRole("dialog", { name: "文档处理" });
    expect(await within(detail).findByText("Full original Skill instructions.")).toBeTruthy();
    expect((within(detail).getByRole("button", { name: "立即试用" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(detail).getByRole("switch", { name: "启用 文档处理" }));
    await waitFor(() => expect((within(detail).getByRole("button", { name: "立即试用" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(within(detail).getByRole("button", { name: "立即试用" }));
    expect(useAppStore.getState().selectedSkills).toMatchObject([{ name: "docs", path: docs.path }]);
    expect(useAppStore.getState().draft).toBe("Keep draft\nUse the document workflow");
    expect(vi.mocked(sendClientCommand).mock.calls.some(([command]) => command.type === "user_message")).toBe(false);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
