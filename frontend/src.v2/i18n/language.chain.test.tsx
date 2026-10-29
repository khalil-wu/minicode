/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DesktopPreferences } from "../desktop/runtime";

const bridge = vi.hoisted(() => ({
  preferences: { localeOverride: "zh-CN", systemLocale: "en-US" } as unknown as DesktopPreferences,
  changed: null as ((preferences: DesktopPreferences) => void) | null,
}));
vi.mock("../desktop/runtime", () => {
  const api = { preferences: {
    get: () => Promise.resolve(bridge.preferences),
    onChanged: (callback: (preferences: DesktopPreferences) => void) => {
      bridge.changed = callback;
      return () => { bridge.changed = null; };
    },
    set: vi.fn(),
  } };
  return { isDesktop: () => true, desktop: () => api };
});
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

import { DesktopLanguageProvider, LanguageProvider, resolveLocale, translate, useI18n } from "./index";
import { showPrompt } from "../overlays/DialogService";
import en from "./en.json";
import zh from "./zh-CN.json";
import desktopEn from "./desktop.en.json";
import desktopZh from "./desktop.zh-CN.json";
import processEn from "./process.en.json";
import processZh from "./process.zh-CN.json";
import rootEn from "./root.en.json";
import rootZh from "./root.zh-CN.json";
import appearanceEn from "./appearance.en.json";
import appearanceZh from "./appearance.zh-CN.json";

function ComposerFixture() {
  const { t } = useI18n();
  return <>
    <button>{t("设置")}</button>
    <textarea aria-label={t("消息输入")} defaultValue="" />
    <p data-testid="user">设置 — 用户原文</p>
    <p data-testid="model">模型正文包含：自动检测</p>
    <pre data-testid="log">C:\项目\设置.ts — 原始工具诊断</pre>
    <span data-testid="attachment">设计稿.png</span>
  </>;
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("UI language boundary", () => {
  it("uses the explicit desktop setting, then updates live without replacing the draft or content", async () => {
    bridge.preferences = { localeOverride: "zh-CN", systemLocale: "en-US" } as DesktopPreferences;
    render(<DesktopLanguageProvider><ComposerFixture /></DesktopLanguageProvider>);
    const draft = await screen.findByRole("textbox", { name: "消息输入" });
    fireEvent.change(draft, { target: { value: "未保存的草稿 $skill @文件" } });
    const attachment = screen.getByTestId("attachment");
    act(() => bridge.changed!({ ...bridge.preferences, localeOverride: "en-US" }));
    expect(screen.getByRole("button", { name: "Settings" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Message input" })).toBe(draft);
    expect((draft as HTMLTextAreaElement).value).toBe("未保存的草稿 $skill @文件");
    expect(screen.getByTestId("attachment")).toBe(attachment);
    expect(screen.getByTestId("user").textContent).toBe("设置 — 用户原文");
    expect(screen.getByTestId("model").textContent).toBe("模型正文包含：自动检测");
    expect(screen.getByTestId("log").textContent).toBe(String.raw`C:\项目\设置.ts — 原始工具诊断`);
    expect(document.documentElement.lang).toBe("en-US");
    act(() => bridge.changed!({ ...bridge.preferences, localeOverride: null, systemLocale: "zh-CN" }));
    expect(screen.getByRole("button", { name: "设置" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "消息输入" })).toBe(draft);
    expect(document.documentElement.lang).toBe("zh-CN");
  });

  it("keeps the same mounted controls when the locale provider changes", () => {
    const view = render(<LanguageProvider locale="zh-CN"><ComposerFixture /></LanguageProvider>);
    const draft = screen.getByRole("textbox", { name: "消息输入" });
    view.rerender(<LanguageProvider locale="en-US"><ComposerFixture /></LanguageProvider>);
    expect(screen.getByRole("textbox", { name: "Message input" })).toBe(draft);
  });

  it("updates an imperative dialog's own React root without changing its raw text or draft", async () => {
    const view = render(<LanguageProvider locale="en-US"><ComposerFixture /></LanguageProvider>);
    let answer!: Promise<string | null>;
    await act(async () => { answer = showPrompt({ message: "用户原文：设置", defaultValue: "原名称" }); });
    const dialog = screen.getByRole("dialog", { name: "Input required" });
    const input = dialog.querySelector("input")!;
    fireEvent.change(input, { target: { value: "改过且未提交的名称" } });
    expect(screen.getByRole("button", { name: "OK" })).toBeTruthy();
    view.rerender(<LanguageProvider locale="zh-CN"><ComposerFixture /></LanguageProvider>);
    expect(screen.getByRole("dialog", { name: "需要输入" })).toBe(dialog);
    expect(dialog.querySelector("input")).toBe(input);
    expect(input.value).toBe("改过且未提交的名称");
    expect(screen.getByText("用户原文：设置")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确定" }));
    expect(await answer).toBe("改过且未提交的名称");
  });

  it("detects only supported locales and keeps dynamic labels in their original language", () => {
    expect(resolveLocale(null, "zh-CN")).toBe("zh-CN");
    expect(resolveLocale(null, "zh_Hans_CN")).toBe("zh-CN");
    expect(resolveLocale(null, "zh-SG")).toBe("zh-CN");
    expect(resolveLocale(null, "zh-TW")).toBe("en-US");
    expect(resolveLocale(null, "fr-FR")).toBe("en-US");
    expect(resolveLocale("en-US", "zh-CN")).toBe("en-US");
    expect(resolveLocale("zh-CN", "en-US")).toBe("zh-CN");
    expect(translate("en-US", "打开 {name}", { name: "设置与日志.txt" })).toBe("Open 设置与日志.txt");
    expect(translate("en-US", "高")).toBe("High");
    expect(translate("en-US", "视口高度")).toBe("Viewport height");
    expect(translate("en-US", "回复详细程度")).toBe("Output detail");
  });

  it("has matching explicit keys in both language catalogs", () => {
    expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort());
    expect(Object.keys(desktopZh).sort()).toEqual(Object.keys(desktopEn).sort());
    expect(Object.keys(processZh).sort()).toEqual(Object.keys(processEn).sort());
    expect(Object.keys(rootZh).sort()).toEqual(Object.keys(rootEn).sort());
    expect(Object.keys(appearanceZh).sort()).toEqual(Object.keys(appearanceEn).sort());
  });
});
