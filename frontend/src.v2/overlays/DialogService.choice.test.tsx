/* @vitest-environment jsdom */
import { act, fireEvent, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { showChoice } from "./DialogService";

describe("permission choice dialogs", () => {
  it.each(["继续", "使用完全访问", "Escape"] as const)("distinguishes %s from closing the dialog", async (action) => {
    let result!: Promise<boolean | null>;
    act(() => { result = showChoice({ title: "Ultra 权限选择", message: "当前使用完全访问", cancelLabel: "继续", confirmLabel: "使用完全访问", danger: true }); });
    if (action === "Escape") fireEvent.keyDown(screen.getByRole("dialog", { name: "Ultra 权限选择" }), { key: "Escape" });
    else fireEvent.click(screen.getByRole("button", { name: action }));
    expect(await result).toBe(action === "Escape" ? null : action === "使用完全访问");
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
