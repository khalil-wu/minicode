// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { DesktopPetState } from "../desktop/runtime";
import { useAppStore } from "../stores";
import { SourcePetsTab } from "./SourcePetsTab";

const holder = vi.hoisted(() => ({ runtime: null as unknown }));
vi.mock("../desktop/runtime", async load => ({ ...await load<Record<string, unknown>>(), desktop: () => holder.runtime }));
let state: DesktopPetState;
let changed: (next: DesktopPetState) => void;
let api: {
  getState: ReturnType<typeof vi.fn>; refresh: ReturnType<typeof vi.fn>; update: ReturnType<typeof vi.fn>;
  toggleMini: ReturnType<typeof vi.fn>; openFolder: ReturnType<typeof vi.fn>; getSprite: ReturnType<typeof vi.fn>; onStateChanged: ReturnType<typeof vi.fn>;
};
beforeEach(() => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  state = { pets: [{ id: "codex", displayName: "Codex", description: "The original Codex companion.", assetRef: "codex", spriteVersionNumber: 2, updatedAt: 0 },
    { id: "dewey", displayName: "Dewey", description: "A calm companion", assetRef: "dewey", spriteVersionNumber: 2, updatedAt: 0 }],
    selectedPetId: "codex", size: 112, miniOpen: false, petVisible: true, petDirectory: "C:/private-minicode/pets", reducedMotion: false, mutedConversationIds: [],
    activity: { running: false, waiting: false, failed: false, review: false, tasks: [] }, issues: [] };
  api = {
    getState: vi.fn(async () => state), refresh: vi.fn(async () => state),
    update: vi.fn(async patch => { state = { ...state, ...patch }; return state; }),
    toggleMini: vi.fn(async () => { state = { ...state, miniOpen: !state.miniOpen }; return state; }),
    openFolder: vi.fn(async () => {}), getSprite: vi.fn(async () => "data:image/webp;base64,fixture"),
    onStateChanged: vi.fn(callback => { changed = callback; return vi.fn(); }),
  };
  holder.runtime = { pets: api };
  useAppStore.setState({ draft: "Keep original workspace draft", workingDirectory: "C:/original-workspace" });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("connects selection, Refresh, folder, size and Wake/Tuck to their real desktop consumers", async () => {
  const create = vi.fn(async () => {});
  render(<SourcePetsTab onCreatePet={create} />);
  expect(await screen.findByRole("button", { name: "已选择 Codex" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "选择 Dewey" }));
  await screen.findByRole("button", { name: "已选择 Dewey" });
  expect(api.update).toHaveBeenCalledWith({ selectedPetId: "dewey", petVisible: true });
  fireEvent.change(screen.getByRole("slider", { name: "宠物大小" }), { target: { value: "180" } });
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ size: 180 }));
  await waitFor(() => expect(screen.getByRole("slider", { name: "宠物大小" })).toHaveProperty("value", "180"));
  fireEvent.click(screen.getByRole("button", { name: "唤醒 Mini" }));
  fireEvent.click(await screen.findByRole("button", { name: "收起 Mini" }));
  await screen.findByRole("button", { name: "唤醒 Mini" });
  fireEvent.click(screen.getByRole("button", { name: "打开文件夹" }));
  await waitFor(() => expect(api.openFolder).toHaveBeenCalledOnce());
  await waitFor(() => expect(screen.getByRole("button", { name: "刷新宠物" })).toHaveProperty("disabled", false));
  fireEvent.click(screen.getByRole("button", { name: "刷新宠物" }));
  await waitFor(() => expect(api.refresh).toHaveBeenCalledOnce());
  await waitFor(() => expect(screen.getByRole("button", { name: "创建宠物" })).toHaveProperty("disabled", false));
  fireEvent.click(screen.getByRole("button", { name: "创建宠物" }));
  await waitFor(() => expect(create).toHaveBeenCalledOnce());
  expect(useAppStore.getState()).toMatchObject({ draft: "Keep original workspace draft", workingDirectory: "C:/original-workspace" });
});

it("loads only the active page and applies real broadcasts from other windows", async () => {
  const view = render(<SourcePetsTab active={false} onCreatePet={vi.fn()} />);
  expect(api.getState).not.toHaveBeenCalled(); expect(api.onStateChanged).not.toHaveBeenCalled();
  view.rerender(<SourcePetsTab onCreatePet={vi.fn()} />);
  await screen.findByRole("button", { name: "已选择 Codex" });
  act(() => changed({ ...state, selectedPetId: "dewey", size: 200, miniOpen: true }));
  expect(screen.getByRole("button", { name: "已选择 Dewey" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("slider", { name: "宠物大小" })).toHaveProperty("value", "200");
  expect(screen.getByRole("button", { name: "收起 Mini" })).toBeTruthy();
});

it("preserves an unsuccessful selection and reports actual preparation or manifest errors", async () => {
  state.issues = ["C:/private-minicode/pets/broken/pet.json: invalid JSON"];
  api.update.mockRejectedValueOnce(new Error("Actual preference write error"));
  render(<SourcePetsTab onCreatePet={vi.fn(async () => { throw new Error("Reference image editing is unavailable"); })} />);
  await screen.findByRole("button", { name: "已选择 Codex" });
  expect(screen.getByText(/broken\/pet.json: invalid JSON/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "选择 Dewey" }));
  expect(await screen.findByText(/Actual preference write error/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "已选择 Codex" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "创建宠物" }));
  expect(await screen.findByText(/Reference image editing is unavailable/)).toBeTruthy();
  expect(screen.queryByText("已创建宠物")).toBeNull();
});
