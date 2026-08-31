// @vitest-environment jsdom

import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SelectMenu } from "./SelectMenu";

const ControlledSelect = ({ disabled = false }: { disabled?: boolean }) => {
  const [value, setValue] = useState("auto");
  return (
    <SelectMenu ariaLabel="推理强度" value={value} disabled={disabled} onValueChange={setValue}>
      <option value="auto">自动</option>
      <option value="low">低</option>
      <option value="high">高</option>
    </SelectMenu>
  );
};

describe("SelectMenu", () => {
  afterEach(cleanup);

  it("opens a themed menu and updates the controlled value", () => {
    const { container } = render(<ControlledSelect />);

    fireEvent.click(screen.getByRole("button", { name: "推理强度，当前：自动" }));
    fireEvent.click(screen.getByRole("option", { name: "高" }));

    expect(screen.getByRole("button", { name: "推理强度，当前：高" })).toBeTruthy();
    expect((container.querySelector("select") as HTMLSelectElement).value).toBe("high");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("does not open while disabled", () => {
    render(<ControlledSelect disabled />);

    const trigger = screen.getByRole("button", { name: "推理强度，当前：自动" });
    expect((trigger as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(trigger);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("closes with Escape and restores focus to the trigger", async () => {
    render(<ControlledSelect />);
    const trigger = screen.getByRole("button", { name: "推理强度，当前：自动" });

    fireEvent.click(trigger);
    const selected = screen.getByRole("option", { name: "自动" });
    fireEvent.keyDown(selected, { key: "Escape" });

    expect(screen.queryByRole("listbox")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });
});
