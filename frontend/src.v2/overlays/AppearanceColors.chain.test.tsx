/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import { installAppThemeTokens } from "../../test/app-theme-tokens";
import { emptyAppearanceColors, applyAppearanceColors } from "../lib/appearance-colors";
import { syncSystemTheme, useAppStore } from "../stores";
import { LS, initialAppearanceColors } from "../stores/shared-helpers";
import { AppearanceColors } from "./AppearanceColors";

let removeTokens: () => void;
beforeEach(() => {
  localStorage.clear();
  removeTokens = installAppThemeTokens();
  useAppStore.setState({ appearanceColors: emptyAppearanceColors(), settingsOpen: true });
  act(() => useAppStore.getState().setThemeMode("light"));
});
afterEach(() => {
  cleanup();
  applyAppearanceColors("light", emptyAppearanceColors());
  useAppStore.setState({ appearanceColors: emptyAppearanceColors() });
  localStorage.clear(); removeTokens();
});

it("leaves default colors and typography alone, commits complete hex and restores incomplete drafts", () => {
  const original = getComputedStyle(document.documentElement).getPropertyValue("--surface-base");
  const fontValues = { family: "User Font", prose: "17px", weight: "625", scale: "1.125" };
  document.documentElement.style.setProperty("--font-app", fontValues.family);
  document.documentElement.style.setProperty("--mc-font-reading", fontValues.prose);
  document.documentElement.style.setProperty("--fw-medium", fontValues.weight);
  document.documentElement.style.setProperty("--app-text-scale", fontValues.scale);
  localStorage.setItem(LS.textScale, fontValues.scale);
  const authoredPreferences = localStorage.getItem(LS.textScale);
  render(<AppearanceColors />);
  expect(screen.getByRole("button", { name: "浅色代码主题" }).textContent).toContain("ChatGPT");
  expect(document.documentElement.style.getPropertyValue("--surface-base")).toBe("");
  const background = screen.getByRole("textbox", { name: "浅色背景颜色" });
  fireEvent.change(background, { target: { value: "#ab" } });
  expect((background as HTMLInputElement).value).toBe("#AB");
  expect(localStorage.getItem(LS.appearanceLight)).toBeNull();
  expect(getComputedStyle(document.documentElement).getPropertyValue("--surface-base")).toBe(original);
  fireEvent.blur(background);
  expect((background as HTMLInputElement).value).toBe("#FFFFFF");
  fireEvent.change(background, { target: { value: "#F3E8D7" } });
  expect(initialAppearanceColors()).toMatchObject({ light: { surface: "#f3e8d7" }, dark: null });
  expect(document.documentElement.style.getPropertyValue("--surface-base")).toBe("#f3e8d7");
  expect(document.documentElement.style.getPropertyValue("--font-app")).toBe(fontValues.family);
  expect(document.documentElement.style.getPropertyValue("--mc-font-reading")).toBe(fontValues.prose);
  expect(document.documentElement.style.getPropertyValue("--fw-medium")).toBe(fontValues.weight);
  expect(localStorage.getItem(LS.textScale)).toBe(authoredPreferences);
});

it("uses a real accessible react-colorful popover with immediate keyboard edits and Escape focus restoration", () => {
  render(<AppearanceColors />);
  fireEvent.change(screen.getByRole("textbox", { name: "浅色强调色" }), { target: { value: "#2468AC" } });
  const trigger = screen.getByRole("button", { name: "选择浅色强调色" });
  fireEvent.click(trigger);
  expect(screen.getByRole("dialog", { name: "浅色强调色" })).toBeTruthy();
  expect(document.querySelector('input[type="color"]')).toBeNull();
  const hue = screen.getByRole("slider", { name: "Hue" });
  const firstSlider = screen.getByRole("dialog", { name: "浅色强调色" }).querySelector<HTMLElement>('[role="slider"]')!;
  trigger.focus(); fireEvent.keyDown(trigger, { key: "Tab" });
  expect(document.activeElement).toBe(firstSlider);
  fireEvent.keyDown(firstSlider, { key: "Tab" });
  expect(document.activeElement).toBe(hue);
  hue.focus();
  fireEvent.keyDown(hue, { key: "ArrowRight", keyCode: 39, which: 39 });
  expect(useAppStore.getState().appearanceColors.light!.accent).toMatch(/^#[0-9a-f]{6}$/);
  expect(document.documentElement.style.getPropertyValue("--accent-primary")).toBe(useAppStore.getState().appearanceColors.light!.accent);
  fireEvent.keyDown(hue, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(trigger);
  expect(useAppStore.getState().settingsOpen).toBe(true);
  fireEvent.click(trigger);
  fireEvent.pointerDown(document.body);
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("keeps account accent choices unavailable while Custom edits the real local variant", () => {
  render(<AppearanceColors />);
  const trigger = screen.getByRole("button", { name: "浅色强调色" });
  fireEvent.click(trigger);
  const menu = screen.getByRole("menu", { name: "浅色强调色" });
  const accountOptions = [...menu.querySelectorAll<HTMLButtonElement>('button[role="menuitemradio"]')].filter(option => option.textContent !== "自定义");
  expect(accountOptions).toHaveLength(8);
  expect(accountOptions.every(option => option.disabled)).toBe(true);
  fireEvent.click(accountOptions[0]);
  expect(localStorage.getItem(LS.appearanceLight)).toBeNull();
  fireEvent.click(screen.getByRole("menuitemradio", { name: "自定义" }));
  fireEvent.change(screen.getByRole("textbox", { name: "浅色强调色" }), { target: { value: "#123ABC" } });
  expect(initialAppearanceColors()).toMatchObject({ light: { accent: "#123abc" }, dark: null });
  expect(document.documentElement.style.getPropertyValue("--accent-primary")).toBe("#123abc");
});

it("keeps both variants and follows system changes without leaking the old variant's inline palette", () => {
  let light = false;
  const previous = window.matchMedia;
  window.matchMedia = () => ({ matches: light, addEventListener() {}, removeEventListener() {} }) as MediaQueryList;
  try {
    render(<AppearanceColors />);
    act(() => useAppStore.getState().setThemeMode("dark"));
    fireEvent.change(screen.getByRole("textbox", { name: "深色前景颜色" }), { target: { value: "#BBDDEE" } });
    act(() => useAppStore.getState().setThemeMode("light"));
    expect(document.documentElement.style.getPropertyValue("--text-primary")).toBe("");
    fireEvent.change(screen.getByRole("textbox", { name: "浅色背景颜色" }), { target: { value: "#F3E8D7" } });
    act(() => useAppStore.getState().setThemeMode("system"));
    expect(document.documentElement.style.getPropertyValue("--text-primary")).toBe("#bbddee");
    light = true; act(syncSystemTheme);
    expect(useAppStore.getState().resolvedTheme).toBe("light");
    expect(document.documentElement.style.getPropertyValue("--surface-base")).toBe("#f3e8d7");
    expect(document.documentElement.style.getPropertyValue("--text-primary")).toBe("#1a1c1f");
    expect(initialAppearanceColors()).toMatchObject({ light: { surface: "#f3e8d7" }, dark: { ink: "#bbddee" } });
  } finally { window.matchMedia = previous; }
});
