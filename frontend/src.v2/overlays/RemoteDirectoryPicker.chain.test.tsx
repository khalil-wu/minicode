// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { pickDirectory, pickWorkspaceDirectory, remoteDirectoryEntries } from "../desktop/runtime";
import { RemoteDirectoryPicker } from "./RemoteDirectoryPicker";

afterEach(async () => {
  cleanup();
  await act(async () => { document.querySelectorAll('.remote-directory-dialog').forEach(dialog => fireEvent.keyDown(dialog, { key: "Escape" })); });
  delete window.__MINICODE_RUNTIME__;
});

const folder = (path: string, name: string) => ({ name, path, type: "directory" as const });
const listing = (path: string, parent: string | null, names: string[]) => ({ directoryPath: path, parentPath: parent,
  entries: names.map(name => folder(path === "/" ? `/${name}` : `${path}/${name}`, name)) });

it("browses the selected host before trust and only trusts the final confirmed Unicode folder", async () => {
  const trust = vi.fn().mockImplementation(async path => path);
  const list = vi.fn().mockImplementation(async path => path === "/home/remote"
    ? listing(path, "/home", ["中文 文件夹", ".hidden", "alpha"]) : listing(path, "/home/remote", []));
  const local = vi.fn();
  window.__MINICODE_RUNTIME__ = { remoteHost: { id: "real-host", displayName: "Remote", home: "/home/remote", platform: "linux", kind: "ssh", generation: "generation" },
    desktop: { workspaceDirectoryEntries: list, trustWorkspace: trust, pickDirectory: local, pickWorkspaceDirectory: local } as never };
  let result!: Promise<string | null>;
  await act(async () => { result = pickWorkspaceDirectory(); });
  await screen.findByRole("option", { name: "中文 文件夹" });
  expect(trust).not.toHaveBeenCalled();
  expect(screen.getAllByRole("option").map(entry => entry.textContent).at(-1)).toBe(".hidden");
  fireEvent.click(screen.getByRole("option", { name: "中文 文件夹" }));
  await waitFor(() => expect(list).toHaveBeenCalledWith("/home/remote/中文 文件夹", true));
  expect(trust).not.toHaveBeenCalled();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "使用文件夹" })); });
  expect(await result).toBe("/home/remote/中文 文件夹");
  expect(trust).toHaveBeenCalledExactlyOnceWith("/home/remote/中文 文件夹");
  expect(local).not.toHaveBeenCalled();
});

it("does not trust or switch the workspace when a browsed remote folder is cancelled", async () => {
  const trust = vi.fn();
  const list = vi.fn().mockResolvedValue(listing("/home/remote", "/home", ["project"]));
  window.__MINICODE_RUNTIME__ = { remoteHost: { id: "cancel-host", displayName: "Remote", home: "/home/remote", platform: "linux", kind: "ssh", generation: "generation" },
    desktop: { workspaceDirectoryEntries: list, trustWorkspace: trust } as never };
  let result!: Promise<string | null>;
  await act(async () => { result = pickWorkspaceDirectory(); });
  await screen.findByRole("option", { name: "project" });
  fireEvent.click(screen.getByRole("option", { name: "project" }));
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "取消" })); });
  expect(await result).toBeNull();
  expect(trust).not.toHaveBeenCalled();
});

it("uses remote Windows roots and keyboard suggestions without applying the controller's path rules", async () => {
  const root = "C:\\", project = "C:\\Projects";
  const result = vi.fn();
  const list = vi.fn().mockImplementation(async path => ({ directoryPath: path, parentPath: path === root ? null : root,
    entries: path === root ? [folder(project, "Projects"), folder(`${root}Users`, "Users")] : [] }));
  render(<RemoteDirectoryPicker platform="win32" initialDirectoryPath={root} listDirectory={list} onResult={result} />);
  await screen.findByRole("option", { name: "Projects" });
  expect((screen.getByRole("button", { name: "转到上一级文件夹" }) as HTMLButtonElement).disabled).toBe(true);
  const input = screen.getByRole("combobox", { name: "已选文件夹路径" });
  fireEvent.change(input, { target: { value: `${root}Pro` } });
  await screen.findByRole("option", { name: "Projects" });
  expect(screen.queryByRole("option", { name: "Users" })).toBeNull();
  fireEvent.keyDown(input, { key: "ArrowDown" });
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect((input as HTMLInputElement).value).toBe(project));
  expect(list).not.toHaveBeenCalledWith(project);
  expect(result).not.toHaveBeenCalled();
  expect(screen.getByRole("option", { name: "Projects" }).getAttribute("aria-selected")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "转到上一级文件夹" }));
  expect((input as HTMLInputElement).value).toBe(root);
  expect(list).not.toHaveBeenCalledWith(project);
  fireEvent.keyDown(input, { key: "ArrowDown" });
  fireEvent.keyDown(input, { key: "Enter" });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "使用文件夹" })); });
  expect(list).toHaveBeenCalledWith(project);
  expect(result).toHaveBeenCalledExactlyOnceWith(project);
});

it("keeps Linux backslashes literal and allows Enter to submit a full path without an active suggestion", async () => {
  const result = vi.fn();
  const actual = "/srv/team\\name/project";
  const parent = "/srv/team\\name";
  const list = vi.fn().mockImplementation(async path => path === parent ? listing(parent, "/srv", ["project"]) : listing(path, "/srv", []));
  render(<RemoteDirectoryPicker platform="linux" initialDirectoryPath="/srv" listDirectory={list} onResult={result} />);
  await screen.findByText("此目录中没有文件夹。");
  const input = screen.getByRole("combobox");
  fireEvent.change(input, { target: { value: actual } });
  await screen.findByRole("option", { name: "project" });
  expect(list).toHaveBeenLastCalledWith(parent);
  expect(fireEvent.keyDown(input, { key: "Enter" })).toBe(true);
  await act(async () => { fireEvent.submit(screen.getByRole("dialog")); });
  expect(result).toHaveBeenCalledExactlyOnceWith(actual);
});

it("keeps stale parent results out of a newer path and never requests relative paths", async () => {
  let oldResult!: (value: ReturnType<typeof listing>) => void;
  const list = vi.fn().mockImplementation(path => path === "/old" ? new Promise(resolve => { oldResult = resolve; }) : Promise.resolve(listing(path, "/", ["new-project"])));
  render(<RemoteDirectoryPicker platform="linux" initialDirectoryPath="/old" listDirectory={list} onResult={vi.fn()} />);
  const input = screen.getByRole("combobox");
  fireEvent.change(input, { target: { value: "/new/" } });
  await screen.findByRole("option", { name: "new-project" });
  await act(async () => { oldResult(listing("/old", "/", ["stale-project"])); });
  expect(screen.queryByRole("option", { name: "stale-project" })).toBeNull();
  fireEvent.change(input, { target: { value: "relative/project" } });
  await screen.findByText("输入完整的文件夹路径以浏览");
  expect((screen.getByRole("button", { name: "使用文件夹" }) as HTMLButtonElement).disabled).toBe(true);
  expect(list).toHaveBeenCalledTimes(2);
});

it("distinguishes a directory loading error from an empty directory and keeps the real error", async () => {
  const error = new Error("Remote desktop operation: HTTP 403 permission denied");
  const list = vi.fn().mockRejectedValue(error);
  const result = vi.fn();
  render(<RemoteDirectoryPicker platform="linux" initialDirectoryPath="/denied" listDirectory={list} onResult={result} />);
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toBe("无法加载此文件夹");
  expect(alert.title).toBe(error.message);
  expect(screen.queryByText("此目录中没有文件夹。")).toBeNull();
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "使用文件夹" })); });
  expect(result).not.toHaveBeenCalled();
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  expect(result).toHaveBeenCalledExactlyOnceWith(null);
});

it("passes native directory errors through and distinguishes local picker failure from cancellation", async () => {
  const failure = new Error("Actual SSH connection is disconnected");
  window.__MINICODE_RUNTIME__ = { desktop: { workspaceDirectoryEntries: vi.fn().mockRejectedValue(failure), pickDirectory: vi.fn().mockRejectedValueOnce(failure).mockResolvedValueOnce(null) } as never };
  await expect(remoteDirectoryEntries("/remote")).rejects.toBe(failure);
  await expect(pickDirectory()).rejects.toBe(failure);
  await expect(pickDirectory()).resolves.toBeNull();
});
