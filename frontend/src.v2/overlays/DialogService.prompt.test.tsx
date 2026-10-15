// @vitest-environment jsdom
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { showPrompt } from "./DialogService";

afterEach(cleanup);

it("distinguishes an empty Rename submission from Cancel when the source dialog permits empty input", async () => {
  let result!: Promise<string | null>;
  await act(async () => { result = showPrompt({ title: "Rename chat", message: "Name", defaultValue: "Old name", confirmLabel: "Save", allowEmpty: true }); });
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await result).toBe("");
});

it("keeps the existing empty-value behavior for unrelated prompts", async () => {
  let result!: Promise<string | null>;
  await act(async () => { result = showPrompt({ title: "Name", message: "Name", confirmLabel: "Save" }); });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(await result).toBeNull();
});
