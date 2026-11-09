/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { BrowserSiteCategory, BrowserSiteDecision, BrowserSitePolicy } from "../desktop/runtime";
import { BrowserPersonalDataSettings } from "./BrowserPersonalDataSettings";

const { state, api } = vi.hoisted(() => {
  const state = { policy: null as BrowserSitePolicy | null };
  const api = {
    getSitePolicy: vi.fn(async () => structuredClone(state.policy!)), onSettingsChanged: vi.fn(() => () => {}),
    setSitePolicy: vi.fn(async ({ origin, category, setting }: { origin?: string; category: BrowserSiteCategory; setting: BrowserSiteDecision | "default" }) => {
      const policy = state.policy!;
      if (origin) {
        let site = policy.sites.find(item => item.origin === origin);
        if (!site) { site = { origin, settings: {} }; policy.sites.push(site); }
        if (setting === "default") delete site.settings[category]; else site.settings[category] = setting;
      } else policy.defaults[category] = setting as BrowserSiteDecision;
      return structuredClone(policy);
    }),
    removeSitePermissions: vi.fn(async ({ origin }: { origin: string }) => { state.policy!.sites = state.policy!.sites.filter(item => item.origin !== origin); }),
    removeSiteDevice: vi.fn(async ({ id }: { id: string }) => { state.policy!.devices = state.policy!.devices.filter(item => item.id !== id); return structuredClone(state.policy!); }),
  };
  return { state, api };
});
vi.mock("../desktop/runtime", () => ({ desktop: () => ({ embeddedBrowser: api }) }));
beforeEach(() => {
  vi.clearAllMocks();
  state.policy = { categories: [
    { id: "camera", allowedSettings: ["ask", "allow", "block"], defaultSettings: ["ask", "block"] },
    { id: "microphone", allowedSettings: ["ask", "allow", "block"], defaultSettings: ["ask", "block"] },
    { id: "notifications", allowedSettings: ["ask", "allow", "block"], defaultSettings: ["ask", "block"] },
    { id: "usb", allowedSettings: ["ask", "block"], defaultSettings: ["ask", "block"] },
  ], defaults: { camera: "ask", microphone: "ask", geolocation: "ask", notifications: "ask", "clipboard-read": "ask", mediaKeySystem: "allow", sound: "allow", images: "allow", javascript: "allow", usb: "ask" },
    sites: [{ origin: "https://example.test", settings: { camera: "allow", microphone: "block", notifications: "allow" } }], devices: [] };
});
afterEach(cleanup);
const show = () => render(<BrowserPersonalDataSettings section="sites" active breadcrumb={current => <nav>{current}</nav>} />);

it("opens category defaults and real per-site details while keeping camera and microphone independent", async () => {
  show();
  fireEvent.click(await screen.findByRole("button", { name: "摄像头 询问", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "摄像头，当前：询问", exact: true }));
  const defaults = within(await screen.findByRole("listbox", { name: "摄像头", exact: true }));
  expect(defaults.queryByRole("option", { name: "允许", exact: true })).toBeNull();
  fireEvent.click(defaults.getByRole("option", { name: "阻止", exact: true }));
  await waitFor(() => expect(api.setSitePolicy).toHaveBeenCalledWith({ category: "camera", setting: "block" }));
  await waitFor(() => expect(screen.queryByRole("listbox", { name: "摄像头", exact: true })).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "https://example.test 允许", exact: true }));
  expect(screen.getByRole("button", { name: "摄像头，当前：允许", exact: true })).toBeTruthy();
  expect(screen.getByRole("button", { name: "麦克风，当前：阻止", exact: true })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "摄像头，当前：允许", exact: true }));
  const exceptions = within(await screen.findByRole("listbox", { name: "摄像头", exact: true }));
  fireEvent.click(exceptions.getByRole("option", { name: "阻止", exact: true }));
  await waitFor(() => expect(api.setSitePolicy).toHaveBeenCalledWith({ origin: "https://example.test", category: "camera", setting: "block" }));
  await screen.findByRole("button", { name: "摄像头，当前：阻止", exact: true });
  expect(api.setSitePolicy.mock.calls.some(([request]) => request.category === "microphone")).toBe(false);
});

it("reset returns all site fields to the real default while preserving the open detail", async () => {
  show(); fireEvent.click(await screen.findByRole("button", { name: "https://example.test", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "重置权限", exact: true }));
  await waitFor(() => expect(api.removeSitePermissions).toHaveBeenCalledWith({ origin: "https://example.test" }));
  fireEvent.click(await screen.findByRole("button", { name: "通知，当前：默认设置：询问", exact: true }));
  const notification = within(await screen.findByRole("listbox", { name: "通知", exact: true }));
  const selectedDefault = notification.getByRole("option", { name: "默认设置：询问", exact: true });
  expect(selectedDefault.getAttribute("aria-selected")).toBe("true");
  fireEvent.keyDown(selectedDefault, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("listbox", { name: "通知", exact: true })).toBeNull());
  expect(api.setSitePolicy).not.toHaveBeenCalled();
  expect(screen.getByRole("heading", { name: "https://example.test", exact: true })).toBeTruthy();
});

it("device management exposes only paired metadata and revokes the selected native grant", async () => {
  state.policy!.devices = [{ id: "actual-device", origin: "https://example.test", name: "Fixture device" }];
  show(); fireEvent.click(await screen.findByRole("button", { name: "USB 设备 询问", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "移除设备授权 Fixture device", exact: true }));
  await waitFor(() => expect(api.removeSiteDevice).toHaveBeenCalledWith({ id: "actual-device" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "移除设备授权 Fixture device", exact: true })).toBeNull());
  expect(screen.queryByText("侵入式广告")).toBeNull();
});
