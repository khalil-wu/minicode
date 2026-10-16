// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  expect(screen.getByRole("listbox", { name: "Dark code theme" })).toBeTruthy();
  expect(screen.getByRole("option", { name: "GitHub" })).toBeTruthy();
});

it("shows only actual mode-compatible directory themes and their real candidate previews", async () => {
  const seed = await loadCodeThemeSeed("github", "light");
  const { container } = render(<AppearanceCodeThemePicker variant="light" value="github" colors={colors} onSelect={vi.fn()} />);
  const trigger = screen.getByRole("button", { name: "浅色代码主题，当前：GitHub" });
  const preview = trigger.querySelector<HTMLElement>(".appearance-code-theme-preview")!;
  expect(preview.textContent).toBe("Aa");
  expect(preview.style.getPropertyValue("--code-theme-current-accent")).toBe(colors.accent);
  expect(preview.style.getPropertyValue("--code-theme-current-surface")).toBe(colors.surface);
  fireEvent.click(trigger);
  const menu = screen.getByRole("listbox", { name: "浅色代码主题" });
  expect(menu.parentElement).toBe(document.body);
  expect(container.contains(menu)).toBe(false);
  const options = screen.getAllByRole("option");
  expect(options.map(option => option.getAttribute("data-value"))).toEqual(codeThemesForVariant("light").map(theme => theme.id));
  expect(screen.queryByText("Codex themes")).toBeNull();
  expect(screen.queryByRole("option", { name: "Dracula" })).toBeNull();
  const github = screen.getByRole("option", { name: "GitHub" });
  expect(github.getAttribute("aria-selected")).toBe("true");
  const candidate = github.querySelector<HTMLElement>(".appearance-code-theme-preview")!;
  await waitFor(() => expect(candidate.style.getPropertyValue("--code-theme-preview-accent")).toBe(seed.accent));
  expect(candidate.style.getPropertyValue("--code-theme-preview-surface")).toBe(seed.surface);
  expect(candidate.style.getPropertyValue("--code-theme-preview-border")).toBe(`color-mix(in srgb, ${seed.ink} 16%, ${seed.surface})`);
  fireEvent.keyDown(github, { key: "Escape" });
  expect(screen.queryByRole("listbox")).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

it("selects by keyboard, keeps the controlled value and exposes the actual failed write", async () => {
  const error = new Error("theme setting write failed");
  const onSelect = vi.fn().mockRejectedValueOnce(error);
  const logged = vi.spyOn(console, "error").mockImplementation(() => {});
  render(<AppearanceCodeThemePicker variant="dark" value="github" colors={colors} onSelect={onSelect} />);
  const trigger = screen.getByRole("button", { name: "深色代码主题，当前：GitHub" });
  trigger.focus(); fireEvent.keyDown(trigger, { key: "ArrowDown" });
  const selected = screen.getByRole("option", { name: "GitHub" });
  await waitFor(() => expect(document.activeElement).toBe(selected));
  fireEvent.keyDown(selected, { key: "ArrowUp" });
  const previous = screen.getByRole("option", { name: "Everforest" });
  expect(document.activeElement).toBe(previous);
  fireEvent.keyDown(previous, { key: "ArrowUp" });
  expect(document.activeElement).toBe(screen.getByRole("option", { name: "Dracula" }));
  await act(async () => { fireEvent.keyDown(document.activeElement!, { key: "Enter" }); });
  expect(onSelect).toHaveBeenCalledExactlyOnceWith("dracula");
  expect(screen.queryByRole("listbox")).toBeNull();
  expect(screen.getByRole("button", { name: "深色代码主题，当前：GitHub" })).toBe(trigger);
  expect(document.activeElement).toBe(trigger);
  expect(logged).toHaveBeenCalledWith("Code theme selection failed", error);
  expect(mocks.toast).toHaveBeenCalledWith("无法应用深色代码主题", "error");
});
