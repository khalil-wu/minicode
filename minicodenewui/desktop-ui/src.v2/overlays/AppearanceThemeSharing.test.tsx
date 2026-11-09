// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { codeThemesForVariant } from "../lib/code-themes";
import { LanguageProvider } from "../i18n";
import { useFocusTrap } from "../hooks/useFocusTrap";
import type { ReactNode } from "react";
import { exportThemeString, type CodeThemeAppearance } from "../lib/theme-sharing";
import { AppearanceThemeSharing } from "./AppearanceThemeSharing";

const mocks = vi.hoisted(() => ({ copyText: vi.fn(), toast: vi.fn() }));
vi.mock("../lib/clipboard", () => ({ copyText: mocks.copyText }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));

const snapshot: CodeThemeAppearance = {
  codeThemeId: codeThemesForVariant("light")[0].id,
  theme: {
    accent: "#236abc", accentSource: "custom", contrast: 45, ink: "#123456", opaqueWindows: false,
    semanticColors: { diffAdded: "#00a240", diffRemoved: "#ba2623", skill: "#924ff7" }, surface: "#f3e8d7",
  },
};
beforeEach(() => { vi.clearAllMocks(); mocks.copyText.mockResolvedValue(true); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function SettingsTrap({ children }: { children: ReactNode }) {
  const ref = useFocusTrap(true);
  return <div ref={ref} tabIndex={-1}><button>返回应用</button>{children}</div>;
}

it("copies the current complete theme using the existing clipboard action", () => {
  render(<AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "复制浅色主题" }));
  expect(mocks.copyText).toHaveBeenCalledWith(exportThemeString(snapshot, "light"), "浅色主题", "已复制浅色主题");
});

it("uses registered English copy and updates the same import draft when the locale changes", () => {
  const onImport = vi.fn(() => true);
  const view = render(<LanguageProvider locale="en-US"><AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={onImport} /></LanguageProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Copy Light theme" }));
  expect(mocks.copyText).toHaveBeenCalledWith(exportThemeString(snapshot, "light"), "Light theme", "Light theme copied");
  fireEvent.click(screen.getByRole("button", { name: "Import Light theme" }));
  const input = screen.getByRole("textbox", { name: "Light theme share string" }) as HTMLInputElement;
  fireEvent.change(input, { target: { value: "unfinished share text" } });
  expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
  view.rerender(<LanguageProvider locale="zh-CN"><AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={onImport} /></LanguageProvider>);
  expect(screen.getByRole("textbox", { name: "浅色主题分享字符串" })).toBe(input);
  expect(input.value).toBe("unfinished share text");
  expect(screen.getByRole("button", { name: "取消" })).toBeTruthy();
});

it("imports inside the Settings focus trap, restores its trigger and clears on reopen", async () => {
  const onImport = vi.fn(() => true);
  render(<SettingsTrap><AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={onImport} /></SettingsTrap>);
  const trigger = screen.getByRole("button", { name: "导入浅色主题" });
  trigger.focus(); fireEvent.click(trigger);
  const input = screen.getByRole("textbox", { name: "浅色主题分享字符串" }) as HTMLInputElement;
  const submit = screen.getByRole("button", { name: "导入主题" }) as HTMLButtonElement;
  expect(document.activeElement).toBe(input);
  expect(input.placeholder).toBe(exportThemeString(snapshot, "light"));
  expect(submit.disabled).toBe(true);
  fireEvent.change(input, { target: { value: exportThemeString(snapshot, "dark") } });
  expect(submit.disabled).toBe(true);
  fireEvent.change(input, { target: { value: exportThemeString(snapshot, "light") } });
  expect(submit.disabled).toBe(false);
  await act(async () => { fireEvent.click(submit); });
  expect(onImport).toHaveBeenCalledWith("light", snapshot);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(trigger);
  expect(mocks.toast).toHaveBeenCalledWith("已导入浅色主题", "success");
  fireEvent.click(trigger);
  expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe("");
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("retains failed imports for correction, reports the actual error and permits retry", async () => {
  const error = new Error("settings write rejected");
  const onImport = vi.fn().mockRejectedValueOnce(error).mockResolvedValueOnce(true);
  const logged = vi.spyOn(console, "error").mockImplementation(() => {});
  render(<AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={onImport} />);
  fireEvent.click(screen.getByRole("button", { name: "导入浅色主题" }));
  const text = exportThemeString(snapshot, "light");
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "导入主题" })); });
  expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe(text);
  expect(screen.getByRole("textbox")).toHaveProperty("disabled", false);
  expect(logged).toHaveBeenCalledWith("Theme import failed", error);
  expect(mocks.toast).toHaveBeenCalledWith("无法导入浅色主题", "error");
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "导入主题" })); });
  expect(onImport).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("keeps an asynchronous import pending until the actual save finishes and prevents duplicate submission", async () => {
  let complete!: (committed: boolean) => void;
  const onImport = vi.fn(() => new Promise<boolean>(resolve => { complete = resolve; }));
  render(<AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={onImport} />);
  fireEvent.click(screen.getByRole("button", { name: "导入浅色主题" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: exportThemeString(snapshot, "light") } });
  const submit = screen.getByRole("button", { name: "导入主题" });
  const dialog = screen.getByRole("dialog");
  fireEvent.submit(submit.closest("form")!);
  fireEvent.submit(submit.closest("form")!);
  expect(onImport).toHaveBeenCalledTimes(1);
  expect(submit).toHaveProperty("disabled", true);
  expect(submit.getAttribute("aria-busy")).toBe("true");
  expect(screen.getByRole("textbox")).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "取消" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "关闭" })).toHaveProperty("disabled", true);
  fireEvent.keyDown(dialog, { key: "Escape" });
  fireEvent.pointerDown(dialog.parentElement!);
  expect(screen.getByRole("dialog")).toBe(dialog);
  expect(mocks.toast).not.toHaveBeenCalled();
  await act(async () => complete(true));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(mocks.toast).toHaveBeenCalledWith("已导入浅色主题", "success");
});

it("hides the import portal without clearing its unfinished string and restores it only when reopened", () => {
  const onImport = vi.fn(() => true);
  const view = render(<AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={onImport} />);
  fireEvent.click(screen.getByRole("button", { name: "导入浅色主题" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "unfinished authored theme" } });
  view.rerender(<AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={onImport} active={false} />);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(onImport).not.toHaveBeenCalled();
  view.rerender(<AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={onImport} />);
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "导入浅色主题" }));
  expect(screen.getByRole("textbox")).toHaveProperty("value", "unfinished authored theme");
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  fireEvent.click(screen.getByRole("button", { name: "导入浅色主题" }));
  expect(screen.getByRole("textbox")).toHaveProperty("value", "");
});

it("retains the original pending import owner through hide and reopen without submitting twice", async () => {
  let complete!: (committed: boolean) => void;
  const onImport = vi.fn(() => new Promise<boolean>(resolve => { complete = resolve; }));
  const view = render(<AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={onImport} />);
  fireEvent.click(screen.getByRole("button", { name: "导入浅色主题" }));
  const text = exportThemeString(snapshot, "light");
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } });
  fireEvent.submit(screen.getByRole("button", { name: "导入主题" }).closest("form")!);
  view.rerender(<AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={onImport} active={false} />);
  expect(screen.queryByRole("dialog")).toBeNull();
  view.rerender(<AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={onImport} />);
  fireEvent.click(screen.getByRole("button", { name: "导入浅色主题" }));
  expect(screen.getByRole("textbox")).toHaveProperty("value", text);
  expect(screen.getByRole("textbox")).toHaveProperty("disabled", true);
  fireEvent.submit(screen.getByRole("button", { name: "导入主题" }).closest("form")!);
  expect(onImport).toHaveBeenCalledOnce();
  await act(async () => complete(true));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(mocks.toast).toHaveBeenCalledWith("已导入浅色主题", "success");
});

it("retains an import retired by a newer edit and only announces success after a committed retry", async () => {
  const onImport = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  render(<AppearanceThemeSharing variant="light" snapshot={snapshot} onImport={onImport} />);
  fireEvent.click(screen.getByRole("button", { name: "导入浅色主题" }));
  const text = exportThemeString(snapshot, "light");
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "导入主题" })); });
  expect(screen.getByRole("textbox")).toHaveProperty("value", text);
  expect(screen.getByRole("textbox")).toHaveProperty("disabled", false);
  expect(mocks.toast).toHaveBeenCalledWith("主题已更改，未导入浅色主题。", "info");
  expect(mocks.toast).not.toHaveBeenCalledWith("已导入浅色主题", "success");
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "导入主题" })); });
  expect(onImport).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(mocks.toast).toHaveBeenCalledWith("已导入浅色主题", "success");
});
