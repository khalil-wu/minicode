/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionsTab } from "./ConnectionsTab";

const api = vi.hoisted(() => ({ list: vi.fn(), discover: vi.fn(), save: vi.fn(), saveMany: vi.fn(), connect: vi.fn(), open: vi.fn(), remove: vi.fn(), disconnect: vi.fn(), onChanged: vi.fn() }));
const clipboard = vi.hoisted(() => ({ copyText: vi.fn(), pushToast: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ desktop: () => ({ connections: api, platformInfo: { platform: "win32" } }) }));
vi.mock("../i18n", () => ({ useI18n: () => ({ t: (key: string, values: Record<string, string> = {}) => key.replace(/\{(\w+)\}/g, (_, name) => values[name]) }) }));
vi.mock("../lib/clipboard", () => ({ copyText: clipboard.copyText }));
vi.mock("./ToastContainer", () => ({ pushToast: clipboard.pushToast }));

beforeEach(() => {
  vi.clearAllMocks();
  api.list.mockResolvedValue([{ id: "host-one", kind: "ssh", displayName: "Build server", sshHost: "user@build", status: "disconnected" }]);
  api.onChanged.mockReturnValue(() => {});
  api.discover.mockResolvedValue({ sshAliases: ["build"], wslDistributions: ["Ubuntu"] });
  api.save.mockImplementation(async connection => ({ ...connection, status: "disconnected" }));
  api.saveMany.mockImplementation(async connections => connections.map((connection: Record<string, unknown>) => ({ ...connection, status: "disconnected" })));
  api.connect.mockResolvedValue(undefined);
  api.open.mockResolvedValue(undefined);
  clipboard.copyText.mockResolvedValue(true);
});
afterEach(cleanup);

describe("desktop remote connection controls", () => {
  it("sorts saved hosts by current display name for both SSH and WSL without reordering the producer catalog", async () => {
    const connections = [
      { id: "z-ssh", kind: "ssh" as const, displayName: "Zulu SSH", sshHost: "zulu", status: "disconnected" as const },
      { id: "a-ssh", kind: "ssh" as const, displayName: "Alpha SSH", sshHost: "alpha", status: "disconnected" as const },
      { id: "z-wsl", kind: "wsl" as const, displayName: "Zulu WSL", distribution: "Zulu", status: "disconnected" as const },
      { id: "a-wsl", kind: "wsl" as const, displayName: "Alpha WSL", distribution: "Alpha", status: "disconnected" as const },
    ];
    let changed!: (items: typeof connections) => void;
    api.list.mockResolvedValue(connections);
    api.discover.mockResolvedValue({ sshAliases: [], wslDistributions: ["Zulu", "Aardvark distro", "Alpha"] });
    api.onChanged.mockImplementationOnce(handler => { changed = handler; return () => {}; });
    const { container } = render(<ConnectionsTab active />);
    await screen.findByText("Zulu SSH");
    const names = () => screen.getAllByRole("button", { name: /的连接操作$/ }).map(button => button.getAttribute("aria-label"));
    expect(names()).toEqual(["Alpha SSH的连接操作", "Zulu SSH的连接操作"]);
    act(() => changed(connections.map(connection => connection.id === "z-ssh" ? { ...connection, displayName: "Aardvark SSH" } : connection)));
    expect(names()).toEqual(["Aardvark SSH的连接操作", "Alpha SSH的连接操作"]);
    fireEvent.click(screen.getByRole("button", { name: "WSL", exact: true }));
    await screen.findByText("Aardvark distro");
    expect(names()).toEqual(["Alpha WSL的连接操作", "Zulu WSL的连接操作"]);
    expect([...container.querySelectorAll(".remote-connection-row .settings-row-title")].map(row => row.textContent))
      .toEqual(["Aardvark distro", "Alpha WSL", "Zulu WSL"]);
    const discovered = screen.getByText("Aardvark distro").closest(".remote-connection-row") as HTMLElement;
    fireEvent.click(within(discovered).getByRole("button", { name: "连接", exact: true }));
    await waitFor(() => expect(api.open).toHaveBeenCalledOnce());
    expect(api.save).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ kind: "wsl", distribution: "Aardvark distro" }));
    expect(api.open).toHaveBeenCalledExactlyOnceWith(api.save.mock.calls[0][0].id);
    fireEvent.click(screen.getByRole("button", { name: "SSH", exact: true }));
    expect(screen.queryByText("Aardvark distro")).toBeNull();
    expect(names()).toEqual(["Aardvark SSH的连接操作", "Alpha SSH的连接操作"]);
    expect(connections.map(connection => connection.displayName)).toEqual(["Zulu SSH", "Alpha SSH", "Zulu WSL", "Alpha WSL"]);
  });

  it("keeps discovered WSL rows out of an empty SSH tab after switching connection types", async () => {
    api.list.mockResolvedValue([{ id: "saved-wsl", kind: "wsl", displayName: "Saved WSL", distribution: "Ubuntu", status: "disconnected" }]);
    api.discover.mockResolvedValue({ sshAliases: [], wslDistributions: ["Ubuntu", "Discovered WSL"] });
    render(<ConnectionsTab active />);
    await screen.findByText("通过 SSH 连接到远程设备");
    fireEvent.click(screen.getByRole("button", { name: "WSL", exact: true }));
    await screen.findByText("Discovered WSL");
    fireEvent.click(screen.getByRole("button", { name: "SSH", exact: true }));
    expect(screen.getByText("通过 SSH 连接到远程设备")).toBeTruthy();
    expect(screen.queryByText("Saved WSL")).toBeNull();
    expect(screen.queryByText("Discovered WSL")).toBeNull();
  });

  it("opens the selected real host without projecting a connection before the producer confirms it", async () => {
    let finish!: () => void;
    api.open.mockReturnValue(new Promise<void>(resolve => { finish = resolve; }));
    render(<ConnectionsTab active />);
    expect(await screen.findByText("Build server")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "连接" }));
    expect(api.open).toHaveBeenCalledExactlyOnceWith("host-one");
    expect(screen.getByText("未连接")).toBeTruthy();
    expect(screen.queryByText("已连接")).toBeNull();
    finish();
    await waitFor(() => expect((screen.getByRole("button", { name: "连接" }) as HTMLButtonElement).disabled).toBe(false));
  });

  it("retains the complete SSH draft when saving the configuration fails", async () => {
    api.save.mockRejectedValue(new Error("Configuration cannot be written"));
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    fireEvent.click(await screen.findByRole("button", { name: "手动添加" }));
    fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "My server" } });
    fireEvent.change(screen.getByLabelText("主机名或 SSH 别名"), { target: { value: "alice@server" } });
    fireEvent.change(screen.getByLabelText("端口"), { target: { value: "2222" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(api.save).toHaveBeenCalled());
    expect(api.save).toHaveBeenCalledWith(expect.objectContaining({ source: "manual", sshHost: "alice@server", sshPort: 2222 }));
    expect(api.connect).not.toHaveBeenCalled();
    await waitFor(() => expect((screen.getByLabelText("主机名或 SSH 别名") as HTMLInputElement).value).toBe("alice@server"));
    expect((screen.getByLabelText("显示名称") as HTMLInputElement).value).toBe("My server");
    expect((screen.getByLabelText("端口") as HTMLInputElement).value).toBe("2222");
    await waitFor(() => expect(screen.getByRole("dialog", { name: "SSH 连接" }).textContent).toContain("Configuration cannot be written"));
  });

  it("opens discovery only after success, sorts aliases and excludes already saved alias identities", async () => {
    let discover!: (value: { sshAliases: string[]; wslDistributions: string[] }) => void;
    api.list.mockResolvedValue([{ id: "saved", kind: "ssh", source: "discovered", alias: "saved-alias", sshHost: "saved-alias", displayName: "Existing", status: "disconnected" }]);
    api.discover.mockReturnValue(new Promise(resolve => { discover = resolve; }));
    render(<ConnectionsTab active />);
    await screen.findByText("Existing");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    expect(api.discover).toHaveBeenCalledExactlyOnceWith("ssh");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect((screen.getByRole("button", { name: "添加" }) as HTMLButtonElement).disabled).toBe(true);
    discover({ sshAliases: ["zulu", "saved-alias", "alpha"], wslDistributions: [] });
    const dialog = await screen.findByRole("dialog", { name: "添加 SSH 连接" });
    expect(within(dialog).getAllByRole("checkbox").map(input => input.getAttribute("aria-label"))).toEqual(["选择 alpha", "选择 zulu"]);
    expect((within(dialog).getByRole("button", { name: "添加" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the real initial discovery error without opening an editor or an empty discovery dialog", async () => {
    api.discover.mockRejectedValue(new Error("SSH configuration cannot be read"));
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    expect((await screen.findByRole("alert")).textContent).toBe("SSH configuration cannot be read");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(api.save).not.toHaveBeenCalled();
    expect(api.saveMany).not.toHaveBeenCalled();
  });

  it("refreshes without clearing checked aliases and never saves a staged alias that is no longer available", async () => {
    api.discover.mockResolvedValueOnce({ sshAliases: ["build", "old"], wslDistributions: [] })
      .mockResolvedValueOnce({ sshAliases: ["new", "old"], wslDistributions: [] });
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    const dialog = await screen.findByRole("dialog", { name: "添加 SSH 连接" });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "选择 build" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "选择 old" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "刷新" }));
    await within(dialog).findByRole("checkbox", { name: "选择 new" });
    expect(api.discover.mock.calls).toEqual([["ssh"], ["ssh"]]);
    expect((within(dialog).getByRole("checkbox", { name: "选择 old" }) as HTMLInputElement).checked).toBe(true);
    expect(within(dialog).queryByRole("checkbox", { name: "选择 build" })).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "添加" }));
    await waitFor(() => expect(api.connect).toHaveBeenCalledOnce());
    expect(api.saveMany).toHaveBeenCalledExactlyOnceWith([expect.objectContaining({ alias: "old", source: "discovered", sshHost: "old" })]);
  });

  it("retains selected aliases and the real error when refreshing fails", async () => {
    api.discover.mockResolvedValueOnce({ sshAliases: ["build"], wslDistributions: [] }).mockRejectedValueOnce(new Error("Include file is unavailable"));
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    const dialog = await screen.findByRole("dialog", { name: "添加 SSH 连接" });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "选择 build" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "刷新" }));
    expect((await within(dialog).findByRole("alert")).textContent).toBe("Include file is unavailable");
    expect((within(dialog).getByRole("checkbox", { name: "选择 build" }) as HTMLInputElement).checked).toBe(true);
  });

  it("keeps a pending save disabled when an overlapping refresh finishes", async () => {
    let finishRefresh!: (value: { sshAliases: string[]; wslDistributions: string[] }) => void;
    let finishSave!: (value: unknown) => void;
    api.discover.mockResolvedValueOnce({ sshAliases: ["build"], wslDistributions: [] })
      .mockReturnValueOnce(new Promise(resolve => { finishRefresh = resolve; }));
    api.saveMany.mockReturnValue(new Promise(resolve => { finishSave = resolve; }));
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    const dialog = await screen.findByRole("dialog", { name: "添加 SSH 连接" });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "选择 build" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "刷新" }));
    const add = within(dialog).getByRole("button", { name: "添加" }) as HTMLButtonElement;
    expect(add.disabled).toBe(false);
    fireEvent.click(add);
    finishRefresh({ sshAliases: ["build", "new"], wslDistributions: [] });
    await within(dialog).findByRole("checkbox", { name: "选择 new" });
    expect(add.disabled).toBe(true);
    expect((within(dialog).getByRole("checkbox", { name: "选择 build" }) as HTMLInputElement).disabled).toBe(true);
    expect(api.connect).not.toHaveBeenCalled();
    finishSave([{ id: "saved-build", kind: "ssh", source: "discovered", alias: "build", sshHost: "build", displayName: "build" }]);
    await waitFor(() => expect(api.connect).toHaveBeenCalledExactlyOnceWith("saved-build"));
  });

  it("atomically saves selected discovered aliases before connecting each returned real identity", async () => {
    api.discover.mockResolvedValue({ sshAliases: ["zulu", "alpha"], wslDistributions: [] });
    let finish!: (value: unknown) => void;
    api.saveMany.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    const dialog = await screen.findByRole("dialog", { name: "添加 SSH 连接" });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "选择 alpha" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "选择 zulu" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "添加" }));
    expect(api.saveMany).toHaveBeenCalledOnce();
    expect(api.save).not.toHaveBeenCalled();
    expect(api.connect).not.toHaveBeenCalled();
    finish([{ id: "actual-alpha", displayName: "alpha", kind: "ssh", source: "discovered", alias: "alpha", sshHost: "actual-alpha.internal" },
      { id: "actual-zulu", displayName: "zulu", kind: "ssh", source: "discovered", alias: "zulu", sshHost: "actual-zulu.internal" }]);
    await waitFor(() => expect(api.connect.mock.calls).toEqual([["actual-alpha"], ["actual-zulu"]]));
    expect(api.open).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText("已连接")).toBeNull();
    expect(api.saveMany.mock.calls[0][0].map((connection: Record<string, unknown>) => ({ source: connection.source, alias: connection.alias, sshHost: connection.sshHost, sshPort: connection.sshPort, identity: connection.identity }))).toEqual([
      { source: "discovered", alias: "alpha", sshHost: "alpha", sshPort: null, identity: null },
      { source: "discovered", alias: "zulu", sshHost: "zulu", sshPort: null, identity: null },
    ]);
  });

  it("connects only batch results that remain available after Source catalog resolution", async () => {
    api.discover.mockResolvedValue({ sshAliases: ["available", "filtered"], wslDistributions: [] });
    api.saveMany.mockResolvedValue([{ id: "resolved-available", kind: "ssh", source: "discovered", alias: "available", sshHost: "actual.internal", displayName: "available" }]);
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    const dialog = await screen.findByRole("dialog", { name: "添加 SSH 连接" });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "选择 available" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "选择 filtered" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "添加" }));
    await waitFor(() => expect(api.connect).toHaveBeenCalledExactlyOnceWith("resolved-available"));
    expect(api.saveMany.mock.calls[0][0]).toHaveLength(2);
    expect(api.save).not.toHaveBeenCalled();
  });

  it("does not create a connection when every saved alias is filtered from the catalog", async () => {
    api.saveMany.mockResolvedValue([]);
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    const dialog = await screen.findByRole("dialog", { name: "添加 SSH 连接" });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "选择 build" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "添加" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.saveMany).toHaveBeenCalledOnce();
    expect(api.connect).not.toHaveBeenCalled();
    expect(screen.queryByText("已连接")).toBeNull();
  });

  it("keeps failed discovered saves selected and does not start a fabricated connection", async () => {
    api.saveMany.mockRejectedValue(new Error("Cannot save SSH connection"));
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    const dialog = await screen.findByRole("dialog", { name: "添加 SSH 连接" });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "选择 build" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "添加" }));
    expect((await within(dialog).findByRole("alert")).textContent).toBe("Cannot save SSH connection");
    expect((within(dialog).getByRole("checkbox", { name: "选择 build" }) as HTMLInputElement).checked).toBe(true);
    expect(api.connect).not.toHaveBeenCalled();
    expect(api.save).not.toHaveBeenCalled();
    expect(screen.queryByText("已连接")).toBeNull();
  });

  it("does not connect a manual save that is filtered from the resolved catalog", async () => {
    api.save.mockResolvedValue(null);
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    fireEvent.click(await screen.findByRole("button", { name: "手动添加" }));
    fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "Filtered host" } });
    fireEvent.change(screen.getByLabelText("主机名或 SSH 别名"), { target: { value: "git@github.com" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.save).toHaveBeenCalledOnce();
    expect(api.connect).not.toHaveBeenCalled();
  });

  it("offers manual entry from the empty discovery state and connects only the saved identity", async () => {
    api.discover.mockResolvedValue({ sshAliases: [], wslDistributions: [] });
    api.save.mockResolvedValue({ id: "actual-manual", kind: "ssh", displayName: "Manual host", source: "manual", sshHost: "alice@manual" });
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    const dialog = await screen.findByRole("dialog", { name: "添加 SSH 连接" });
    expect(within(dialog).getByText("未找到 SSH 连接")).toBeTruthy();
    expect((within(dialog).getByRole("button", { name: "添加" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(dialog).getByRole("button", { name: "手动添加" }));
    fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "Manual host" } });
    fireEvent.change(screen.getByLabelText("主机名或 SSH 别名"), { target: { value: "alice@manual" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(api.connect).toHaveBeenCalledExactlyOnceWith("actual-manual"));
    expect(api.save).toHaveBeenCalledWith(expect.objectContaining({ source: "manual", sshHost: "alice@manual" }));
  });

  it("starts each successful Add discovery with an empty selection", async () => {
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    const dialog = await screen.findByRole("dialog", { name: "添加 SSH 连接" });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "选择 build" }));
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    const reopened = await screen.findByRole("dialog", { name: "添加 SSH 连接" });
    expect((within(reopened).getByRole("checkbox", { name: "选择 build" }) as HTMLInputElement).checked).toBe(false);
  });

  it("edits discovered display names without converting or rewriting alias provenance", async () => {
    api.list.mockResolvedValue([{ id: "alias-owner", kind: "ssh", source: "discovered", alias: "prod", sshHost: "alice@prod.internal", sshPort: 2222, identity: "/keys/prod", displayName: "Production", status: "disconnected" }]);
    render(<ConnectionsTab active />);
    await screen.findByText("Production");
    fireEvent.click(screen.getByRole("button", { name: "Production的连接操作" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "详情" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑连接" }));
    expect((screen.getByLabelText("SSH 别名") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("SSH 别名") as HTMLInputElement).value).toBe("prod");
    expect(screen.queryByLabelText("端口")).toBeNull();
    expect(screen.queryByLabelText("身份验证")).toBeNull();
    fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "Production renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.save).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ id: "alias-owner", source: "discovered", alias: "prod", sshHost: "alice@prod.internal", sshPort: 2222, identity: "/keys/prod", displayName: "Production renamed" }));
    expect(api.connect).not.toHaveBeenCalled();
  });

  it("keeps existing manual connection identity, port and authentication on an ordinary edit", async () => {
    api.list.mockResolvedValue([{ id: "legacy-owner", kind: "ssh", sshHost: "alice@legacy", sshPort: 2222, identity: "/keys/legacy", displayName: "Legacy", status: "disconnected" }]);
    render(<ConnectionsTab active />);
    await screen.findByText("Legacy");
    fireEvent.click(screen.getByRole("button", { name: "Legacy的连接操作" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "详情" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑连接" }));
    fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "Legacy renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.save).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ id: "legacy-owner", source: "manual", sshHost: "alice@legacy", sshPort: 2222, identity: "/keys/legacy", displayName: "Legacy renamed" }));
    expect(api.connect).not.toHaveBeenCalled();
  });

  it("shows resolved SSH details only from the menu and copies the actual field value", async () => {
    api.list.mockResolvedValue([{ id: "alias-owner", kind: "ssh", source: "discovered", alias: "prod", sshHost: "alice@prod.internal", sshPort: 2222, identity: "/keys/prod", appServerVersion: "0.2.0", displayName: "Production", status: "connected" }]);
    render(<ConnectionsTab active />);
    await screen.findByText("Production");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText("/keys/prod")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Production的连接操作" }));
    expect(screen.queryByRole("menuitem", { name: "编辑连接" })).toBeNull();
    fireEvent.click(await screen.findByRole("menuitem", { name: "详情" }));
    const dialog = screen.getByRole("dialog", { name: "Production" });
    expect(within(dialog).getByText("prod")).toBeTruthy();
    expect(within(dialog).getByText("alice@prod.internal")).toBeTruthy();
    expect(within(dialog).getByText("2222")).toBeTruthy();
    expect(within(dialog).getByText("/keys/prod")).toBeTruthy();
    expect(within(dialog).getByText("0.2.0")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "复制 主机" }));
    await waitFor(() => expect(clipboard.copyText).toHaveBeenCalledExactlyOnceWith("alice@prod.internal", "主机"));
    await waitFor(() => expect(clipboard.pushToast).toHaveBeenCalledWith("已复制到剪贴板", "info"));
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps absent details neutral and omits stale disconnected versions and manual aliases", async () => {
    api.list.mockResolvedValue([{ id: "manual-owner", kind: "ssh", source: "manual", sshHost: "alice@manual", sshPort: null, identity: null, appServerVersion: "stale-version", displayName: "Manual", status: "disconnected" }]);
    render(<ConnectionsTab active />);
    await screen.findByText("Manual");
    fireEvent.click(screen.getByRole("button", { name: "Manual的连接操作" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "详情" }));
    const dialog = screen.getByRole("dialog", { name: "Manual" });
    expect(within(dialog).getAllByText("—")).toHaveLength(2);
    expect(within(dialog).queryByRole("button", { name: "复制 端口" })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "复制 身份" })).toBeNull();
    expect(within(dialog).queryByText("别名")).toBeNull();
    expect(within(dialog).queryByText("版本")).toBeNull();
    expect(within(dialog).queryByText("stale-version")).toBeNull();
  });

  it("updates an open details dialog from real producer status and version events", async () => {
    const connection = { id: "live-owner", kind: "ssh", source: "manual", sshHost: "alice@live", displayName: "Live", status: "disconnected" };
    api.list.mockResolvedValue([connection]);
    render(<ConnectionsTab active />);
    await screen.findByText("Live");
    fireEvent.click(screen.getByRole("button", { name: "Live的连接操作" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "详情" }));
    const dialog = screen.getByRole("dialog", { name: "Live" });
    expect(within(dialog).queryByText("版本")).toBeNull();
    act(() => api.onChanged.mock.calls[0][0]([{ ...connection, status: "connected", appServerVersion: "0.2.1" }]));
    expect(within(dialog).getByText("0.2.1")).toBeTruthy();
    act(() => api.onChanged.mock.calls[0][0]([{ ...connection, status: "disconnected", appServerVersion: "0.2.1" }]));
    expect(within(dialog).queryByText("版本")).toBeNull();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows WSL distribution and only the actual connected app-server version", async () => {
    api.list.mockResolvedValue([{ id: "wsl-owner", kind: "wsl", distribution: "Ubuntu", displayName: "Work Linux", status: "connected", appServerVersion: "0.2.0" }]);
    render(<ConnectionsTab active />);
    fireEvent.click(screen.getByRole("button", { name: "WSL" }));
    await screen.findByText("Work Linux");
    fireEvent.click(screen.getByRole("button", { name: "Work Linux的连接操作" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "详情" }));
    const dialog = screen.getByRole("dialog", { name: "Work Linux" });
    expect(within(dialog).getByText("发行版")).toBeTruthy();
    expect(within(dialog).getByText("Ubuntu")).toBeTruthy();
    expect(within(dialog).getByText("0.2.0")).toBeTruthy();
    expect(within(dialog).queryByText("主机")).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "编辑连接" })).toBeNull();
  });

  it("opens edit from the details header while a connection is connected", async () => {
    api.list.mockResolvedValue([{ id: "connected-owner", kind: "ssh", source: "discovered", alias: "prod", sshHost: "actual.internal", displayName: "Connected", status: "connected", appServerVersion: "0.2.0" }]);
    render(<ConnectionsTab active />);
    await screen.findByText("Connected");
    fireEvent.click(screen.getByRole("button", { name: "Connected的连接操作" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "详情" }));
    const edit = screen.getByRole("button", { name: "编辑连接" }) as HTMLButtonElement;
    expect(edit.disabled).toBe(false);
    fireEvent.click(edit);
    expect(screen.queryByRole("dialog", { name: "Connected" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "SSH 连接" })).toBeTruthy();
    expect((screen.getByLabelText("SSH 别名") as HTMLInputElement).value).toBe("prod");
  });

  it("disables the details edit action only while an actual operation is pending", async () => {
    let finish!: () => void;
    api.open.mockReturnValue(new Promise<void>(resolve => { finish = resolve; }));
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    fireEvent.click(screen.getByRole("button", { name: "连接" }));
    fireEvent.click(screen.getByRole("button", { name: "Build server的连接操作" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "详情" }));
    const edit = screen.getByRole("button", { name: "编辑连接" }) as HTMLButtonElement;
    expect(edit.disabled).toBe(true);
    finish();
    await waitFor(() => expect(edit.disabled).toBe(false));
  });

  it("discovers WSL on selection, saves the selected distro and opens its returned host identity", async () => {
    api.save.mockResolvedValue({ id: "actual-wsl-owner", displayName: "Ubuntu", distribution: "Ubuntu", kind: "wsl" });
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    expect(api.discover).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "WSL" }));
    await screen.findByText("Ubuntu");
    expect(api.discover).toHaveBeenCalledExactlyOnceWith("wsl");
    fireEvent.click(screen.getByRole("button", { name: "连接" }));
    await waitFor(() => expect(api.open).toHaveBeenCalledExactlyOnceWith("actual-wsl-owner"));
    expect(api.save).toHaveBeenCalledWith(expect.objectContaining({ kind: "wsl", distribution: "Ubuntu" }));
  });

  it("does not read connections while its visited settings page is hidden", () => {
    render(<ConnectionsTab active={false} />);
    expect(api.list).not.toHaveBeenCalled();
    expect(api.onChanged).not.toHaveBeenCalled();
  });
});
