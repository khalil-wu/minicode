/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Profiler } from "react";

const fsListTree = vi.fn();
const fsSearchFiles = vi.fn();
const listWorkspaceTree = vi.fn();
const searchWorkspaceFiles = vi.fn();
const isDesktop = vi.fn(() => true);

vi.mock("../desktop/runtime", () => ({
  desktop: () => undefined,
  isDesktop: () => isDesktop(),
  fsListTree: (path: string) => fsListTree(path),
  fsSearchFiles: (...args: unknown[]) => fsSearchFiles(...args),
}));

vi.mock("../protocol/workspace", () => ({
  listWorkspaceTree: (...args: unknown[]) => listWorkspaceTree(...args),
  searchWorkspaceFiles: (...args: unknown[]) => searchWorkspaceFiles(...args),
}));

import { useAppStore } from "../stores";
import { MenuOverlay } from "./MenuOverlay";
import { __clearMentionFileCacheForTests } from "./mentionCache";
import { pluginAssetResourceUrlWithToken } from "../protocol/api";

describe("MenuOverlay mentions", () => {
  beforeEach(() => {
    vi.useRealTimers();
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockImplementation(() => ({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    });
    Element.prototype.scrollIntoView = vi.fn();
    fsListTree.mockReset();
    fsSearchFiles.mockReset();
    listWorkspaceTree.mockReset();
    searchWorkspaceFiles.mockReset().mockResolvedValue([]);
    isDesktop.mockReturnValue(true);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ plugins: [] }))));
    __clearMentionFileCacheForTests();
    useAppStore.setState({
      workingDirectory: "",
      conversations: [],
      selectedSkills: [],
      availableSkills: [],
      slashCommands: [],
    });
  });

  it("keeps the actual plugin label, stable icon descriptor and model identity through the mention picker", async () => {
    const plugin = { id: "computer-use@runtime", name: "figma", displayName: "电脑", marketplace: "runtime", enabled: true,
      path: "C:/plugins/computer-use", iconVariant: "composer", iconVariants: ["composer", "logo", "logo-dark"] };
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ plugins: [plugin] })));
    fsListTree.mockResolvedValue([]);
    useAppStore.setState({ workingDirectory: "C:/owner", resolvedTheme: "dark" });
    const select = vi.fn();
    render(<MenuOverlay open kind="mention" filter="@" onSelect={select} />);
    const item = await screen.findByRole("option", { name: /@电脑/ });
    expect(item.querySelector("img")?.getAttribute("src")).toBe(pluginAssetResourceUrlWithToken(plugin.path, "composer", undefined, "C:/owner"));
    fireEvent.click(item);
    expect(select).toHaveBeenCalledWith(`plugin:${encodeURIComponent(plugin.id)}`, expect.objectContaining({
      kind: "plugin", name: "电脑", configName: plugin.id, path: `plugin://${plugin.id}`, pluginPath: plugin.path, iconVariant: "composer",
    }));
  });

  it("selects an existing chat's actual title and original local deep link as URL context", async () => {
    fsListTree.mockResolvedValue([]);
    useAppStore.setState({ conversationId: "current", conversations: [{ id: "current", title: "Current" },
      { id: "other-chat", title: "继续完成 MiniCode UI 复刻", workspaceRoot: "C:/other" }] });
    const select = vi.fn();
    render(<MenuOverlay open kind="mention" filter="@复刻" onSelect={select} />);
    fireEvent.click(await screen.findByRole("option", { name: /继续完成 MiniCode UI 复刻/ }));
    expect(select).toHaveBeenCalledWith("conversation:other-chat", { kind: "url", name: "继续完成 MiniCode UI 复刻", path: "minicode://conversation?conversation_id=other-chat" });
  });

  it("localizes only the actually declared bundled Computer Use identity and preserves third-party names", async () => {
    useAppStore.setState({ workingDirectory: "C:/namespace-owner" });
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ plugins: [
      { id: "computer-use@minicode-bundled", name: "computer-use", displayName: "Computer Use", marketplace: "minicode-bundled", enabled: true },
      { id: "computer-use@personal", name: "computer-use", displayName: "我的电脑插件", marketplace: "personal", enabled: true },
      { id: "browser@personal", name: "browser", displayName: "自定义浏览器", marketplace: "personal", enabled: true },
    ] })));
    fsListTree.mockResolvedValue([]);
    const select = vi.fn();
    render(<MenuOverlay open kind="mention" filter="@" onSelect={select} />);
    fireEvent.click(await screen.findByRole("option", { name: /@电脑/ }));
    expect(select).toHaveBeenLastCalledWith(`plugin:${encodeURIComponent("computer-use@minicode-bundled")}`, expect.objectContaining({ name: "电脑", configName: "computer-use@minicode-bundled" }));
    fireEvent.click(screen.getByRole("option", { name: /@我的电脑插件/ }));
    expect(select).toHaveBeenLastCalledWith(`plugin:${encodeURIComponent("computer-use@personal")}`, expect.objectContaining({ name: "我的电脑插件", configName: "computer-use@personal" }));
    expect(screen.getByRole("option", { name: /@自定义浏览器/ })).toBeTruthy();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("does not recompute a closed menu on 32 answer frames", async () => {
    useAppStore.setState({ conversationId: "menu-perf-owner", isStreaming: true,
      messages: [{ id: "menu-perf-answer", role: "assistant", content: "", timestamp: 1, isStreaming: true }] });
    let commits = 0;
    render(<Profiler id="menu-performance" onRender={() => { commits += 1; }}><MenuOverlay open={false} kind="slash" onSelect={vi.fn()} /></Profiler>);
    await act(async () => Promise.resolve());
    commits = 0;
    for (let index = 0; index < 32; index++) act(() => {
      useAppStore.getState().appendAgentMessageDelta("menu-perf-text", ` frame-${index}`, "menu-perf-owner", "menu-perf-answer");
    });
    console.info("PERF", JSON.stringify({ scenario: "closed-composer-menu", frames: 32, menuCommits: commits }));
    expect(commits).toBe(0);
  });

  it.each(["C:/foreign-owner", ""])("requests plugin enablement for its explicit owner root '%s'", async (root) => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ plugins: [] })));
    vi.stubGlobal("fetch", fetchMock);
    fsListTree.mockResolvedValue([]); listWorkspaceTree.mockResolvedValue({ children: [] });
    useAppStore.setState({ workingDirectory: "C:/global-root" });
    render(<MenuOverlay open kind="mention" filter="@" workspaceRoot={root} onSelect={vi.fn()} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    const query = new URL(String(fetchMock.mock.calls[0][0]), window.location.origin).searchParams;
    expect(query.has("workspace_root")).toBe(true);
    expect(query.get("workspace_root")).toBe(root);
  });

  it.each(["tree", "search"])("does not cache a %s response that arrives after the picker unmounts", async (mode) => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    let complete!: (items: unknown[]) => void;
    const source = mode === "tree" ? fsListTree : fsSearchFiles;
    source.mockReturnValueOnce(new Promise((resolve) => { complete = resolve; }));
    source.mockResolvedValue([{ name: "fresh.ts", path: "fresh.ts", kind: "file", isDirectory: false }]);
    useAppStore.setState({ workingDirectory: "C:/unmount-owner" });
    const filter = mode === "tree" ? "@" : "@fresh";
    const first = render(<MenuOverlay open kind="mention" filter={filter} onSelect={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(150); });
    expect(source).toHaveBeenCalledTimes(1);
    first.unmount();
    await act(async () => complete([{ name: "late.ts", path: "late.ts", kind: "file", isDirectory: false }]));
    render(<MenuOverlay open kind="mention" filter={filter} onSelect={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(150); });
    expect(source).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("option", { name: /fresh\.ts/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /late\.ts/ })).toBeNull();
  });

  it("lets IME confirm text and leaves another dialog's Enter key alone", () => {
    const select = vi.fn();
    useAppStore.setState({ slashCommands: [{ name: "help", command: "help", label: "/help", type: "local", description: "Help" }] });
    render(<><div className="composer-container"><textarea aria-label="Draft" /><MenuOverlay open kind="slash" filter="/help" onSelect={select} /></div><input aria-label="Dialog input" /></>);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Draft" }), { key: "Enter", isComposing: true });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Dialog input" }), { key: "Enter" });
    expect(select).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Draft" }), { key: "Enter" });
    expect(select).toHaveBeenCalledWith("/help");
  });

  it("keeps files available when plugins fail and supports retrying the plugin request", async () => {
    useAppStore.setState({ workingDirectory: "C:\\workspace" });
    fsListTree.mockResolvedValue([{ name: "app.ts", path: "app.ts", isDirectory: false }]);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Plugin service unavailable" }), { status: 503 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ plugins: [{ id: "helper@local", name: "helper", marketplace: "local", enabled: true }] })));
    vi.stubGlobal("fetch", fetchMock);
    const select = vi.fn();
    render(<MenuOverlay open kind="mention" filter="@" onSelect={select} />);

    expect((await screen.findByRole("alert")).textContent).toContain("Plugin service unavailable");
    expect(screen.getByRole("option", { name: /app\.ts/ })).toBeTruthy();
    const retry = screen.getByRole("button", { name: "重试加载插件" });
    fireEvent.keyDown(retry, { key: "Enter" });
    expect(select).not.toHaveBeenCalled();
    fireEvent.click(retry);

    expect(await screen.findByRole("option", { name: /@helper/ })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fsListTree).toHaveBeenCalledTimes(1);
  });

  it("lists the active desktop workspace for an empty @ query", async () => {
    fsListTree.mockResolvedValue([
      { name: "package.json", path: "package.json", isDirectory: false },
      { name: "app", path: "app", isDirectory: true },
    ]);
    useAppStore.setState({
      workingDirectory: "C:\\projects\\build_project",
      availableSkills: [],
      slashCommands: [],
    });

    render(<MenuOverlay open kind="mention" filter="@" onSelect={() => {}} />);

    await waitFor(() => expect(fsListTree).toHaveBeenCalledWith("C:\\projects\\build_project"));
    expect(listWorkspaceTree).not.toHaveBeenCalled();
    expect(await screen.findByRole("option", { name: /package\.json/ })).toBeTruthy();
    expect(await screen.findByRole("option", { name: /app/ })).toBeTruthy();
  });

  it("debounces and caches @ file searches while typing", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    fsSearchFiles.mockResolvedValue([
      { name: "README.md", path: "README.md", kind: "file" },
    ]);
    useAppStore.setState({
      workingDirectory: "C:\\projects\\mention-cache",
      availableSkills: [],
      slashCommands: [],
    });

    const { rerender } = render(<MenuOverlay open kind="mention" filter="@r" onSelect={() => {}} />);

    rerender(<MenuOverlay open kind="mention" filter="@re" onSelect={() => {}} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(149);
    });
    expect(fsSearchFiles).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });

    expect(fsSearchFiles).toHaveBeenCalledTimes(1);
    expect(fsSearchFiles).toHaveBeenLastCalledWith("C:\\projects\\mention-cache", "re", 10, "all");
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByRole("option", { name: /README\.md/ })).toBeTruthy();

    rerender(<MenuOverlay open={false} kind="mention" filter="@re" onSelect={() => {}} />);
    rerender(<MenuOverlay open kind="mention" filter="@re" onSelect={() => {}} />);

    expect(fsSearchFiles).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("option", { name: /README\.md/ })).toBeTruthy();
  });

  it("isolates Web mention caches by workspace and clears the old list while loading", async () => {
    isDesktop.mockReturnValue(false);
    useAppStore.setState({ workingDirectory: "C:\\project-a" });
    let resolveNext!: (value: unknown) => void;
    listWorkspaceTree.mockResolvedValueOnce({ children: [{ name: "old.ts", path: "old.ts", is_dir: false }] })
      .mockReturnValueOnce(new Promise((resolve) => { resolveNext = resolve; }));
    render(<MenuOverlay open kind="mention" filter="@" onSelect={() => {}} />);
    await screen.findByRole("option", { name: /old\.ts/ });

    act(() => useAppStore.setState({ workingDirectory: "C:\\project-b" }));

    expect(screen.queryByRole("option", { name: /old\.ts/ })).toBeNull();
    await act(async () => resolveNext({ children: [{ name: "new.ts", path: "new.ts", is_dir: false }] }));
    expect(screen.getByRole("option", { name: /new\.ts/ })).toBeTruthy();
    expect(listWorkspaceTree).toHaveBeenLastCalledWith("C:\\project-b", ".");
  });

  it("keeps a file search error visible beside matching plugin suggestions", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ plugins: [{ id: "helper@local", name: "helper", marketplace: "local", description: "Helper plugin", enabled: true }] }))));
    fsListTree.mockRejectedValueOnce(new Error("Workspace access denied"));
    useAppStore.setState({ workingDirectory: "C:\\project-a" });
    render(<MenuOverlay open kind="mention" filter="@" onSelect={() => {}} />);

    expect((await screen.findByRole("alert")).textContent).toContain("Workspace access denied");
    expect(await screen.findByRole("option", { name: /helper/i })).toBeTruthy();
    expect(screen.queryByText("未找到文件")).toBeNull();
  });

  it("lists skills for the explicit $ picker without searching files", async () => {
    useAppStore.setState({
      workingDirectory: "C:\\projects\\skill-picker",
      selectedSkills: [],
      availableSkills: [
        {
          name: "openai-docs",
          description: "Use official OpenAI documentation",
          display_name: "OpenAI Docs",
          source_level: "builtin",
          triggers: ["minicode", "docs"],
          allow_implicit_invocation: false,
          mcp_dependencies: ["docs"],
        },
      ],
      slashCommands: [],
    });

    render(<MenuOverlay open kind="skill" filter="$open" onSelect={() => {}} />);

    expect(fsListTree).not.toHaveBeenCalled();
    expect(fsSearchFiles).not.toHaveBeenCalled();
    expect(screen.getByRole("option", { name: /OpenAI Docs/ })).toBeTruthy();
    expect(screen.getByText("仅显式调用 · MCP: docs")).toBeTruthy();
    expect(screen.getByText("内置")).toBeTruthy();
  });

  it("lists installed skills after entering the singular /skill picker", async () => {
    useAppStore.setState({
      selectedSkills: [],
      availableSkills: [
        { name: "openai-docs", description: "Use official OpenAI documentation", source_level: "builtin" },
      ],
      slashCommands: [
        { name: "skill", command: "skill", label: "/skill", description: "Choose a skill", type: "local" },
      ],
    });

    render(<MenuOverlay open kind="slash" filter="/skill " onSelect={() => {}} />);

    expect(screen.getByRole("option", { name: /openai-docs/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /^\/skill/ })).toBeNull();
  });

  it("prioritizes the exact /skill command over the fuzzy /skills match on Enter", async () => {
    useAppStore.setState({
      selectedSkills: [],
      availableSkills: [],
      slashCommands: [
        { name: "skills", command: "skills", label: "/skills", description: "Manage skills", type: "local" },
        { name: "skill", command: "skill", label: "/skill", description: "Choose a skill", type: "local" },
      ],
    });
    const onSelect = vi.fn();

    render(<MenuOverlay open kind="slash" filter="/skill" onSelect={onSelect} />);

    expect(screen.getByRole("option", { name: /\/skill Choose a skill/ }).getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(document, { key: "Enter" });

    expect(onSelect).toHaveBeenCalledWith("/skill");
  });

  it("keeps an exact namespaced project command selected on Enter", async () => {
    useAppStore.setState({
      selectedSkills: [],
      availableSkills: [],
      slashCommands: [
        { name: "review", command: "review", label: "/review", description: "General review", type: "template" },
        { name: "review:security", command: "review:security", label: "/review:security", description: "Security review", type: "template" },
      ],
    });
    const onSelect = vi.fn();

    render(<MenuOverlay open kind="slash" filter="/review:security" onSelect={onSelect} />);

    expect(screen.getByRole("option", { name: /\/review:security Security review/ }).getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(document, { key: "Enter" });

    expect(onSelect).toHaveBeenCalledWith("/review:security");
  });

  it("keeps skills discoverable in the root slash menu even with many commands", async () => {
    useAppStore.setState({
      workingDirectory: "C:\\projects\\slash-skills",
      selectedSkills: [],
      availableSkills: [
        { name: "browser", description: "Control the in-app browser", source_level: "personal" },
        { name: "openai-docs", description: "Use official OpenAI docs", source_level: "builtin" },
      ],
      slashCommands: Array.from({ length: 30 }, (_, index) => ({
        name: `cmd-${index}`,
        command: `cmd-${index}`,
        label: `/cmd-${index}`,
        description: `Command ${index}`,
        type: "protocol" as const,
      })),
    });

    render(<MenuOverlay open kind="slash" filter="/" onSelect={() => {}} />);

    expect(screen.getByText("技能")).toBeTruthy();
    expect(screen.getByRole("option", { name: /browser/ })).toBeTruthy();
    expect(screen.getByRole("option", { name: /openai-docs/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /cmd-29/ })).toBeNull();
  });
});
