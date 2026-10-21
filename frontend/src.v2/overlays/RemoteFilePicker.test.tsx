// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { RemoteDirectoryEntries } from "../desktop/runtime";
import { RemoteFilePicker, showRemoteFilePicker } from "./RemoteFilePicker";

afterEach(async () => {
  cleanup();
  await act(async () => { document.querySelectorAll(".remote-file-dialog").forEach(dialog => fireEvent.keyDown(dialog, { key: "Escape" })); });
});

const directory = (path: string, name: string) => ({ name, path, type: "directory" as const });
const file = (path: string, name: string) => ({ name, path, type: "file" as const });
const listing = (directoryPath: string, parentPath: string | null, entries: RemoteDirectoryEntries["entries"]): RemoteDirectoryEntries => ({ directoryPath, parentPath, entries });

it("keeps file selections across remote folders and only uses folder rows to navigate", async () => {
  const result = vi.fn();
  const list = vi.fn().mockImplementation(async path => path === "/remote"
    ? listing(path, "/", [directory("/remote/资料", "资料"), file("/remote/a.txt", "a.txt")])
    : listing(path, "/remote", [file("/remote/资料/中文.txt", "中文.txt")]));
  render(<RemoteFilePicker initialDirectoryPath="/remote" platform="linux" listDirectory={list} onResult={result} />);
  await screen.findByRole("button", { name: "a.txt" });
  expect((screen.getByRole("button", { name: "选择文件" }) as HTMLButtonElement).disabled).toBe(true);
  const path = screen.getByRole("textbox", { name: "当前文件夹路径" }) as HTMLInputElement;
  expect(path.disabled).toBe(true); expect(path.readOnly).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "a.txt" }));
  expect(screen.getByRole("button", { name: "a.txt" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("button", { name: "资料" }).hasAttribute("aria-pressed")).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "资料" }));
  await screen.findByRole("button", { name: "中文.txt" });
  expect(path.value).toBe("/remote/资料");
  fireEvent.click(screen.getByRole("button", { name: "中文.txt" }));
  fireEvent.click(screen.getByRole("button", { name: "转到上一级文件夹" }));
  await screen.findByRole("button", { name: "a.txt" });
  expect(screen.getByRole("button", { name: "a.txt" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "a.txt" }));
  fireEvent.click(screen.getByRole("button", { name: "选择文件" }));
  expect(result).toHaveBeenCalledExactlyOnceWith(["/remote/资料/中文.txt"]);
  expect(list.mock.calls.map(([value]) => value)).toEqual(["/remote", "/remote/资料", "/remote"]);
});

it("preserves remote Windows paths and the server's parent root without applying controller path rules", async () => {
  const root = "C:\\", home = "C:\\Users\\远程", path = `${root}file.txt`;
  const result = vi.fn();
  const list = vi.fn().mockImplementation(async value => value === home ? listing(home, root, []) : listing(root, null, [file(path, "file.txt")]));
  render(<RemoteFilePicker initialDirectoryPath={home} platform="win32" listDirectory={list} onResult={result} />);
  await screen.findByText("此目录中没有文件或文件夹。");
  fireEvent.click(screen.getByRole("button", { name: "转到上一级文件夹" }));
  await screen.findByRole("button", { name: "file.txt" });
  expect((screen.getByRole("textbox", { name: "当前文件夹路径" }) as HTMLInputElement).value).toBe(root);
  expect((screen.getByRole("button", { name: "转到上一级文件夹" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "file.txt" }));
  fireEvent.click(screen.getByRole("button", { name: "选择文件" }));
  expect(result).toHaveBeenCalledExactlyOnceWith([path]);
  expect(list.mock.calls.map(([value]) => value)).toEqual([home, root]);
});

it("uses the server's default remote folder when no initial directory is supplied", async () => {
  const list = vi.fn().mockResolvedValue(listing("/home/remote", "/home", [file("/home/remote/.config", ".config")]));
  render(<RemoteFilePicker initialDirectoryPath="" platform="linux" listDirectory={list} onResult={vi.fn()} />);
  await screen.findByRole("button", { name: ".config" });
  expect(list).toHaveBeenCalledExactlyOnceWith(null);
  expect((screen.getByRole("textbox", { name: "当前文件夹路径" }) as HTMLInputElement).value).toBe("/home/remote");
});

it("shows the actual remote listing error separately from an empty folder and keeps an existing selection", async () => {
  const result = vi.fn();
  const failure = new Error("SSH connection lost while listing /remote/denied");
  const list = vi.fn().mockImplementation(path => path === "/remote" ? Promise.resolve(listing(path, "/", [file("/remote/original.txt", "original.txt"), directory("/remote/denied", "denied")])) : Promise.reject(failure));
  render(<RemoteFilePicker initialDirectoryPath="/remote" platform="linux" listDirectory={list} onResult={result} />);
  await screen.findByRole("button", { name: "original.txt" });
  fireEvent.click(screen.getByRole("button", { name: "original.txt" }));
  fireEvent.click(screen.getByRole("button", { name: "denied" }));
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toBe("无法加载此文件夹"); expect(alert.title).toBe(failure.message);
  expect(screen.queryByText("此目录中没有文件或文件夹。")).toBeNull();
  expect((screen.getByRole("button", { name: "选择文件" }) as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "转到上一级文件夹" }));
  await screen.findByRole("button", { name: "original.txt" });
  expect(screen.getByRole("button", { name: "original.txt" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "选择文件" }));
  expect(result).toHaveBeenCalledExactlyOnceWith(["/remote/original.txt"]);
});

it.each(["cancel", "escape", "outside"])("returns cancellation for %s without submitting retained file selections", async action => {
  const result = vi.fn(); const escaped = vi.fn();
  const list = vi.fn().mockResolvedValue(listing("/remote", "/", [file("/remote/a.txt", "a.txt")]));
  window.addEventListener("keydown", escaped);
  try {
    render(<RemoteFilePicker initialDirectoryPath="/remote" platform="linux" listDirectory={list} onResult={result} />);
    await screen.findByRole("button", { name: "a.txt" });
    fireEvent.click(screen.getByRole("button", { name: "a.txt" }));
    if (action === "cancel") fireEvent.click(screen.getByRole("button", { name: "取消" }));
    else if (action === "escape") fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    else fireEvent.mouseDown(document.querySelector(".remote-directory-backdrop")!);
    expect(result).toHaveBeenCalledExactlyOnceWith(null);
    expect(escaped).not.toHaveBeenCalled();
  } finally { window.removeEventListener("keydown", escaped); }
});

it("closes and resolves null when its original owner aborts while a listing is pending", async () => {
  const controller = new AbortController();
  let complete!: (value: RemoteDirectoryEntries) => void;
  const list = vi.fn().mockImplementation(() => new Promise<RemoteDirectoryEntries>(resolve => { complete = resolve; }));
  let result!: Promise<string[] | null>;
  await act(async () => { result = showRemoteFilePicker({ initialDirectoryPath: "/remote", platform: "linux", listDirectory: list, signal: controller.signal }); });
  await screen.findByRole("dialog", { name: "选择远程文件" });
  await act(async () => { controller.abort(); });
  await expect(result).resolves.toBeNull();
  expect(screen.queryByRole("dialog", { name: "选择远程文件" })).toBeNull();
  await act(async () => { complete(listing("/remote", "/", [file("/remote/late.txt", "late.txt")])); });
  expect(screen.queryByRole("button", { name: "late.txt" })).toBeNull();
});

it("does not mount a picker or request a directory for an already retired owner", async () => {
  const controller = new AbortController(); controller.abort(); const list = vi.fn();
  await expect(showRemoteFilePicker({ initialDirectoryPath: "/remote", platform: "linux", listDirectory: list, signal: controller.signal })).resolves.toBeNull();
  expect(list).not.toHaveBeenCalled(); expect(document.querySelector(".remote-file-dialog")).toBeNull();
});

it("unmounts the modal after confirming the original selected files", async () => {
  const controller = new AbortController();
  const list = vi.fn().mockResolvedValue(listing("/remote", "/", [file("/remote/a.txt", "a.txt")]));
  let result!: Promise<string[] | null>;
  await act(async () => { result = showRemoteFilePicker({ initialDirectoryPath: "/remote", platform: "linux", listDirectory: list, signal: controller.signal }); });
  await screen.findByRole("button", { name: "a.txt" });
  fireEvent.click(screen.getByRole("button", { name: "a.txt" }));
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "选择文件" })); });
  await expect(result).resolves.toEqual(["/remote/a.txt"]);
  await waitFor(() => expect(document.querySelector(".remote-file-dialog")).toBeNull());
  controller.abort();
});
