/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { SkillsMarketplace } from "./SkillsMarketplace";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
});

const { websocketSend } = vi.hoisted(() => ({
  websocketSend: vi.fn(),
}));

vi.mock("../hooks/useWebSocket", () => ({
  getWebSocket: () => ({ send: websocketSend }),
}));

vi.mock("../protocol/ws-outbox", () => ({
  sendClientCommand: websocketSend,
}));

vi.mock("../protocol/api", () => ({
  apiBase: () => "http://test.local",
  authHeaders: (headers?: HeadersInit) => headers ?? {},
  LONG_HTTP_TIMEOUT_MS: 300_000,
  fetchWithTimeout: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, init),
}));

vi.mock("./ToastContainer", () => ({
  pushToast: vi.fn(),
}));

describe("SkillsMarketplace workspace", () => {
  beforeEach(() => {
    useAppStore.setState({
      skillsMarketplaceOpen: true,
      skillsMarketplaceReturnTarget: "app",
      settingsOpen: false,
      availableSkills: [{
        name: "docs",
        display_name: "文档处理",
        description: "处理文档",
        source_level: "user",
        active: false,
      }, {
        name: "code-review",
        display_name: "代码审查",
        description: "检查代码问题",
        source_level: "builtin",
        active: false,
      }],
      marketplaceSkills: [],
      selectedSkills: [],
    });
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/skills/install") && init?.method === "POST") {
        return Promise.resolve(new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }));
      }
      return Promise.resolve(new Response(JSON.stringify({
        skills: [],
        source_status: { openai_skills: { ok: true } },
      }), { status: 200, headers: { "content-type": "application/json" } }));
    }));
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.unstubAllGlobals();
    useAppStore.setState({ skillsMarketplaceOpen: false, skillsMarketplaceReturnTarget: "app", settingsOpen: false });
  });

  it("shows builtin and local skills in the full-page catalog", async () => {
    render(<SkillsMarketplace />);

    expect(screen.getByRole("dialog", { name: "技能" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "技能", level: 1 })).toBeTruthy();
    expect(await screen.findByText("代码审查")).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "本地" }));
    expect(screen.getByText("文档处理")).toBeTruthy();
  });

  it("offers a local skill import action instead of a network marketplace", async () => {
    render(<SkillsMarketplace />);
    expect(screen.getByRole("button", { name: "导入本地技能" })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "公开" })).toBeNull();
  });

  it("adds an installed skill to the next user turn", async () => {
    render(<SkillsMarketplace />);
    await screen.findByText("代码审查");
    fireEvent.click(screen.getByRole("tab", { name: "本地" }));
    fireEvent.click(screen.getByRole("button", { name: "使用" }));

    expect(useAppStore.getState().selectedSkills).toEqual([expect.objectContaining({
      name: "docs",
      path: undefined,
      description: "处理文档",
      sourceLevel: "user",
      kind: "skill",
    })]);
  });

  it("returns to the application from the catalog", () => {
    render(<SkillsMarketplace />);
    fireEvent.click(screen.getByRole("button", { name: "返回应用" }));
    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(false);
  });

  it("returns to the Skills settings page when opened from Settings", () => {
    useAppStore.setState({ skillsMarketplaceReturnTarget: "settings" });
    render(<SkillsMarketplace />);

    fireEvent.click(screen.getByRole("button", { name: "返回技能设置" }));

    expect(useAppStore.getState().skillsMarketplaceOpen).toBe(false);
    expect(useAppStore.getState().settingsOpen).toBe(true);
    expect(useAppStore.getState().settingsTab).toBe("skills");
  });

  // Regression: the 本地 list shows everything non-builtin, but removal is served
  // by `remove_user_skill`, which only deletes inside the user skills directory.
  // Non-`user` sources therefore got a disabled trash button whose tooltip read
  // 内置技能不能卸载 — not the reason, and not even a source in the list.
  it("explains per source why an installed skill cannot be uninstalled here", async () => {
    useAppStore.setState({
      availableSkills: [
        { name: "docs", display_name: "文档处理", description: "", source_level: "user", active: false },
        { name: "policy", display_name: "受管技能", description: "", source_level: "managed", active: false },
        { name: "from-plugin", display_name: "插件技能", description: "", source_level: "plugin", active: false },
        { name: "repo-skill", display_name: "项目技能", description: "", source_level: "workspace", active: false },
      ],
    });
    render(<SkillsMarketplace />);
    fireEvent.click(screen.getByRole("tab", { name: "本地" }));

    const removable = screen.getByRole("button", { name: "卸载技能 docs" });
    expect(removable.getAttribute("title")).toBe("卸载");
    expect((removable as HTMLButtonElement).disabled).toBe(false);

    for (const [name, reason] of [
      ["policy", "受管技能由管理员策略提供，不能在此卸载"],
      ["from-plugin", "插件提供的技能请在插件中管理"],
      ["repo-skill", "工作区技能属于项目文件，请在项目中删除"],
    ] as const) {
      const blocked = screen.getByRole("button", { name: `${name}：${reason}` });
      expect(blocked.getAttribute("title")).toBe(reason);
      expect((blocked as HTMLButtonElement).disabled).toBe(true);
    }
    expect(screen.queryByTitle("内置技能不能卸载")).toBeNull();
  });

  it("labels installed skill sources with the backend vocabulary", () => {
    useAppStore.setState({
      availableSkills: [
        { name: "policy", display_name: "受管技能", description: "", source_level: "managed", active: false },
        { name: "from-plugin", display_name: "插件技能", description: "", source_level: "plugin", active: false },
      ],
    });
    render(<SkillsMarketplace />);
    fireEvent.click(screen.getByRole("tab", { name: "本地" }));

    expect(screen.getByText("受管")).toBeTruthy();
    expect(screen.getByText("插件")).toBeTruthy();
  });
});
