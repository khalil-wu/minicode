/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConversationMenu, ConfirmDialog } from "./sidebarComponents";
import { WorkspaceContextMenu } from "../workspace/WorkspaceContextMenu";
import { activateWorkspaceFolder } from "../workspace/openWorkspaceFolder";
import { useAppStore } from "../stores";

const { send, toast } = vi.hoisted(() => ({ send: vi.fn(), toast: vi.fn() }));
vi.mock("../protocol/ws-outbox", async (original) => ({ ...await original(), sendClientCommandAwaitResult: send }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: toast }));
let anchor: HTMLButtonElement;
const menuProps = () => ({ anchor, menuId: "owned-menu", archived: false, isIsolated: true,
  canReveal: false, canCopy: true, canMerge: true, onRename: vi.fn(), onReveal: vi.fn(), onCopy: vi.fn(),
  onCleanup: vi.fn(), onHandoff: vi.fn(), onArchive: vi.fn(), onClone: vi.fn(), onMerge: vi.fn(), onExport: vi.fn(), onClose: vi.fn() });

beforeEach(() => {
  vi.clearAllMocks();
  anchor = document.createElement("button");
  document.body.append(anchor);
  anchor.getBoundingClientRect = () => ({ top: 100, bottom: 125, left: 200, right: 224, width: 24, height: 25, x: 200, y: 100, toJSON: () => ({}) });
  useAppStore.setState({ conversationId: "owner-a", workingDirectory: "C:/a", appMode: "code", permissionMode: "confirm" });
});
afterEach(() => { cleanup(); anchor.remove(); });

it("keeps the selected conversation action while callbacks change", () => {
  const props = menuProps();
  const view = render(<ConversationMenu {...props} />);
  fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
  const selected = screen.getByRole("menuitem", { name: "克隆会话" });
  expect(document.activeElement).toBe(selected);
  view.rerender(<ConversationMenu {...props} onClose={vi.fn()} />);
  expect(document.activeElement).toBe(selected);
});

it("keeps the selected conversation action while anchor placement changes", () => {
  render(<ConversationMenu {...menuProps()} />);
  fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
  const selected = screen.getByRole("menuitem", { name: "克隆会话" });
  anchor.getBoundingClientRect = () => ({ top: 140, bottom: 165, left: 200, right: 224, width: 24, height: 25, x: 200, y: 140, toJSON: () => ({}) });
  fireEvent.resize(window);
  expect(document.activeElement).toBe(selected);
});

it("keeps dialog focus inside and restores its trigger after dismissal", () => {
  anchor.focus();
  const dialog = { title: "清理工作区", message: "确认操作", confirmLabel: "清理", onConfirm: vi.fn() };
  const view = render(<ConfirmDialog dialog={dialog} onCancel={vi.fn()} onConfirm={vi.fn()} />);
  expect(view.container.contains(screen.getByRole("dialog"))).toBe(false);
  const confirm = screen.getByRole("button", { name: "清理" });
  confirm.focus();
  fireEvent.keyDown(confirm, { key: "Tab" });
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "取消" }));
  view.unmount();
  expect(document.activeElement).toBe(anchor);
});

it("does not reset a chosen dialog action on callback updates", () => {
  const dialog = { title: "清理工作区", message: "确认操作", confirmLabel: "清理", onConfirm: vi.fn() };
  const view = render(<ConfirmDialog dialog={dialog} onCancel={vi.fn()} onConfirm={vi.fn()} />);
  const confirm = screen.getByRole("button", { name: "清理" });
  confirm.focus();
  view.rerender(<ConfirmDialog dialog={dialog} onCancel={vi.fn()} onConfirm={vi.fn()} />);
  expect(document.activeElement).toBe(confirm);
});

it("does not switch a later projectless selection to code from an old activation receipt", async () => {
  send.mockImplementationOnce(async () => {
    useAppStore.setState({ conversationId: "newer", workingDirectory: "", appMode: "cowork" });
    return { level: "success", data: { conversation_id: "owner-a", workspace_root: "C:/a" } };
  });
  expect(await activateWorkspaceFolder("C:/a")).toBe("C:/a");
  expect(useAppStore.getState()).toMatchObject({ conversationId: "newer", appMode: "cowork" });
});

it("does not close a newer code workspace from an old removal receipt", async () => {
  let finish!: (value: unknown) => void;
  send.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  render(<WorkspaceContextMenu path="C:/a" position={{ x: 200, y: 100 }} onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("menuitem", { name: "移除工作区" }));
  useAppStore.setState({ conversationId: "owner-b", workingDirectory: "C:/b", appMode: "code" });
  await act(async () => finish({ level: "success", data: { closed_active: true, conversation_id: "projectless-result", workspace_root: "", path: "C:/a" } }));
  expect(useAppStore.getState()).toMatchObject({ conversationId: "owner-b", workingDirectory: "C:/b", appMode: "code" });
});

it("dismisses the conversation menu with Tab and returns focus to the trigger", () => {
  const props = menuProps();
  render(<ConversationMenu {...props} />);
  fireEvent.keyDown(screen.getByRole("menu"), { key: "Tab" });
  expect(props.onClose).toHaveBeenCalledOnce();
  expect(document.activeElement).toBe(anchor);
});

it("applies the workspace receipt mode when its conversation and root are still current", async () => {
  useAppStore.setState({ appMode: "cowork" });
  send.mockResolvedValue({ level: "success", data: { conversation_id: "owner-a", workspace_root: "C:/a" } });
  await activateWorkspaceFolder("C:/a");
  expect(useAppStore.getState().appMode).toBe("code");
});

it("applies the removal mode only to its resulting projectless conversation", async () => {
  useAppStore.setState({ conversationId: "projectless-result", workingDirectory: "", appMode: "code" });
  send.mockResolvedValue({ level: "success", data: { closed_active: true, conversation_id: "projectless-result", workspace_root: "", path: "C:/a" } });
  render(<WorkspaceContextMenu path="C:/a" position={{ x: 200, y: 100 }} onClose={vi.fn()} />);
  await act(async () => fireEvent.click(screen.getByRole("menuitem", { name: "移除工作区" })));
  expect(useAppStore.getState().appMode).toBe("cowork");
});
