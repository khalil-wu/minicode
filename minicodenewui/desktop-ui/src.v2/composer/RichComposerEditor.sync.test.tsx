/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { EditorView } from "prosemirror-view";
import { RichComposerEditor, type ComposerInputHandle } from "./RichComposerEditor";

vi.hoisted(() => Object.defineProperty(globalThis, "matchMedia", { configurable: true,
  value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) }));

beforeEach(() => {
  const rect = new DOMRect(0, 0, 600, 80);
  Object.defineProperty(Range.prototype, "getClientRects", { configurable: true, value: () => [rect] });
  Object.defineProperty(Range.prototype, "getBoundingClientRect", { configurable: true, value: () => rect });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(rect);
  vi.spyOn(window, "scrollBy").mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("mounts once, preserves selection and skips unchanged editor props while rendering real changes", () => {
  const propsUpdate = vi.spyOn(EditorView.prototype, "setProps");
  const stateUpdate = vi.spyOn(EditorView.prototype, "updateState");
  const ref = createRef<ComposerInputHandle>();
  const props = { value: "Retained text", disabled: false, placeholder: "Write", ariaLabel: "Prompt", enterBehavior: "enter",
    plainTextMode: true, initialSelection: { start: 2, end: 5 }, initialFocus: true,
    onChange: vi.fn(), onSelectionChange: vi.fn(), onFocusChange: vi.fn(), onKeyDown: vi.fn(),
    onPaste: vi.fn(), onDrop: vi.fn(), onCompositionChange: vi.fn() };
  const view = render(<RichComposerEditor ref={ref} {...props} />);
  expect(stateUpdate).not.toHaveBeenCalled();
  expect(propsUpdate).not.toHaveBeenCalled();
  expect([ref.current!.selectionStart, ref.current!.selectionEnd]).toEqual([2, 5]);
  view.rerender(<RichComposerEditor ref={ref} {...props} references={[]} />);
  expect(propsUpdate).not.toHaveBeenCalled();
  expect(stateUpdate).not.toHaveBeenCalled();
  view.rerender(<RichComposerEditor ref={ref} {...props} disabled ariaLabel="Paused prompt" />);
  const input = screen.getByRole("textbox", { name: "Paused prompt" });
  expect(input.getAttribute("aria-disabled")).toBe("true");
  expect(input.getAttribute("contenteditable")).toBe("false");
  expect([ref.current!.selectionStart, ref.current!.selectionEnd]).toEqual([2, 5]);
  expect(propsUpdate).toHaveBeenCalledTimes(1);
});

it("updates empty placeholders and applies deferred input changes after IME completion", async () => {
  const props = { value: "", disabled: false, placeholder: "Initial prompt", ariaLabel: "Prompt", enterBehavior: "enter",
    plainTextMode: true, initialSelection: { start: 0, end: 0 }, initialFocus: false,
    onChange: vi.fn(), onSelectionChange: vi.fn(), onFocusChange: vi.fn(), onKeyDown: vi.fn(),
    onPaste: vi.fn(), onDrop: vi.fn(), onCompositionChange: vi.fn() };
  const view = render(<RichComposerEditor {...props} />);
  const input = screen.getByRole("textbox", { name: "Prompt" });
  expect(input.getAttribute("data-placeholder")).toBe("Initial prompt");
  view.rerender(<RichComposerEditor {...props} placeholder="Changed prompt" />);
  expect(input.getAttribute("data-placeholder")).toBe("Changed prompt");
  fireEvent.compositionStart(input);
  view.rerender(<RichComposerEditor {...props} value="恢复输入" placeholder="Final prompt" disabled />);
  expect(input.textContent).toBe("");
  fireEvent.compositionEnd(input);
  await act(() => new Promise(resolve => setTimeout(resolve, 30)));
  expect(input.textContent).toBe("恢复输入");
  expect(input.getAttribute("aria-disabled")).toBe("true");
});
