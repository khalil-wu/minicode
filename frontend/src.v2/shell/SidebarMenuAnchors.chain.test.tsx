// @vitest-environment jsdom
import { loadConversationMenu } from "./ConversationMenu";

// These action suites use a ready interaction module; cold ownership is covered separately.
beforeAll(async () => { await loadConversationMenu(); });
import { useRef, useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConversationMenu } from "./sidebarComponents";
import { SidebarSortMenu } from "./SidebarSortMenu";
import { useAppStore } from "../stores";

vi.mock("../desktop/runtime", () => ({ isDesktop: () => false, desktop: () => undefined }));

function ConversationMenuHarness() {
  const anchor = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  return <><button ref={anchor} onClick={() => setOpen(value => !value)}>原会话菜单</button>
    {open && <ConversationMenu anchor={anchor.current} menuId="actual-conversation-menu" conversationId="background-owner"
      workspaceRoot="C:/actual-owner" archived={false} isIsolated={false} canCopy={true} canHandoff={false}
      onPin={vi.fn()} onReadState={vi.fn()} onProjectChange={vi.fn()} onRename={vi.fn()} onCopy={vi.fn()}
      onCopyLink={vi.fn()} onCopyMarkdown={vi.fn()} onHandoff={vi.fn()} onArchive={vi.fn()} onClose={() => setOpen(false)} />}</>;
}

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(236);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(180);
  useAppStore.setState({ conversationId: "reader", workingDirectory: "C:/reader", draft: "unchanged reader draft" });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it.each(["conversation", "sort"])("anchors the %s menu to its real trigger as the sidebar scrolls", async kind => {
  render(kind === "conversation" ? <ConversationMenuHarness /> : <SidebarSortMenu section="chats" mode="manual" onSelect={vi.fn()} />);
  const trigger = screen.getByRole("button", { name: kind === "conversation" ? "原会话菜单" : "聊天侧栏选项" });
  let top = 160;
  vi.spyOn(trigger, "getBoundingClientRect").mockImplementation(() => ({ top, bottom: top + 24, left: 80, right: 316,
    width: 236, height: 24, x: 80, y: top, toJSON: () => ({}) }));
  fireEvent.click(trigger);
  const menu = screen.getByRole("menu", { name: kind === "conversation" ? "会话操作" : "聊天侧栏选项" });
  expect(menu.style.top).toBe("190px");
  const focused = document.activeElement;
  top = 60; fireEvent.scroll(window);
  expect(menu.style.top).toBe("90px");
  expect(menu.style.left).toBe("80px");
  expect(document.activeElement).toBe(focused);
  fireEvent.keyDown(menu, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  expect(document.activeElement).toBe(trigger);
  expect(useAppStore.getState()).toMatchObject({ conversationId: "reader", workingDirectory: "C:/reader", draft: "unchanged reader draft" });
});

it.each(["conversation", "sort"])("a second real click closes the %s menu instead of closing on mousedown and reopening on click", async kind => {
  render(kind === "conversation" ? <ConversationMenuHarness /> : <SidebarSortMenu section="projects" mode="manual" onSelect={vi.fn()} />);
  const trigger = screen.getByRole("button", { name: kind === "conversation" ? "原会话菜单" : "项目侧栏选项" });
  fireEvent.click(trigger);
  expect(screen.getByRole("menu")).toBeTruthy();
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
  fireEvent.mouseDown(trigger);
  // The trigger belongs to the open menu; its click is the one toggle owner.
  expect(screen.getByRole("menu")).toBeTruthy();
  fireEvent.click(trigger);
  expect(screen.queryByRole("menu")).toBeNull();
});
