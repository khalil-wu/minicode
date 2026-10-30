/* @vitest-environment jsdom */
import { loadConversationMenu } from "./ConversationMenu";

// These action suites use a ready interaction module; cold ownership is covered separately.
beforeAll(async () => { await loadConversationMenu(); });
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeAll, afterEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import { ConversationMenu } from "./sidebarComponents";

const native = vi.hoisted(() => ({
  changed: null as ((state: { mutedConversationIds: string[] }) => void) | null,
  unmute: vi.fn(async () => ({ mutedConversationIds: [] })), unsubscribe: vi.fn(),
}));
vi.mock("../desktop/runtime", async load => ({ ...await load<Record<string, unknown>>(), desktop: () => ({ pets: {
  getState: async () => ({ mutedConversationIds: [] }), setNotificationMuted: native.unmute,
  onStateChanged: (callback: typeof native.changed) => { native.changed = callback; return native.unsubscribe; },
} }) }));
vi.mock("./threadWindowActions", () => ({ canOpenThreadWindow: () => false, openThreadWindow: vi.fn() }));
vi.mock("./useWorkspaceOpenMenu", () => ({ useWorkspaceOpenMenu: () => [] }));

afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("offers Unmute only for the actual muted chat and sends its owner id without touching the draft", async () => {
  useAppStore.setState({ conversationId: "other-active-chat", draft: "Keep this unsent draft" });
  const anchor = document.createElement("button");
  document.body.appendChild(anchor);
  const view = render(<ConversationMenu anchor={anchor} menuId="fixture-menu" conversationId="muted-chat"
    archived={false} isIsolated={false} canCopy={false} canHandoff={false}
    onPin={vi.fn()} onReadState={vi.fn()} onProjectChange={vi.fn()} onRename={vi.fn()}
    onCopy={vi.fn()} onCopyLink={vi.fn()} onCopyMarkdown={vi.fn()} onHandoff={vi.fn()} onArchive={vi.fn()} onClose={vi.fn()} />);
  await waitFor(() => expect(native.changed).toBeTypeOf("function"));
  expect(screen.queryByRole("menuitem", { name: "取消静音聊天" })).toBeNull();
  act(() => native.changed!({ mutedConversationIds: ["muted-chat"] }));
  const unmute = screen.getByRole("menuitem", { name: "取消静音聊天" });
  expect(unmute.querySelector("svg")).toBeNull();
  fireEvent.click(unmute);
  await waitFor(() => expect(native.unmute).toHaveBeenCalledWith({ conversationId: "muted-chat", muted: false }));
  expect(useAppStore.getState()).toMatchObject({ conversationId: "other-active-chat", draft: "Keep this unsent draft" });
  view.unmount(); anchor.remove();
  expect(native.unsubscribe).toHaveBeenCalledOnce();
});
