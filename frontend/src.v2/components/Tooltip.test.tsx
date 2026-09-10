// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { Tip } from "./Tooltip";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("escapes a clipping pane, stays in the viewport and follows its trigger on resize", () => {
  let x = window.innerWidth - 24;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return (this.classList.contains("mc-tip")
      ? { left: x, right: x + 24, top: 2, bottom: 26, width: 24, height: 24 }
      : { left: 0, right: 140, top: 0, bottom: 28, width: 140, height: 28 }) as DOMRect;
  });
  render(<div style={{ overflow: "hidden", width: 24 }}><Tip content="完整提示"><button>主题</button></Tip></div>);
  const trigger = screen.getByRole("button", { name: "主题" });
  fireEvent.focus(trigger);
  const hint = screen.getByText("完整提示");
  expect(hint.parentElement).toBe(document.body);
  expect(Number.parseFloat(hint.style.left) + 140).toBeLessThanOrEqual(window.innerWidth - 8);
  expect(Number.parseFloat(hint.style.top)).toBe(32);
  x = 200;
  fireEvent(window, new Event("resize"));
  expect(Number.parseFloat(hint.style.left)).toBe(142);
  fireEvent.pointerLeave(trigger.parentElement!);
  expect(screen.queryByText("完整提示")).not.toBeNull();
  fireEvent.blur(trigger, { relatedTarget: null });
  expect(screen.queryByText("完整提示")).toBeNull();
});

it("dismisses a pointer hint with Escape without consuming the enclosing surface's key event", () => {
  const escape = vi.fn();
  render(<div onKeyDown={escape}><Tip content="关闭提示"><button>操作</button></Tip></div>);
  const trigger = screen.getByRole("button", { name: "操作" });
  fireEvent.pointerEnter(trigger.parentElement!);
  expect(screen.queryByText("关闭提示")).not.toBeNull();
  fireEvent.keyDown(trigger, { key: "Escape" });
  expect(screen.queryByText("关闭提示")).toBeNull();
  expect(escape).toHaveBeenCalledOnce();
});
