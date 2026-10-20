/* @vitest-environment jsdom */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fireEvent, screen, waitFor } from "@testing-library/dom";
import { afterEach, expect, it, vi } from "vitest";

afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); });

it("preserves Mini follow-up text and focus through updates, rejection, and edits during submission", async () => {
  document.body.innerHTML = '<div id="activity"></div><div id="pet"></div><div id="error" hidden></div>';
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: true, addEventListener() {} }) });
  let update!: (state: unknown) => void;
  let accept!: () => void;
  let reject!: (reason: Error) => void;
  const state = { locale: "en-US", size: 112, miniOpen: true, petVisible: true, reducedMotion: true,
    selectedPetId: "codex", pets: [{ id: "codex", displayName: "Codex", spriteVersionNumber: 2, updatedAt: 0 }],
    animations: JSON.parse(readFileSync(resolve("public/pets/animations.json"), "utf8")), layout: { noticeHeight: 260 },
    activity: { running: true, waiting: false, failed: false, review: false,
      tasks: [{ id: "actual-task", title: "Actual task", status: "running", followUpAllowed: true }] } };
  const act = vi.fn(async (action: string) => {
    if (action === "task-followup") await new Promise<void>((resolve, decline) => { accept = resolve; reject = decline; });
  });
  const miniPet = { act, getState: async () => state, getSprite: async () => "data:image/png;base64,fixture", onState: (callback: typeof update) => { update = callback; }, onError() {} };
  const source = readFileSync(resolve("../desktop/pet-window.js"), "utf8");
  new Function("window", "document", "requestAnimationFrame", "setTimeout", "clearTimeout", source)(
    { miniPet, matchMedia: window.matchMedia }, document, (callback: () => void) => callback(), () => 0, () => {});
  const input = await screen.findByRole("textbox", { name: "Follow up…" }) as HTMLTextAreaElement;
  fireEvent.input(input, { target: { value: "Unsent Mini message" } }); input.focus(); input.setSelectionRange(4, 4);
  update({ ...state, size: 120 });
  expect(document.activeElement).toBe(screen.getByRole("textbox"));
  expect(screen.getByRole("textbox")).toHaveProperty("value", "Unsent Mini message");
  fireEvent.submit(screen.getByRole("textbox").closest("form")!);
  await waitFor(() => expect(act).toHaveBeenCalledWith("task-followup", { taskId: "actual-task", prompt: "Unsent Mini message" }));
  reject(new Error("Actual refusal"));
  await screen.findByRole("alert");
  expect(screen.getByRole("textbox")).toHaveProperty("value", "Unsent Mini message");
  fireEvent.submit(screen.getByRole("textbox").closest("form")!);
  fireEvent.input(screen.getByRole("textbox"), { target: { value: "New message typed during send" } });
  accept();
  await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toHaveProperty("disabled", false));
  expect(screen.getByRole("textbox")).toHaveProperty("value", "New message typed during send");
});

it("keeps IME composition alive through activity updates and sends the complete multiline text only on non-composing Enter", async () => {
  document.body.innerHTML = '<div id="activity"></div><div id="pet"></div><div id="error" hidden></div>';
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: true, addEventListener() {} }) });
  let update!: (state: unknown) => void;
  const state = { locale: "en-US", size: 112, miniOpen: true, petVisible: true, reducedMotion: true,
    selectedPetId: "codex", pets: [{ id: "codex", displayName: "Codex", spriteVersionNumber: 2, updatedAt: 0 }],
    animations: JSON.parse(readFileSync(resolve("public/pets/animations.json"), "utf8")), layout: { noticeHeight: 260 },
    activity: { running: true, waiting: false, failed: false, review: false, tasks: [{ id: "actual-task", title: "Actual task", status: "running", followUpAllowed: true }] } };
  const act = vi.fn(async (_action: string, _payload?: unknown) => {});
  const miniPet = { act, getState: async () => state, getSprite: async () => "data:image/png;base64,fixture", onState: (callback: typeof update) => { update = callback; }, onError() {} };
  new Function("window", "document", "requestAnimationFrame", "setTimeout", "clearTimeout", readFileSync(resolve("../desktop/pet-window.js"), "utf8"))(
    { miniPet, matchMedia: window.matchMedia }, document, (callback: () => void) => callback(), () => 0, () => {});
  const input = await screen.findByRole("textbox", { name: "Follow up…" }) as HTMLTextAreaElement;
  input.focus(); fireEvent.compositionStart(input); fireEvent.input(input, { target: { value: "中文候选" } });
  update({ ...state, size: 120 });
  expect(screen.getByRole("textbox")).toBe(input);
  fireEvent.keyDown(input, { key: "Enter", isComposing: true });
  expect(act.mock.calls.some(([action]) => action === "task-followup")).toBe(false);
  fireEvent.compositionEnd(input);
  const current = screen.getByRole("textbox");
  expect(current).toHaveProperty("value", "中文候选");
  fireEvent.keyDown(current, { key: "Enter", shiftKey: true });
  expect(act.mock.calls.some(([action]) => action === "task-followup")).toBe(false);
  fireEvent.input(current, { target: { value: "中文候选\nSecond line" } });
  fireEvent.keyDown(current, { key: "Enter" });
  await waitFor(() => expect(act).toHaveBeenCalledWith("task-followup", { taskId: "actual-task", prompt: "中文候选\nSecond line" }));
});
