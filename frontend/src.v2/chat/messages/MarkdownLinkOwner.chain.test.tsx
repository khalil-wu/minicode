/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { useAppStore } from "../../stores";
import { pushToast } from "../../overlays/ToastContainer";

const native = vi.hoisted(() => ({ get: vi.fn(), external: vi.fn(), inApp: vi.fn() }));
vi.mock("../../desktop/runtime", async load => ({ ...await load<typeof import("../../desktop/runtime")>(), isDesktop: () => true,
  getDesktopPreferences: native.get, openExternal: native.external }));
vi.mock("../openWebInBrowser", async load => ({ ...await load<typeof import("../openWebInBrowser")>(), openWebInBrowser: native.inApp }));
vi.mock("../../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("passes the rendered message's owner into the asynchronous desktop web opener", async () => {
  let finish!: (value: unknown) => void;
  native.get.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  useAppStore.setState({ conversationId: "message-owner", workingDirectory: "C:/Original", pendingConversationSwitchId: null, draft: "Original draft" });
  render(<MarkdownRenderer content="[Original web page](https://example.com/original)" conversationId="message-owner" workspaceRoot="C:/Original" />);
  fireEvent.click(screen.getByRole("link", { name: /Original web page/ }));
  await waitFor(() => expect(native.get).toHaveBeenCalledTimes(1));
  useAppStore.setState({ conversationId: "new-owner", workingDirectory: "C:/New", draft: "Keep new draft" });
  finish({ externalBrowserLinkShortcut: "primary", urlOpenTarget: "in-app-browser", localUrlOpenTarget: "in-app-browser" });
  await waitFor(() => expect(pushToast).toHaveBeenCalledWith("会话已切换，请回到原请求后打开链接。", "error"));
  expect(native.inApp).not.toHaveBeenCalled();
  expect(native.external).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "new-owner", workingDirectory: "C:/New", draft: "Keep new draft" });
});
