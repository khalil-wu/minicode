import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ native: true, save: vi.fn() }));
vi.mock("../desktop/runtime", () => ({ desktop: () => ({ saveFile: mocks.save }), isDesktop: () => mocks.native }));
import { saveBlobFile } from "./save-blob";
beforeEach(() => { mocks.native = true; mocks.save.mockReset(); });
afterEach(() => vi.unstubAllGlobals());

describe("original-byte save lifecycle", () => {
  it("awaits native completion with exact binary bytes and filename", async () => {
    let finish!: (saved: boolean) => void;
    mocks.save.mockImplementation(() => new Promise<boolean>(resolve => { finish = resolve; }));
    const bytes = new Uint8Array([0, 255, 13, 10, 65]);
    const saved = saveBlobFile(new Blob([bytes]), "原文件.bin");
    await vi.waitFor(() => expect(mocks.save).toHaveBeenCalledWith("原文件.bin", bytes));
    finish(true); expect(await saved).toBe(true);
  });
  it("keeps cancel distinct from a failed native write", async () => {
    mocks.save.mockResolvedValueOnce(false).mockRejectedValueOnce(new Error("Access denied for selected destination."));
    expect(await saveBlobFile(new Blob(["bytes"]), "file.txt")).toBe(false);
    await expect(saveBlobFile(new Blob(["bytes"]), "file.txt")).rejects.toThrow("Access denied for selected destination.");
  });
  it("retains a browser Blob URL through pending download and releases it when the page is left", async () => {
    mocks.native = false;
    const anchor = { href: "", download: "", click: vi.fn(), remove: vi.fn() };
    const createObjectURL = vi.fn(() => "blob:pending-original"); const revokeObjectURL = vi.fn();
    const events = new EventTarget();
    vi.stubGlobal("document", { createElement: () => anchor, body: { appendChild: vi.fn() } });
    vi.stubGlobal("window", events);
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    const original = new Blob(["actual original bytes"]);
    expect(await saveBlobFile(original, "original.txt")).toBe(true);
    expect(anchor).toMatchObject({ href: "blob:pending-original", download: "original.txt" });
    expect(createObjectURL).toHaveBeenCalledWith(original); expect(anchor.click).toHaveBeenCalledOnce();
    expect(revokeObjectURL).not.toHaveBeenCalled();
    events.dispatchEvent(new Event("pagehide")); expect(revokeObjectURL).toHaveBeenCalledWith("blob:pending-original");
  });
});
