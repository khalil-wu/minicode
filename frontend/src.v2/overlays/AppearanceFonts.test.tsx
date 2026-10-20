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
function Harness() {
  const [fonts, setFonts] = useState<ThemeFonts>({ ui: null, content: null, code: null });
  const change = async (next: ThemeFonts) => { native.changed(next); setFonts(next); };
  return <AppearanceFontCatalogProvider><AppearanceFonts variant="light" fonts={fonts} onChange={change} />
    <AppearanceFonts advanced variant="light" fonts={fonts} onChange={change} /></AppearanceFontCatalogProvider>;
}
beforeEach(() => { vi.clearAllMocks(); native.families.mockResolvedValue(catalog); });
afterEach(cleanup);

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
