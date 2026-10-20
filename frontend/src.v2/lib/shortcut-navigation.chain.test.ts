/* @vitest-environment jsdom */
import { beforeEach, expect, it, vi } from "vitest";
import { matchesExternalBrowserClick, numberedShortcut } from "./shortcut-navigation";
import { openPreferredWebLink } from "./open-preferred-target";
import { useAppStore } from "../stores";

const native = vi.hoisted(() => ({ preferences: vi.fn(), external: vi.fn(), inApp: vi.fn() }));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(), isDesktop: () => true,
  getDesktopPreferences: native.preferences, openExternal: native.external }));
vi.mock("../chat/openWebInBrowser", () => ({ openWebInBrowser: native.inApp }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));
const modifiers = { ctrlKey: false, metaKey: false, altKey: false, shiftKey: false };

beforeEach(() => {
  vi.clearAllMocks();
  native.preferences.mockResolvedValue({ externalBrowserLinkShortcut: "primary", urlOpenTarget: "in-app-browser", localUrlOpenTarget: "in-app-browser" });
  native.inApp.mockReturnValue(true);
  useAppStore.setState({ conversationId: "original", pendingConversationSwitchId: null, draft: "Keep authored draft" });
});

it("routes primary and secondary number keys to opposite real destinations on Windows and macOS", () => {
  expect(numberedShortcut({ ...modifiers, key: "2", ctrlKey: true }, "tabs", false)).toEqual({ target: "tabs", index: 1 });
  expect(numberedShortcut({ ...modifiers, key: "2", altKey: true }, "tabs", false)).toEqual({ target: "sidebar", index: 1 });
  expect(numberedShortcut({ ...modifiers, key: "2", altKey: true }, "sidebar", false)).toEqual({ target: "tabs", index: 1 });
  expect(numberedShortcut({ ...modifiers, key: "3", metaKey: true }, "tabs", true)).toEqual({ target: "tabs", index: 2 });
  expect(numberedShortcut({ ...modifiers, key: "3", ctrlKey: true }, "tabs", true)).toEqual({ target: "sidebar", index: 2 });
  expect(numberedShortcut({ ...modifiers, key: "1", ctrlKey: true, altKey: true }, "tabs", false)).toBeNull();
  expect(numberedShortcut({ ...modifiers, key: "1" }, "tabs", false)).toBeNull();
});

it("matches only the configured modifier gesture, including macOS Command and Option", () => {
  expect(matchesExternalBrowserClick({ ...modifiers, ctrlKey: true }, "primary", false)).toBe(true);
  expect(matchesExternalBrowserClick({ ...modifiers, ctrlKey: true, shiftKey: true }, "primary", false)).toBe(false);
  expect(matchesExternalBrowserClick({ ...modifiers, ctrlKey: true, shiftKey: true }, "primaryShift", false)).toBe(true);
  expect(matchesExternalBrowserClick({ ...modifiers, metaKey: true }, "primary", true)).toBe(true);
  expect(matchesExternalBrowserClick({ ...modifiers, ctrlKey: true }, "primary", true)).toBe(false);
  expect(matchesExternalBrowserClick({ ...modifiers, altKey: true }, "alt", true)).toBe(true);
  expect(matchesExternalBrowserClick({ ...modifiers, ctrlKey: true }, "unassigned", false)).toBe(false);
});

it("uses the saved modifier to override the normal web destination while keeping ordinary clicks in the app", async () => {
  await openPreferredWebLink("https://example.com/ordinary", "original", modifiers);
  expect(native.inApp).toHaveBeenCalledExactlyOnceWith("https://example.com/ordinary");
  native.preferences.mockResolvedValueOnce({ externalBrowserLinkShortcut: "alt", urlOpenTarget: "in-app-browser", localUrlOpenTarget: "in-app-browser" });
  await openPreferredWebLink("https://example.com/system", "original", { ...modifiers, altKey: true });
  expect(native.external).toHaveBeenCalledExactlyOnceWith("https://example.com/system");
  expect(useAppStore.getState().draft).toBe("Keep authored draft");
});

it("does not open an old message link in a new chat when the preference response arrives late", async () => {
  let finish!: (value: unknown) => void;
  native.preferences.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const opened = openPreferredWebLink("https://example.com/original", "original", modifiers);
  useAppStore.setState({ conversationId: "next", draft: "New chat draft" });
  finish({ externalBrowserLinkShortcut: "primary", urlOpenTarget: "in-app-browser" });
  expect(await opened).toBe(false);
  expect(native.external).not.toHaveBeenCalled();
  expect(native.inApp).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toBe("New chat draft");
});
