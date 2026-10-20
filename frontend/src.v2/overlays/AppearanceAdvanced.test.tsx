/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { installAppThemeTokens } from "../../test/app-theme-tokens";
import { AppearanceTab } from "./AppearanceTab";
import { useAppStore } from "../stores";
import { readAppearancePreferences } from "../stores/shared-helpers";
import { emptyAppearanceColors } from "../lib/appearance-colors";
import { emptyAppearanceThemes } from "../lib/appearance-themes";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { writable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));
vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, desktop: () => undefined }));
let removeTokens: () => void;
beforeEach(() => {
  localStorage.clear(); removeTokens = installAppThemeTokens();
  useAppStore.setState({ ...readAppearancePreferences(), appearanceColors: emptyAppearanceColors(), appearanceThemes: emptyAppearanceThemes() });
  useAppStore.getState().setThemeMode("dark");
});
afterEach(() => { cleanup(); localStorage.clear(); removeTokens(); });

it("reveals both saved variants without changing mode or copying values between them", () => {
  act(() => useAppStore.getState().setAppearanceColor("dark", "surface", "#111122"));
  render(<AppearanceTab />);
  const advanced = screen.getByText("高级").closest("summary")!; fireEvent.click(advanced);
  expect(screen.queryByRole("textbox", { name: "浅色背景颜色" })).toBeNull();
  fireEvent.click(screen.getByRole("switch", { name: "分别设置浅色和深色模式" }));
  expect(screen.getByRole("textbox", { name: "浅色背景颜色" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "深色背景颜色" })).toBeTruthy();
  fireEvent.change(screen.getByRole("textbox", { name: "浅色背景颜色" }), { target: { value: "#ddeeff" } });
  expect(useAppStore.getState().themeMode).toBe("dark");
  expect(useAppStore.getState().appearanceColors).toMatchObject({ light: { surface: "#ddeeff" }, dark: { surface: "#111122" } });
  expect(document.documentElement.style.getPropertyValue("--surface-base")).toBe("#111122");
  expect(screen.getByRole("button", { name: /^浅色界面字体样式/ })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: /^深色界面字体样式/ })).toHaveProperty("disabled", true);
});

it("exposes Source advanced values and resets them without clearing existing prose or visual preferences", () => {
  useAppStore.getState().setWorkbenchPreferences({ proseSize: 20 });
  document.documentElement.style.setProperty("--font-app", '"Authored Special Font"');
  render(<AppearanceTab />); fireEvent.click(screen.getByText("高级").closest("summary")!);
  expect(screen.getByRole("spinbutton", { name: "界面字号" })).toHaveProperty("value", "14");
  expect(screen.getByRole("spinbutton", { name: "代码字号" })).toHaveProperty("value", "12");
  expect(screen.queryByRole("spinbutton", { name: "正文字号" })).toBeNull();
  expect(screen.queryByRole("button", { name: "恢复默认字号" })).toBeNull();
  const motion = screen.getByRole("radiogroup", { name: "减少动态效果" });
  fireEvent.click(within(motion).getByRole("radio", { name: "关" }));
  expect(useAppStore.getState().reducedMotionPreference).toBe("off");
  fireEvent.click(screen.getByRole("switch", { name: "使用手形指针" }));
  fireEvent.click(within(screen.getByRole("radiogroup", { name: "差异标记" })).getByRole("radio", { name: "+/-" }));
  expect(document.documentElement.dataset.pointerCursors).toBe("true");
  expect(document.documentElement.dataset.diffMarkerStyle).toBe("symbols");
  fireEvent.click(screen.getByRole("button", { name: "恢复默认", exact: true }));
  expect(useAppStore.getState()).toMatchObject({ reducedMotionPreference: "system", pointerCursors: false, diffMarkerStyle: "color" });
  expect(useAppStore.getState().workbenchPreferences.proseSize).toBe(20);
  expect(document.documentElement.style.getPropertyValue("--font-app")).toBe('"Authored Special Font"');
});
