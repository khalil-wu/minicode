// @vitest-environment jsdom
import { useEffect, useRef, useState, type ComponentType } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ConversationMenuProps } from "./ConversationMenu";
import { useKeyboardShortcuts } from "../hooks/useKeyboardShortcuts";
import { ComposerTextarea } from "../composer/ComposerTextarea";
import { useAppStore, waitComposerDraftRecovery } from "../stores";

const wire = vi.hoisted(() => ({ request: vi.fn(), closed: vi.fn(), pin: vi.fn(), toast: vi.fn(), send: vi.fn(), escape: vi.fn(),
  submit: vi.fn(), history: vi.fn(), switch: vi.fn(), loadedKeys: vi.fn() }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: wire.toast, dismissToast: vi.fn() }));
vi.mock("../protocol/ws-outbox", async load => ({ ...await load<typeof import("../protocol/ws-outbox")>(), sendClientCommand: wire.send }));
async function surfaceModule() {
  await wire.request();
  return { ConversationMenuSurface: (props: ConversationMenuProps) => {
    const menu = useRef<HTMLDivElement>(null);
    useEffect(() => { menu.current!.focus(); }, []);
    return <div ref={menu} tabIndex={-1} role="menu" aria-label={`Owned ${props.conversationId}`} onKeyDown={event => wire.loadedKeys(event.key)}>
      <button role="menuitem" onClick={props.onPin}>Pin {props.conversationId}</button>
    </div>;
  } };
}

let ConversationMenu: ComponentType<ConversationMenuProps>;
let loadConversationMenu: typeof import("./ConversationMenu")["loadConversationMenu"];
let resolve!: () => void, reject!: (error: Error) => void;

function Harness({ owner = "background-A" }: { owner?: string }) {
  useKeyboardShortcuts();
  const anchor = useRef<HTMLButtonElement>(null);
  const row = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [context, setContext] = useState(false);
  const draft = useAppStore(state => state.draft);
  const close = () => { wire.closed(owner); setOpen(false); };
  return <>
    <ComposerTextarea value={draft} onChange={value => useAppStore.getState().setDraft(value)} onSubmit={wire.submit}
      onRecallHistory={() => { wire.history(); return "Earlier input"; }}
      onEscape={() => { wire.escape(); return true; }} ariaLabel="Kept composer" />
    <button ref={row} onClick={() => wire.switch(owner)} onContextMenu={event => {
      event.preventDefault(); setContext(true); setOpen(true);
    }}>Row {owner}</button>
    <button ref={anchor} onClick={() => { setContext(false); setOpen(value => !value); }}>Open {owner}</button>
    <input aria-label="Other focus" />
    {open && <ConversationMenu anchor={context ? row.current : anchor.current} position={context ? { x: 100, y: 100 } : undefined}
      menuId={`menu-${owner}`} conversationId={owner}
      archived={false} isIsolated={false} canCopy={true} canHandoff={false} onPin={() => wire.pin(owner)}
      onReadState={() => {}} onProjectChange={() => {}} onRename={() => {}} onCopy={() => {}}
      onCopyLink={() => {}} onCopyMarkdown={() => {}} onHandoff={() => {}} onArchive={() => {}} onClose={close} />}
  </>;
}

beforeEach(async () => {
  await waitComposerDraftRecovery();
  vi.resetModules();
  vi.doMock("./ConversationMenuSurface", surfaceModule);
  wire.request.mockReset(); wire.closed.mockClear(); wire.pin.mockClear(); wire.toast.mockClear(); wire.send.mockClear(); wire.escape.mockClear();
  wire.submit.mockClear(); wire.history.mockClear(); wire.switch.mockClear(); wire.loadedKeys.mockClear();
  wire.request.mockReturnValue(new Promise<void>((accept, fail) => { resolve = accept; reject = fail; }));
  useAppStore.setState({ conversationId: "reader", activeConversationDraftId: null, workingDirectory: "C:/reader", draft: "Keep this draft",
    richDraft: undefined, composerPlainTextMode: true, isStreaming: true, settingsOpen: false, commandPaletteOpen: false,
    quickOpenVisible: false, shortcutsHelpOpen: false, liveArtifactsOpen: false, agentEditorOpen: false,
    messages: [{ id: "running-reader", turnId: "reader-turn", role: "assistant", content: "Actual work", timestamp: 1, artifacts: [], isStreaming: true }] });
  ({ ConversationMenu, loadConversationMenu } = await import("./ConversationMenu"));
});
afterEach(() => { cleanup(); });

it("loads only after an actual open and hands focus to the loaded menu without a stale loading close", async () => {
  render(<Harness />);
  await act(async () => {});
  expect(wire.request).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Open background-A" }));
  await waitFor(() => expect(wire.request).toHaveBeenCalledOnce());
  expect(screen.queryByRole("menu")).toBeNull();
  await act(async () => resolve());
  const menu = await screen.findByRole("menu", { name: "Owned background-A" });
  expect(document.activeElement).toBe(menu);
  expect(wire.closed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("menuitem", { name: "Pin background-A" }));
  expect(wire.pin).toHaveBeenCalledExactlyOnceWith("background-A");
  expect(useAppStore.getState()).toMatchObject({ conversationId: "reader", draft: "Keep this draft", isStreaming: true });
});

it("owns Escape before the real composer and global interrupt while the menu module is pending", async () => {
  render(<Harness />);
  const composer = screen.getByRole("textbox", { name: "Kept composer" }); composer.focus();
  const trigger = screen.getByRole("button", { name: "Open background-A" });
  fireEvent.click(trigger);
  await waitFor(() => expect(wire.request).toHaveBeenCalledOnce());
  fireEvent.keyDown(composer, { key: "Escape" });
  expect(wire.closed).toHaveBeenCalledExactlyOnceWith("background-A");
  expect(wire.send).not.toHaveBeenCalled(); expect(wire.escape).not.toHaveBeenCalled();
  expect(document.activeElement).toBe(trigger);
  const other = screen.getByRole("textbox", { name: "Other focus" }); other.focus();
  await act(async () => resolve());
  expect(screen.queryByRole("menu")).toBeNull(); expect(document.activeElement).toBe(other);
  expect(useAppStore.getState()).toMatchObject({ draft: "Keep this draft", isStreaming: true });
});

it("consumes pending menu activation and navigation without submitting, recalling input or switching its row", async () => {
  render(<Harness />);
  const composer = screen.getByRole("textbox", { name: "Kept composer" }) as HTMLTextAreaElement; composer.focus();
  const row = screen.getByRole("button", { name: "Row background-A" });
  fireEvent.contextMenu(row, { clientX: 100, clientY: 100 });
  await waitFor(() => expect(wire.request).toHaveBeenCalledOnce());
  for (const key of ["Enter", " ", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "a"]) {
    expect(fireEvent.keyDown(composer, { key })).toBe(false);
  }
  expect(fireEvent.keyDown(composer, { key: "Enter", ctrlKey: true })).toBe(false);
  // jsdom does not generate a button's native Enter click: execute that default
  // only when the real DOM keyboard event has not already been consumed.
  if (fireEvent.keyDown(row, { key: "Enter" })) fireEvent.click(row);
  expect(wire.submit).not.toHaveBeenCalled(); expect(wire.history).not.toHaveBeenCalled();
  expect(wire.switch).not.toHaveBeenCalled(); expect(wire.send).not.toHaveBeenCalled();
  expect(useAppStore.getState()).toMatchObject({ conversationId: "reader", draft: "Keep this draft" });
  expect(fireEvent.keyDown(composer, { key: "Enter", isComposing: true })).toBe(true);
  expect(wire.submit).not.toHaveBeenCalled();
  await act(async () => resolve());
  const pin = await screen.findByRole("menuitem", { name: "Pin background-A" });
  if (fireEvent.keyDown(pin, { key: "Enter" })) fireEvent.click(pin);
  expect(wire.loadedKeys).toHaveBeenCalledWith("Enter");
  expect(wire.pin).toHaveBeenCalledExactlyOnceWith("background-A");
});

it("lets Tab leave a pending menu naturally and keeps the new focus after the module resolves", async () => {
  render(<Harness />); const trigger = screen.getByRole("button", { name: "Open background-A" }); trigger.focus(); fireEvent.click(trigger);
  await waitFor(() => expect(wire.request).toHaveBeenCalledOnce());
  expect(fireEvent.keyDown(trigger, { key: "Tab" })).toBe(true);
  const other = screen.getByRole("textbox", { name: "Other focus" }); act(() => other.focus());
  await act(async () => resolve());
  expect(wire.closed).toHaveBeenCalledOnce();
  expect(screen.queryByRole("menu")).toBeNull(); expect(document.activeElement).toBe(other);
});

it.each(["focus", "mousedown"])("retires the pending request on outside %s and never reopens or steals the new focus", async kind => {
  render(<Harness />); fireEvent.click(screen.getByRole("button", { name: "Open background-A" }));
  await waitFor(() => expect(wire.request).toHaveBeenCalledOnce());
  const other = screen.getByRole("textbox", { name: "Other focus" });
  if (kind === "mousedown") fireEvent.mouseDown(other);
  act(() => other.focus());
  await act(async () => resolve());
  expect(wire.closed).toHaveBeenCalledOnce();
  expect(screen.queryByRole("menu")).toBeNull(); expect(document.activeElement).toBe(other);
  expect(useAppStore.getState().draft).toBe("Keep this draft");
});

it("lets a second trigger click close the pending menu and applies a shared import only to the current owner", async () => {
  const view = render(<Harness />); const trigger = screen.getByRole("button", { name: "Open background-A" });
  fireEvent.click(trigger); await waitFor(() => expect(wire.request).toHaveBeenCalledOnce());
  fireEvent.mouseDown(trigger); fireEvent.click(trigger);
  view.rerender(<Harness owner="background-B" />);
  fireEvent.click(screen.getByRole("button", { name: "Open background-B" }));
  await act(async () => resolve());
  expect(screen.queryByRole("menu", { name: "Owned background-A" })).toBeNull();
  fireEvent.click(await screen.findByRole("menuitem", { name: "Pin background-B" }));
  expect(wire.pin).toHaveBeenCalledExactlyOnceWith("background-B");
  expect(wire.request).toHaveBeenCalledOnce();
});

it("reports the actual import failure for the current request and releases it for a new open attempt", async () => {
  render(<Harness />); const trigger = screen.getByRole("button", { name: "Open background-A" }); fireEvent.click(trigger);
  await waitFor(() => expect(wire.request).toHaveBeenCalledOnce());
  const first = loadConversationMenu(); const outcome = first.catch(error => error as Error);
  await act(async () => reject(new Error("Menu interaction chunk unavailable")));
  const error = await outcome;
  await waitFor(() => expect(wire.toast).toHaveBeenCalledExactlyOnceWith(error.message, "error"));
  expect(screen.queryByRole("menu")).toBeNull();
  fireEvent.click(trigger);
  const retry = loadConversationMenu(); const failedRetry = retry.catch(() => {});
  expect(retry).not.toBe(first);
  await act(async () => { await failedRetry; });
  await waitFor(() => expect(wire.toast).toHaveBeenCalledTimes(2));
  expect(useAppStore.getState().draft).toBe("Keep this draft");
});
