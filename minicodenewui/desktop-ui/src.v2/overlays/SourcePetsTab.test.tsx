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

it("connects the preview cards, Show Mini, Customize and Reset to their actual preferences", async () => {
  const create = vi.fn(async () => {});
  render(<SourcePetsTab onCreatePet={create} />);
  expect((await screen.findByRole("button", { name: "Codex The original Codex companion." })).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "Dewey A calm companion" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Dewey A calm companion" }).getAttribute("aria-pressed")).toBe("true"));
  expect(api.update).toHaveBeenCalledWith({ selectedPetId: "dewey" });
  expect(state.miniOpen).toBe(false);
  expect(screen.queryByRole("slider")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "自定义", exact: true }));
  const dialog = screen.getByRole("dialog", { name: "自定义虚拟宠物" });
  expect(screen.getByRole("button", { name: "重置" })).toHaveProperty("disabled", true);
  fireEvent.change(screen.getByRole("slider", { name: "虚拟宠物大小" }), { target: { value: "180" } });
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ size: 180 }));
  await waitFor(() => expect(screen.getByRole("slider", { name: "虚拟宠物大小" })).toHaveProperty("value", "180"));
  fireEvent.click(screen.getByRole("button", { name: "重置" }));
  await waitFor(() => expect(screen.getByRole("slider", { name: "虚拟宠物大小" })).toHaveProperty("value", "112"));
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "显示 Mini" }));
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ petVisible: true }));
  expect(api.toggleMini).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.getByRole("button", { name: "打开文件夹" })).toHaveProperty("disabled", false));
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
  await screen.findByRole("button", { name: "Codex The original Codex companion." });
  act(() => changed({ ...state, selectedPetId: "dewey", size: 200, miniOpen: true }));
  expect(screen.getByRole("button", { name: "Dewey A calm companion" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "自定义", exact: true }));
  expect(screen.getByRole("slider", { name: "虚拟宠物大小" })).toHaveProperty("value", "200");
  view.rerender(<SourcePetsTab active={false} onCreatePet={vi.fn()} />);
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("preserves an unsuccessful selection and reports actual preparation or manifest errors", async () => {
  state.issues = ["C:/private-minicode/pets/broken/pet.json: invalid JSON"];
  api.update.mockRejectedValueOnce(new Error("Actual preference write error"));
  render(<SourcePetsTab onCreatePet={vi.fn(async () => { throw new Error("Reference image editing is unavailable"); })} />);
  await screen.findByRole("button", { name: "Codex The original Codex companion." });
  expect(screen.getByText(/broken\/pet.json: invalid JSON/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Dewey A calm companion" }));
  expect(await screen.findByText(/Actual preference write error/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Codex The original Codex companion." }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "创建宠物" }));
  expect(await screen.findByText(/Reference image editing is unavailable/)).toBeTruthy();
  expect(screen.queryByText("已创建宠物")).toBeNull();
});

it("selects None independently of Mini visibility and keeps the existing size", async () => {
  state.size = 180; state.miniOpen = true;
  render(<SourcePetsTab onCreatePet={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "无 轻量级 Mini 小伙伴" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "无 轻量级 Mini 小伙伴" }).getAttribute("aria-pressed")).toBe("true"));
  expect(api.update).toHaveBeenCalledExactlyOnceWith({ selectedPetId: null });
  expect(state).toMatchObject({ selectedPetId: null, size: 180, petVisible: true, miniOpen: true });
  expect(screen.getByRole("button", { name: "自定义", exact: true })).toHaveProperty("disabled", true);
  expect(api.toggleMini).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "显示 Mini" }));
  await waitFor(() => expect(api.update).toHaveBeenLastCalledWith({ petVisible: true }));
  expect(state.selectedPetId).toBeNull();
});
