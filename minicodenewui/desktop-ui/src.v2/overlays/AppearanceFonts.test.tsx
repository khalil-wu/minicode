/* @vitest-environment jsdom */
import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AppearanceFontCatalogProvider, AppearanceFonts } from "./AppearanceFonts";
import type { ThemeFonts, SystemFontFamily } from "../lib/theme-fonts";

const native = vi.hoisted(() => ({ families: vi.fn(), changed: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => true, desktop: () => ({ windowControls: { listFontFamilies: native.families } }) }));
const face = (family: string, styleName: string, postscriptName: string, isMonospaced: boolean) => ({ family, styleName, postscriptName,
  fullName: `${family} ${styleName}`, weight: 400, style: "normal" as const, isMonospaced });
const catalog: SystemFontFamily[] = [
  { family: "Fixture UI", faces: [face("Fixture UI", "Book", "Fixture-Book", false), face("Fixture UI", "Condensed Medium", "Fixture-CondensedMedium", false)] },
  { family: "Fixture Mono", faces: [face("Fixture Mono", "Regular", "FixtureMono-Regular", true), face("Fixture Mono", "Medium Oblique", "FixtureMono-MediumOblique", true)] },
  { family: "Mixed family", faces: [face("Mixed family", "Mono", "Mixed-Mono", true), face("Mixed family", "Proportional", "Mixed-Proportional", false)] },
];
function Harness({ active = true }: { active?: boolean }) {
  const [fonts, setFonts] = useState<ThemeFonts>({ ui: null, content: null, code: null });
  const change = async (next: ThemeFonts) => { native.changed(next); setFonts(next); };
  return <AppearanceFontCatalogProvider><AppearanceFonts variant="light" fonts={fonts} onChange={change} active={active} />
    <AppearanceFonts advanced variant="light" fonts={fonts} onChange={change} active={active} /></AppearanceFontCatalogProvider>;
}
beforeEach(() => { vi.clearAllMocks(); native.families.mockResolvedValue(catalog); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("enumerates once and offers only the actual registered styles for the chosen family", async () => {
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: /^浅色界面字体，当前/ }));
  fireEvent.click(screen.getByRole("option", { name: "Fixture UI", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: /^浅色界面字体样式，当前/ }));
  const menu = screen.getByRole("listbox", { name: "浅色界面字体样式" });
  expect(within(menu).getAllByRole("option").map(option => option.textContent)).toEqual(["Book", "Condensed Medium"]);
  fireEvent.click(within(menu).getByRole("option", { name: "Condensed Medium" }));
  expect(native.changed).toHaveBeenLastCalledWith(expect.objectContaining({ ui: '"Fixture UI"',
    uiFace: { family: "Fixture UI", fullName: "Fixture UI Condensed Medium", postscriptName: "Fixture-CondensedMedium" } }));
  expect(native.families).toHaveBeenCalledTimes(1);
});

it("filters code families by every real face's monospace metadata", async () => {
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: /^浅色代码字体，当前/ }));
  const menu = screen.getByRole("listbox", { name: "浅色代码字体" });
  expect(within(menu).queryByRole("option", { name: "Fixture UI" })).toBeNull();
  expect(within(menu).queryByRole("option", { name: "Mixed family" })).toBeNull();
  fireEvent.click(within(menu).getByRole("option", { name: "Fixture Mono" }));
  fireEvent.click(screen.getByRole("button", { name: /^浅色代码字体样式，当前/ }));
  fireEvent.click(screen.getByRole("option", { name: "Medium Oblique" }));
  expect(native.changed).toHaveBeenLastCalledWith(expect.objectContaining({ code: '"Fixture Mono"',
    codeFace: { family: "Fixture Mono", fullName: "Fixture Mono Medium Oblique", postscriptName: "FixtureMono-MediumOblique" } }));
});

it("uses Source's manual CSS-family entry only when native enumeration is unavailable", async () => {
  native.families.mockResolvedValue(null);
  render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "浅色界面字体", exact: true }));
  fireEvent.change(screen.getByRole("textbox", { name: "浅色界面字体自定义值" }), { target: { value: '"Authored Face", serif' } });
  fireEvent.click(screen.getByRole("menuitem", { name: "使用自定义字体值" }));
  await waitFor(() => expect(native.changed).toHaveBeenLastCalledWith(expect.objectContaining({ ui: '"Authored Face", serif', uiFace: undefined })));
  expect(screen.getByRole("button", { name: /^浅色界面字体样式，当前/ })).toHaveProperty("disabled", true);
});

it("retires hidden font menus and keeps their uncommitted manual value until explicit cancellation", async () => {
  native.families.mockResolvedValue(null);
  const view = render(<Harness />);
  const trigger = await screen.findByRole("button", { name: "浅色界面字体", exact: true });
  fireEvent.keyDown(trigger, { key: "ArrowDown" });
  const input = screen.getByRole("textbox", { name: "浅色界面字体自定义值" });
  expect(document.activeElement).toBe(input);
  fireEvent.change(input, { target: { value: '"未提交字体", sans-serif' } });
  view.rerender(<Harness active={false} />);
  expect(screen.queryByRole("menu")).toBeNull();
  expect(native.changed).not.toHaveBeenCalled();
  view.rerender(<Harness />);
  expect(screen.queryByRole("menu")).toBeNull();
  fireEvent.click(trigger);
  expect(screen.getByRole("textbox")).toHaveProperty("value", '"未提交字体", sans-serif');
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Escape" });
  fireEvent.click(trigger);
  expect(screen.getByRole("textbox")).toHaveProperty("value", "");
  expect(native.families).toHaveBeenCalledTimes(1);
});

it("preserves manual input when navigation dismisses the menu before the page becomes inactive", async () => {
  native.families.mockResolvedValue(null);
  const page = (active: boolean) => <><nav className="settings-center-tabs"><button>另一个设置页</button></nav><Harness active={active} /></>;
  const view = render(page(true));
  fireEvent.click(await screen.findByRole("button", { name: "浅色界面字体", exact: true }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "pending font" } });
  fireEvent.pointerDown(screen.getByRole("button", { name: "另一个设置页" }));
  view.rerender(page(false));
  view.rerender(page(true));
  fireEvent.click(screen.getByRole("button", { name: "浅色界面字体", exact: true }));
  expect(screen.getByRole("textbox")).toHaveProperty("value", "pending font");
  expect(native.changed).not.toHaveBeenCalled();
});

it("does not apply or dismiss a manual font while the IME owns Enter or Escape", async () => {
  native.families.mockResolvedValue(null);
  render(<Harness />);
  const trigger = await screen.findByRole("button", { name: "浅色界面字体", exact: true });
  fireEvent.click(trigger);
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "思源黑体" } });
  fireEvent.keyDown(input, { key: "Enter", isComposing: true });
  fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
  fireEvent.keyDown(input, { key: "Escape", isComposing: true });
  expect(native.changed).not.toHaveBeenCalled();
  expect(screen.getByRole("textbox")).toBe(input);
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(native.changed).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ ui: "思源黑体" })));
  expect(screen.queryByRole("menu")).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

it("closes a native enumerated font selector when its settings page is hidden", async () => {
  const view = render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: /^浅色界面字体，当前/ }));
  expect(screen.getByRole("listbox")).toBeTruthy();
  view.rerender(<Harness active={false} />);
  expect(screen.queryByRole("listbox")).toBeNull();
  view.rerender(<Harness />);
  expect(screen.queryByRole("listbox")).toBeNull();
  expect(native.changed).not.toHaveBeenCalled();
});

it("keeps the manual font menu anchored through settings scrolling and detaches positioning while hidden", async () => {
  native.families.mockResolvedValue(null);
  let top = window.innerHeight - 40;
  const bounds = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function () {
    if (this.classList.contains("appearance-custom-font-trigger")) return { top, bottom: top + 28, left: 500, right: 600, width: 100, height: 28 } as DOMRect;
    if (this.classList.contains("appearance-custom-font-menu")) return { top: 0, bottom: 120, left: 0, right: 240, width: 240, height: 120 } as DOMRect;
    return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 } as DOMRect;
  });
  const view = render(<Harness />);
  fireEvent.click(await screen.findByRole("button", { name: "浅色界面字体", exact: true }));
  const menu = screen.getByRole("menu");
  expect(menu.style.top).toBe(`${top - 126}px`);
  top = 100;
  fireEvent.scroll(window);
  expect(menu.style.top).toBe("134px");
  view.rerender(<Harness active={false} />);
  bounds.mockClear();
  fireEvent.scroll(window);
  expect(bounds).not.toHaveBeenCalled();
});
