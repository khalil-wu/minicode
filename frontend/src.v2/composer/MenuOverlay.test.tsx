/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fsListTree = vi.fn();
const fsSearchFiles = vi.fn();
const listWorkspaceTree = vi.fn();
const searchWorkspaceFiles = vi.fn();
const isDesktop = vi.fn(() => true);

vi.mock("../desktop/runtime", () => ({
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
      selectedSkills: [],
      availableSkills: [],
      slashCommands: [],
    });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("keeps files available when plugins fail and supports retrying the plugin request", async () => {
    useAppStore.setState({ workingDirectory: "C:\\workspace" });
    fsListTree.mockResolvedValue([{ name: "app.ts", path: "app.ts", isDirectory: false }]);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Plugin service unavailable" }), { status: 503 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ plugins: [{ name: "helper", enabled: true }] })));
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
    vi.useFakeTimers();
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
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ plugins: [{ name: "helper", description: "Helper plugin" }] }))));
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
