/* @vitest-environment jsdom */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import css from "./PluginsTab.css?raw";
import { PluginsTab } from "./PluginsTab";
import { useAppStore } from "../stores";

vi.mock("../protocol/api", () => ({ apiBase: () => "http://test.local", authHeaders: () => ({}),
  fetchWithTimeout: (input: RequestInfo | URL, init?: RequestInit) => fetch(input, init),
  errorMessageFromResponseText: (text: string) => text, LONG_HTTP_TIMEOUT_MS: 60000,
  pluginAssetResourceUrlWithToken: (_path: string, variant: string) => `https://test.local/bundled-${variant}.png`,
  fetchDraftModelSelection: async () => ({ current_model: "", available_models: [], models_source: "configured",
    available_permission_modes: ["confirm"], config_version: "fixture", config_cwd: "", feature_flags: {}, composer_commands: [] }) }));
vi.mock("../protocol/ws-outbox", () => ({ sendClientCommand: vi.fn() }));
vi.mock("./ToastContainer", () => ({ pushToast: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ desktop: () => undefined, isDesktop: () => false, pickDirectory: vi.fn() }));
vi.mock("../chat/messages/MarkdownRenderer", () => ({ MarkdownRenderer: ({ content }: { content: string }) => <p>{content}</p> }));

let style: HTMLStyleElement;
beforeEach(() => {
  style = document.createElement("style"); style.textContent = css; document.head.appendChild(style);
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  useAppStore.setState({ workingDirectory: "", mcpServers: [], resolvedTheme: "light" });
});
afterEach(() => { cleanup(); style.remove(); vi.unstubAllGlobals(); });

it("shows actual names and logos in both the installed and public directory", async () => {
  const publicCatalog = JSON.parse(readFileSync(resolve(process.cwd(), "../backend/plugins/public-catalog.json"), "utf8"));
  const figma = publicCatalog.plugins.find((entry: { name: string }) => entry.name === "figma");
  figma.installation = { available: false, reason: figma.runtime_blocker };
  const bundled = { name: "minicode-bundled", directory: "public", isBuiltIn: true, status: "ready", source: {}, plugins: [
    { id: "computer-use@minicode-bundled", name: "computer-use", path: "C:/bundled/computer-use", iconVariant: "logo",
      interface: { displayName: "Computer Use", shortDescription: "Work with Windows apps", category: "Developer Tools" } },
  ] };
  const installed = { id: "computer-use@minicode-bundled", name: "computer-use", displayName: "Computer Use",
    path: "C:/installed/computer-use", iconVariant: "logo", iconVariants: ["logo"], enabled: true };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input)).pathname;
    return Response.json(path.endsWith("/marketplaces") ? { marketplaces: [publicCatalog, bundled] } : { plugins: [installed] });
  }));
  render(<PluginsTab catalog />);
  const installedButton = await screen.findByRole("button", { name: "管理插件 computer-use@minicode-bundled" });
  expect(installedButton.textContent).toContain("Computer Use");
  expect(getComputedStyle(within(installedButton).getByText("Computer Use")).display).not.toBe("none");
  await waitFor(() => expect(installedButton.querySelector("img")?.getAttribute("src")).toBe("https://test.local/bundled-logo.png"));
  const figmaButton = await screen.findByRole("button", { name: "查看插件详情 figma@openai-curated" });
  const title = within(figmaButton).getByText("Figma");
  expect(getComputedStyle(title).display).not.toBe("none");
  expect(getComputedStyle(title).color).not.toBe("rgba(0, 0, 0, 0)");
  expect(within(figmaButton).getByText("Figma design-to-code workflows")).toBeTruthy();
  expect(new URL(figmaButton.querySelector("img")!.src).pathname).toBe(`/${figma.iconAsset}`);
  expect(screen.getAllByRole("button", { name: /^查看插件详情 .*@openai-curated$/ })).toHaveLength(65);
  fireEvent.click(figmaButton);
  const detail = screen.getByRole("region", { name: "插件详情" });
  expect(within(detail).getByRole("heading", { name: "Figma" })).toBeTruthy();
  expect(within(detail).getByText("MCP 服务器 · 1")).toBeTruthy();
  expect(within(detail).getByText("技能 · 12")).toBeTruthy();
  expect(within(detail).getByRole("button", { name: "安装插件", exact: true })).toHaveProperty("disabled", true);
  expect(within(detail).getByText(/oauth_resource/)).toBeTruthy();
});
