/* @vitest-environment jsdom */

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const browserList = vi.fn();
const browserGetSettings = vi.fn();
const browserSetSettings = vi.fn();
const browserIsDesktop = vi.fn(() => true);

vi.mock("../desktop/runtime", () => ({
  embeddedBrowserList: (...args: unknown[]) => browserList(...args),
  embeddedBrowserGetSettings: (...args: unknown[]) => browserGetSettings(...args),
  embeddedBrowserSetSettings: (...args: unknown[]) => browserSetSettings(...args),
  isDesktop: () => browserIsDesktop(),
}));
vi.mock("../lib/settings-navigation", () => ({ openRightPanelFromSettings: vi.fn() }));
vi.mock("../components/SelectMenu", () => ({
  SelectMenu: ({ children, ...props }: React.SelectHTMLAttributes<HTMLSelectElement> & { children: React.ReactNode }) => <select {...props}>{children}</select>,
}));

import { BrowserIntegrationTab } from "./BrowserIntegrationTab";
import { useAppStore } from "../stores";

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => { resolve = nextResolve; });
  return { promise, resolve };
};

describe("BrowserIntegrationTab", () => {
  beforeEach(() => {
    browserList.mockReset();
    browserGetSettings.mockReset().mockResolvedValue({ downloadPolicy: "block" });
    browserSetSettings.mockReset();
    browserIsDesktop.mockReturnValue(true);
    useAppStore.setState({ conversationId: "conversation-a" });
  });

  afterEach(() => cleanup());

  it("does not publish a stale tab list after switching conversations", async () => {
    const first = deferred<Array<{ id: string; title: string; active: boolean }>>();
    const second = deferred<Array<{ id: string; title: string; active: boolean }>>();
    browserList.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const { rerender } = render(<BrowserIntegrationTab />);

    act(() => useAppStore.setState({ conversationId: "conversation-b" }));
    rerender(<BrowserIntegrationTab />);
    first.resolve([{ id: "old", title: "Old page", active: true }]);
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByText("Old page")).toBeNull();

    second.resolve([{ id: "new", title: "New page", active: true }]);
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText("New page")).toBeTruthy();
    expect(browserList).toHaveBeenNthCalledWith(1, "conversation-a");
    expect(browserList).toHaveBeenNthCalledWith(2, "conversation-b");
  });
});
