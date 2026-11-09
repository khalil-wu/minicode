// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPane } from "./ChatPane";
import { useAppStore } from "../stores";

vi.mock("./MessageList", () => ({ MessageList: () => <p>owned transcript</p> }));
vi.mock("../composer/Composer", () => ({ Composer: () => <textarea data-composer-input /> }));
vi.mock("./ChatContextCard", () => ({ ChatContextCard: () => null }));
vi.mock("./TurnChangeSummary", () => ({ TurnChangeSummary: () => null }));
const initial = useAppStore.getState();
beforeEach(() => {
  useAppStore.setState({ ...initial, conversationId: "A", pendingConversationSwitchId: null,
    appMode: "chat", conversationHydration: {} }, true);
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("chat search owner", () => {
  it("closes the previous search on a canonical owner change without an optimistic switch", () => {
    render(<ChatPane />);
    act(() => window.dispatchEvent(new Event("chat:request-search")));
    expect(screen.getByRole("search")).toBeTruthy();
    act(() => useAppStore.setState({ conversationId: "B" }));
    expect(screen.queryByRole("search")).toBeNull();
    expect(window.getSelection()?.rangeCount).toBe(0);
  });

  it("leaves the editor's find shortcut available while the editor panel owns focus", () => {
    useAppStore.setState({ appMode: "code", panelSlots: [{ id: "editor", kind: "editor", focused: true }] });
    render(<ChatPane />);
    fireEvent.keyDown(document, { key: "f", ctrlKey: true });
    expect(screen.queryByRole("search")).toBeNull();
  });
});
