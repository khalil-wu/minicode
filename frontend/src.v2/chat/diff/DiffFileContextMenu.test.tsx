// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DesktopPreferences } from "../../desktop/runtime";
import { DiffFileContextMenu, useDiffRequestChanges } from "./DiffFileContextMenu";

const opens = vi.hoisted(() => ({ preferred: vi.fn(), web: vi.fn(), internal: vi.fn() }));
vi.mock("../../lib/open-preferred-target", async original => ({
  ...await original<typeof import("../../lib/open-preferred-target")>(), openPreferredFile: opens.preferred, openPreferredWebLink: opens.web,
}));
vi.mock("../../workspace/openWorkspaceFolder", async original => ({
  ...await original<typeof import("../../workspace/openWorkspaceFolder")>(), openWorkspaceSourceFile: opens.internal,
}));

const prefs = {
  fileOpenTarget: "cursor", fileOpenTargetPerPath: {},
} as DesktopPreferences;
const targets = {
  fileOpenTargets: [
    { id: "minicode", label: "MiniCode", kind: "editor" },
    { id: "cursor", label: "Cursor", kind: "editor" },
    { id: "system", label: "Default application", kind: "systemDefault" },
    { id: "terminal", label: "Terminal", kind: "terminal", hidden: true },
  ],
  terminalShellTargets: [],
};
vi.mock("../../hooks/useDesktopPreferenceTargets", () => ({ useDesktopPreferenceTargets: () => ({ targets }) }));
const originalRuntime = window.__MINICODE_RUNTIME__;
const clipboard = vi.fn();
beforeEach(() => {
  localStorage.clear(); clipboard.mockReset().mockResolvedValue(undefined);
  opens.preferred.mockReset().mockResolvedValue(undefined); opens.web.mockReset().mockResolvedValue(true); opens.internal.mockReset();
  window.__MINICODE_RUNTIME__ = { desktop: { platformInfo: { isDesktop: true } } as any };
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: clipboard } });
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); window.getSelection()!.removeAllRanges(); window.__MINICODE_RUNTIME__ = originalRuntime; vi.unstubAllGlobals(); });

function row() { return screen.getByText("Real code row", { exact: true }); }
function openMenu() { fireEvent.contextMenu(row(), { clientX: 120, clientY: 160 }); }
function openSubmenu(name: string) { const item = screen.getByRole("menuitem", { name, exact: true }); item.focus(); fireEvent.keyDown(item, { key: "ArrowRight" }); }
function listener(handler: (location?: { side: "left" | "right"; line: number }) => void) {
  return function Listener() { useDiffRequestChanges(handler); return <span data-diff-line-number="73" data-diff-line-side="left">Real code row</span>; };
}

describe("Source Diff file context menu", () => {
  it("keeps the Source item order and the captured real line for primary opening without persisting a target", () => {
    render(<DiffFileContextMenu path="src/app.ts" workspaceRoot="C:/work" firstChangedLine={11} preferences={prefs} githubUrl="https://github.com/example/repo/blob/main/src/app.ts">
      <span data-diff-line-number="73" data-diff-line-side="left">Real code row</span>
    </DiffFileContextMenu>);
    openMenu();
    expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual([
      "请求更改", "在“Cursor”中打开", "打开方式", "在 GitHub 中打开", "复制所选内容", "复制路径", "复制相对路径", "切换自动换行",
    ]);
    expect((screen.getByRole("menuitem", { name: "复制所选内容" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("menuitem", { name: /折叠|展开文件/ })).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "在“Cursor”中打开" }));
    expect(opens.preferred).toHaveBeenCalledWith("src/app.ts", "C:/work", 73, undefined, { targetId: "cursor" });
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("registers the real annotation action, passes the picked side/line, and releases the old callback", () => {
    const old = vi.fn(), next = vi.fn(), Old = listener(old), Next = listener(next);
    const view = render(<DiffFileContextMenu path="a.ts" workspaceRoot="C:/work" preferences={null}><Old /></DiffFileContextMenu>);
    openMenu(); fireEvent.click(screen.getByRole("menuitem", { name: "请求更改" }));
    expect(old).toHaveBeenCalledWith({ side: "left", line: 73 });
    view.rerender(<DiffFileContextMenu path="a.ts" workspaceRoot="C:/work" preferences={null}><Next /></DiffFileContextMenu>);
    openMenu(); fireEvent.click(screen.getByRole("menuitem", { name: "请求更改" }));
    expect(next).toHaveBeenCalledWith({ side: "left", line: 73 }); expect(old).toHaveBeenCalledOnce();
    view.rerender(<DiffFileContextMenu path="a.ts" workspaceRoot="C:/work" preferences={null}><span>Real code row</span></DiffFileContextMenu>);
    openMenu(); fireEvent.click(screen.getByRole("menuitem", { name: "请求更改" }));
    expect(next).toHaveBeenCalledOnce();
  });

  it("keeps no-line request changes unlocated while opening a new tab at the actual first changed line", () => {
    const request = vi.fn();
    function Plain() { useDiffRequestChanges(request); return <span>Real code row</span>; }
    render(<DiffFileContextMenu path="a.ts" workspaceRoot="C:/work" firstChangedLine={19} preferences={null}><Plain /></DiffFileContextMenu>);
    openMenu(); fireEvent.click(screen.getByRole("menuitem", { name: "请求更改" }));
    expect(request).toHaveBeenCalledWith(undefined);
    openMenu(); openSubmenu("打开方式");
    fireEvent.click(screen.getByRole("menuitem", { name: "新标签页" }));
    expect(opens.internal).toHaveBeenCalledWith("a.ts", "C:/work", 19);
  });

  it("keeps hidden catalog targets in Open with, persists only editor choices and adds no preferred checkmarks", () => {
    render(<DiffFileContextMenu path="src/a.ts" workspaceRoot="C:/work" preferences={prefs}><span>Real code row</span></DiffFileContextMenu>);
    openMenu(); openSubmenu("打开方式");
    const submenu = screen.getAllByRole("menu").find(menu => within(menu).queryByRole("menuitem", { name: "新标签页" }))!;
    expect(within(submenu).getAllByRole("menuitem").map(item => item.textContent)).toEqual(["新标签页", "MiniCode", "Cursor", "Default application", "Terminal"]);
    expect(screen.queryByRole("menuitemradio")).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "MiniCode", exact: true }));
    expect(opens.preferred).toHaveBeenLastCalledWith("src/a.ts", "C:/work", 1, undefined, { targetId: "minicode", persistPreferredTargetPath: "C:/work" });
    openMenu(); openSubmenu("打开方式");
    fireEvent.click(screen.getByRole("menuitem", { name: "Terminal", exact: true }));
    expect(opens.preferred).toHaveBeenLastCalledWith("src/a.ts", "C:/work", 1, undefined, { targetId: "terminal" });
  });

  it("copies the trimmed selection captured before menu focus changes and keeps absolute and relative paths distinct", () => {
    const { container } = render(<DiffFileContextMenu path="C:\\work\\src\\你好 file.ts" workspaceRoot="c:/WORK" preferences={null}>
      <span>  Selected code  </span>
    </DiffFileContextMenu>);
    const selected = screen.getByText("Selected code"), range = document.createRange(); range.selectNodeContents(selected);
    window.getSelection()!.addRange(range);
    fireEvent.contextMenu(selected);
    window.getSelection()!.removeAllRanges();
    fireEvent.click(screen.getByRole("menuitem", { name: "复制所选内容" }));
    expect(clipboard).toHaveBeenLastCalledWith("Selected code");
    fireEvent.contextMenu(container.querySelector("span")!);
    fireEvent.click(screen.getByRole("menuitem", { name: "复制路径" }));
    expect(clipboard).toHaveBeenLastCalledWith("C:/work/src/你好 file.ts");
    fireEvent.contextMenu(container.querySelector("span")!);
    fireEvent.click(screen.getByRole("menuitem", { name: "复制相对路径" }));
    expect(clipboard).toHaveBeenLastCalledWith("src/你好 file.ts");
  });

  it("retains only actual internal capabilities on a remote host and does not create a new tab for a deleted file", () => {
    window.__MINICODE_RUNTIME__ = { ...window.__MINICODE_RUNTIME__, remoteHost: { id: "remote", displayName: "Remote", kind: "ssh", platform: "linux", home: "/home/user", generation: "remote-one" } };
    const view = render(<DiffFileContextMenu path="a.ts" workspaceRoot="/repo" preferences={prefs}><span>Real code row</span></DiffFileContextMenu>);
    openMenu(); expect(screen.queryByRole("menuitem", { name: "在“Cursor”中打开" })).toBeNull();
    openSubmenu("打开方式");
    expect(screen.queryByRole("menuitem", { name: "Cursor", exact: true })).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "新标签页" }));
    expect(opens.internal).toHaveBeenCalledWith("a.ts", "/repo", 1);
    view.rerender(<DiffFileContextMenu path="a.ts" workspaceRoot="/repo" deleted preferences={prefs}><span>Real code row</span></DiffFileContextMenu>);
    openMenu(); expect(screen.queryByRole("menuitem", { name: "打开方式" })).toBeNull();
  });

  it("gates file opening without fabricating unavailable targets and uses only the supplied real GitHub URL", () => {
    render(<DiffFileContextMenu path="src/a.ts" workspaceRoot="C:/work" canOpenFile={false} preferences={prefs} githubUrl="https://github.com/actual/repo">
      <span>Real code row</span>
    </DiffFileContextMenu>);
    openMenu();
    expect(screen.queryByRole("menuitem", { name: "打开方式" })).toBeNull();
    expect((screen.getByRole("menuitem", { name: "复制路径" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("menuitem", { name: "在 GitHub 中打开" }));
    expect(opens.web).toHaveBeenCalledWith("https://github.com/actual/repo");
  });

  it("toggles the existing global wrap preference from a normal action rather than adding a checkbox", () => {
    render(<DiffFileContextMenu path="a.ts" workspaceRoot="C:/work" preferences={null}><span>Real code row</span></DiffFileContextMenu>);
    openMenu(); fireEvent.click(screen.getByRole("menuitem", { name: "切换自动换行" }));
    expect(localStorage.getItem("wrapCodeDiff.2")).toBe("true");
    openMenu(); expect(screen.queryByRole("menuitemcheckbox")).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "切换自动换行" }));
    expect(localStorage.getItem("wrapCodeDiff.2")).toBe("false");
  });
});
