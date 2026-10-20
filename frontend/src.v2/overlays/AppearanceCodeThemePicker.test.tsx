// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { codeThemesForVariant, loadCodeThemeSeed } from "../lib/code-themes";
import { LanguageProvider } from "../i18n";
import { AppearanceCodeThemePicker } from "./AppearanceCodeThemePicker";

const mocks = vi.hoisted(() => ({ toast: vi.fn() }));
vi.mock("./ToastContainer", () => ({ pushToast: mocks.toast }));

const colors = { accent: "#2468ac", surface: "#f3e8d7", ink: "#102030" };
afterEach(() => { cleanup(); vi.restoreAllMocks(); mocks.toast.mockClear(); });

it("uses the registered English mode label while keeping actual theme names", () => {
  render(<LanguageProvider locale="en-US"><AppearanceCodeThemePicker variant="dark" value="github" colors={colors} onSelect={vi.fn()} /></LanguageProvider>);
  const trigger = screen.getByRole("button", { name: /^Dark code theme/ });
  fireEvent.click(trigger);
  const root = screen.getByRole("menu", { name: "Dark code theme" });
  expect(within(root).getByRole("menuitemradio", { name: "ChatGPT" })).toBeTruthy();
  fireEvent.click(within(root).getByRole("menuitem", { name: "Codex themes" }));
  expect(screen.getByRole("menuitemradio", { name: "GitHub" })).toBeTruthy();
});

it("shows only actual mode-compatible directory themes and their real candidate previews", async () => {
  const seed = await loadCodeThemeSeed("github", "light");
  const { container } = render(<AppearanceCodeThemePicker variant="light" value="github" colors={colors} onSelect={vi.fn()} />);
  const trigger = screen.getByRole("button", { name: "浅色代码主题" });
  const preview = trigger.querySelector<HTMLElement>(".appearance-code-theme-preview")!;
  expect(preview.textContent).toBe("Aa");
  expect(preview.style.getPropertyValue("--code-theme-current-accent")).toBe(colors.accent);
  expect(preview.style.getPropertyValue("--code-theme-current-surface")).toBe(colors.surface);
  fireEvent.click(trigger);
  const menu = screen.getByRole("menu", { name: "浅色代码主题" });
  expect(menu.parentElement).toBe(document.body);
  expect(container.contains(menu)).toBe(false);
  expect(within(menu).getAllByRole("menuitemradio")).toHaveLength(1);
  fireEvent.click(within(menu).getByRole("menuitem", { name: "Codex 主题" }));
  const submenu = screen.getByRole("menu", { name: "Codex 主题" });
  const options = within(submenu).getAllByRole("menuitemradio");
  expect(options.map(option => option.querySelector<HTMLElement>("[data-theme-id]")!.dataset.themeId)).toEqual(codeThemesForVariant("light").filter(theme => theme.id !== "codex").map(theme => theme.id));
  expect(screen.queryByRole("menuitemradio", { name: "Dracula" })).toBeNull();
  const github = screen.getByRole("menuitemradio", { name: "GitHub" });
  expect(github.getAttribute("aria-checked")).toBe("true");
  const candidate = github.querySelector<HTMLElement>(".appearance-code-theme-preview")!;
  await waitFor(() => expect(candidate.style.getPropertyValue("--code-theme-preview-accent")).toBe(seed.accent));
  expect(candidate.style.getPropertyValue("--code-theme-preview-surface")).toBe(seed.surface);
  expect(candidate.style.getPropertyValue("--code-theme-preview-border")).toBe(`color-mix(in srgb, ${seed.ink} 16%, ${seed.surface})`);
  fireEvent.keyDown(github, { key: "Escape" });
  expect(screen.queryByRole("menu")).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

it("selects by keyboard, keeps the controlled value and exposes the actual failed write", async () => {
  const error = new Error("theme setting write failed");
  const onSelect = vi.fn().mockRejectedValueOnce(error);
  const logged = vi.spyOn(console, "error").mockImplementation(() => {});
  render(<AppearanceCodeThemePicker variant="dark" value="github" colors={colors} onSelect={onSelect} />);
  const trigger = screen.getByRole("button", { name: "深色代码主题" });
  trigger.focus(); fireEvent.click(trigger);
  const menu = screen.getByRole("menu", { name: "深色代码主题" });
  fireEvent.keyDown(menu, { key: "End" });
  expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Codex 主题" }));
  fireEvent.keyDown(document.activeElement!, { key: "ArrowRight" });
  const submenu = screen.getByRole("menu", { name: "Codex 主题" });
  await waitFor(() => expect(document.activeElement).toBe(within(submenu).getByRole("menuitemradio", { name: "Absolutely" })));
  for (let index = 0; index < 3; index += 1) fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
  expect(document.activeElement).toBe(within(submenu).getByRole("menuitemradio", { name: "Dracula" }));
  await act(async () => { fireEvent.click(document.activeElement!); });
  expect(onSelect).toHaveBeenCalledExactlyOnceWith("dracula");
  expect(screen.queryByRole("menu")).toBeNull();
  expect(screen.getByRole("button", { name: "深色代码主题" })).toBe(trigger);
  expect(document.activeElement).toBe(trigger);
  expect(logged).toHaveBeenCalledWith("Code theme selection failed", error);
  expect(mocks.toast).toHaveBeenCalledWith("无法应用深色代码主题", "error");
});
