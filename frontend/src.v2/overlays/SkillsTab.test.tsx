/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { SkillsTab } from "./SkillsTab";
import type { SettingsSkill } from "./SkillDetailsDialog";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), toast: vi.fn() }));
vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { writable: true,
    value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) });
});
vi.mock("../protocol/api", async original => ({ ...await original<typeof import("../protocol/api")>(),
  apiBase: () => "http://test.local", authHeaders: (headers?: HeadersInit) => headers || {}, fetchWithTimeout: mocks.fetch }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));
const one: SettingsSkill = { name: "same", display_name: "First", description: "Review files", path: "C:/skills/first/SKILL.md", source_level: "user", enabled: true, uninstall_name: "first" };
const two: SettingsSkill = { name: "same", display_name: "Second", description: "Write tests", path: "C:/skills/second/SKILL.md", source_level: "user", enabled: false, uninstall_name: "second" };
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const payload = (skills: SettingsSkill[] = [one, two], root = "C:/A") => ({ skills, plugin_skills: [], available_skills: skills.filter(skill => skill.enabled), workspace_root: root });

describe("Skills settings inventory", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.fetch.mockResolvedValue(response(payload()));
    useAppStore.setState({ workingDirectory: "C:/A", availableSkills: [], draft: "Keep my draft", selectedSkills: [] });
  });
  afterEach(cleanup);

  it("retains disabled user skills and counts the same inventory while searching", async () => {
    const count = vi.fn();
    const view = render(<SkillsTab onSkillsChange={count} />);
    expect((await screen.findByRole("switch", { name: "停用 First" })).getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("switch", { name: "启用 Second" }).getAttribute("aria-checked")).toBe("false");
    expect(count).toHaveBeenLastCalledWith(2);
    view.rerender(<SkillsTab search="write" onSkillsChange={count} />);
    expect(screen.queryByRole("switch", { name: "停用 First" })).toBeNull();
    expect(screen.getByRole("switch", { name: "启用 Second" })).toBeTruthy();
    expect(count).toHaveBeenLastCalledWith(2);
    expect(screen.queryByRole("button", { name: "使用" })).toBeNull();
    expect(screen.queryByRole("button", { name: "管理技能" })).toBeNull();
  });

  it("writes the exact path and refreshes active capabilities from the effective response", async () => {
    mocks.fetch.mockImplementation(async (_url, init) => init?.method === "PUT"
      ? response(payload([one, { ...two, enabled: true }])) : response(payload()));
    render(<SkillsTab />);
    fireEvent.click(await screen.findByRole("switch", { name: "启用 Second" }));
    await waitFor(() => expect(screen.getByRole("switch", { name: "停用 Second" }).getAttribute("aria-checked")).toBe("true"));
    const update = mocks.fetch.mock.calls.find(([, init]) => init?.method === "PUT")!;
    expect(JSON.parse(update[1].body)).toEqual({ path: two.path, enabled: true, workspace_root: "C:/A" });
    expect(useAppStore.getState().availableSkills.map(skill => skill.path)).toEqual([one.path, two.path]);
    expect(useAppStore.getState().draft).toBe("Keep my draft");
  });

  it("keeps a configuration failure visible and restores the original switch state", async () => {
    mocks.fetch.mockImplementation(async (_url, init) => init?.method === "PUT"
      ? new Response("Blocked by configuration hook", { status: 400 }) : response(payload()));
    render(<SkillsTab />);
    fireEvent.click(await screen.findByRole("switch", { name: "启用 Second" }));
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith("Blocked by configuration hook", "error"));
    expect(screen.getByRole("switch", { name: "启用 Second" }).getAttribute("aria-checked")).toBe("false");
    expect(screen.getByRole("switch", { name: "启用 Second" })).toHaveProperty("disabled", false);
  });

  it("does not replace another workspace's catalog when the prior toggle completes", async () => {
    let finish!: (response: Response) => void;
    mocks.fetch.mockImplementation(async (url, init) => init?.method === "PUT" ? new Promise<Response>(resolve => { finish = resolve; })
      : response(payload([one, two], new URL(url).searchParams.get("workspace_root")!)));
    render(<SkillsTab />);
    fireEvent.click(await screen.findByRole("switch", { name: "启用 Second" }));
    act(() => useAppStore.setState({ workingDirectory: "C:/B", availableSkills: [{ name: "workspace-b", description: "B" }] }));
    await screen.findByRole("switch", { name: "启用 Second" });
    await act(async () => finish(response(payload([one, { ...two, enabled: true }]))));
    expect(useAppStore.getState().availableSkills.map(skill => skill.name)).toEqual(["workspace-b"]);
    expect(screen.getByRole("switch", { name: "启用 Second" }).getAttribute("aria-checked")).toBe("false");
  });

  it("opens a disabled user's full Skill detail, re-enables its exact path and prefills the actual prompt", async () => {
    const second = { ...two, default_prompt: "Follow the second workflow" };
    let enabled = false;
    mocks.fetch.mockImplementation(async (input, init) => {
      const url = new URL(input);
      if (url.pathname === "/api/skills/detail") {
        expect(url.searchParams.get("skill_path")).toBe(two.path);
        return response({ ...second, content: "# Second\n\nComplete user Skill instructions.", raw_content: "---\nname: same\n---\nComplete original instructions." });
      }
      if (init?.method === "PUT") {
        expect(JSON.parse(init.body)).toEqual({ path: two.path, enabled: true, workspace_root: "C:/A" });
        enabled = true;
      }
      return response(payload([one, { ...second, enabled }]));
    });
    render(<SkillsTab />);
    fireEvent.click(await screen.findByRole("button", { name: "查看技能详情 Second" }));
    const dialog = screen.getByRole("dialog", { name: "Second" });
    expect(await screen.findByText("Complete user Skill instructions.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "立即试用" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(dialog).getByRole("switch", { name: "启用 Second" }));
    await waitFor(() => expect((within(dialog).getByRole("button", { name: "立即试用" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(within(dialog).getByRole("button", { name: "立即试用" }));
    expect(useAppStore.getState().selectedSkills).toMatchObject([{ name: "same", path: two.path }]);
    expect(useAppStore.getState().draft).toBe("Keep my draft\nFollow the second workflow");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("uninstalls the backend-owned directory identity and refreshes inventory without changing the draft", async () => {
    let finish!: (result: Response) => void;
    let removed = false;
    mocks.fetch.mockImplementation(async (input, init) => {
      if (init?.method === "DELETE") {
        expect(String(input)).toBe("http://test.local/api/skills/second");
        return new Promise<Response>(resolve => { finish = result => { removed = true; resolve(result); }; });
      }
      if (new URL(input).pathname === "/api/skills/detail") return response({ content: "Actual instructions", raw_content: "Actual source" });
      return response(payload(removed ? [one] : [one, two]));
    });
    useAppStore.setState({ availableSkills: [one, two], selectedSkills: [two] });
    render(<SkillsTab />);
    fireEvent.click(await screen.findByRole("button", { name: "查看技能详情 Second" }));
    const dialog = screen.getByRole("dialog", { name: "Second" });
    fireEvent.click(within(dialog).getByRole("button", { name: "卸载", exact: true }));
    expect(within(dialog).getByRole("button", { name: "卸载", exact: true })).toHaveProperty("disabled", true);
    await act(async () => finish(response({ removed: true })));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryByRole("button", { name: "查看技能详情 Second" })).toBeNull();
    expect(useAppStore.getState().availableSkills.map(skill => skill.path)).toEqual([one.path]);
    expect(useAppStore.getState().draft).toBe("Keep my draft");
    expect(useAppStore.getState().selectedSkills).toEqual([two]);
  });

  it("keeps the Skill detail and actual deletion error when uninstall is rejected", async () => {
    mocks.fetch.mockImplementation(async (input, init) => init?.method === "DELETE"
      ? new Response("Actual delete permission failure", { status: 400 })
      : response(new URL(input).pathname === "/api/skills/detail" ? { content: "Actual instructions", raw_content: "Actual source" } : payload()));
    render(<SkillsTab />);
    fireEvent.click(await screen.findByRole("button", { name: "查看技能详情 First" }));
    fireEvent.click(screen.getByRole("button", { name: "卸载", exact: true }));
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith("Actual delete permission failure", "error"));
    expect(screen.getByRole("dialog", { name: "First" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "卸载", exact: true })).toHaveProperty("disabled", false);
    expect(screen.getByRole("button", { name: "查看技能详情 First" })).toBeTruthy();
  });

  it("does not expose Uninstall for external user roots or read-only details", async () => {
    mocks.fetch.mockImplementation(async input => response(new URL(input).pathname === "/api/skills/detail"
      ? { content: "Actual instructions", raw_content: "Actual source" }
      : payload([{ ...one, plugin_enabled: false }, { ...two, uninstall_name: null }])));
    render(<SkillsTab />);
    fireEvent.click(await screen.findByRole("button", { name: "查看技能详情 Second" }));
    expect(screen.queryByRole("button", { name: "卸载", exact: true })).toBeNull();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "查看技能详情 First" }));
    expect(screen.queryByRole("button", { name: "卸载", exact: true })).toBeNull();
    expect(screen.queryByRole("button", { name: "立即试用" })).toBeNull();
  });

  it("keeps the new workspace catalog when an earlier uninstall finishes", async () => {
    let finish!: (result: Response) => void;
    mocks.fetch.mockImplementation(async (input, init) => {
      if (init?.method === "DELETE") return new Promise<Response>(resolve => { finish = resolve; });
      const url = new URL(input);
      return response(url.pathname === "/api/skills/detail" ? { content: "Actual instructions", raw_content: "Actual source" }
        : payload([one, two], url.searchParams.get("workspace_root")!));
    });
    render(<SkillsTab />);
    fireEvent.click(await screen.findByRole("button", { name: "查看技能详情 Second" }));
    fireEvent.click(screen.getByRole("button", { name: "卸载", exact: true }));
    act(() => useAppStore.setState({ workingDirectory: "C:/B", availableSkills: [{ name: "workspace-b", description: "B" }] }));
    await screen.findByRole("button", { name: "查看技能详情 Second" });
    await act(async () => finish(response({ removed: true })));
    expect(useAppStore.getState().availableSkills.map(skill => skill.name)).toEqual(["workspace-b"]);
    expect(useAppStore.getState().draft).toBe("Keep my draft");
  });
});
