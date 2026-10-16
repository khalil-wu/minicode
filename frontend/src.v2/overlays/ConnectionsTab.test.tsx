/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionsTab } from "./ConnectionsTab";

const api = vi.hoisted(() => ({ list: vi.fn(), discover: vi.fn(), save: vi.fn(), open: vi.fn(), remove: vi.fn(), disconnect: vi.fn(), onChanged: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ desktop: () => ({ connections: api, platformInfo: { platform: "win32" } }) }));
vi.mock("../i18n", () => ({ useI18n: () => ({ t: (key: string, values: Record<string, string> = {}) => key.replace(/\{(\w+)\}/g, (_, name) => values[name]) }) }));

beforeEach(() => {
  vi.clearAllMocks();
  api.list.mockResolvedValue([{ id: "host-one", kind: "ssh", displayName: "Build server", sshHost: "user@build", status: "disconnected" }]);
  api.onChanged.mockReturnValue(() => {});
  api.discover.mockResolvedValue({ sshAliases: ["build"], wslDistributions: ["Ubuntu"] });
  api.open.mockResolvedValue(undefined);
});
afterEach(cleanup);

describe("desktop remote connection controls", () => {
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
    fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "My server" } });
    fireEvent.change(screen.getByLabelText("主机名或 SSH 别名"), { target: { value: "alice@server" } });
    fireEvent.change(screen.getByLabelText("端口"), { target: { value: "2222" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(api.save).toHaveBeenCalled());
    await waitFor(() => expect((screen.getByLabelText("主机名或 SSH 别名") as HTMLInputElement).value).toBe("alice@server"));
    expect((screen.getByLabelText("显示名称") as HTMLInputElement).value).toBe("My server");
    expect((screen.getByLabelText("端口") as HTMLInputElement).value).toBe("2222");
    await waitFor(() => expect(screen.getByRole("dialog", { name: "SSH 连接" }).textContent).toContain("Configuration cannot be written"));
  });

  it("discovers WSL on selection, saves the selected distro and opens its returned host identity", async () => {
    api.save.mockResolvedValue({ id: "actual-wsl-owner", displayName: "Ubuntu", distribution: "Ubuntu", kind: "wsl" });
    render(<ConnectionsTab active />);
    await screen.findByText("Build server");
    expect(api.discover).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "WSL" }));
    await screen.findByText("Ubuntu");
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
