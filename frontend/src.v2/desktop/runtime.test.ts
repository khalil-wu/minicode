/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

import { openPath, ptyList, ptySnapshot, ptySpawn, revealPath } from "./runtime";
import { pushToast } from "../overlays/ToastContainer";

const spawn = vi.fn();
const list = vi.fn();
const snapshot = vi.fn();

describe("desktop PTY owner normalization", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.__MINICODE_RUNTIME__ = {
      desktop: {
        platformInfo: { isDesktop: true },
        pty: {
          spawn,
          list,
          snapshot,
        },
      } as never,
    };
  });

  afterEach(() => {
    delete window.__MINICODE_RUNTIME__;
  });

  it("filters list results to the requested conversation owner", async () => {
    list.mockResolvedValue([
      { session_id: "term_owned", conversation_id: "conv_owned", shell: "pwsh", cwd: "C:/owned" },
      { session_id: "term_other", conversation_id: "conv_other", shell: "pwsh", cwd: "C:/other" },
      { session_id: "term_missing", shell: "pwsh", cwd: "C:/missing" },
    ]);

    await expect(ptyList("conv_owned")).resolves.toMatchObject([
      { sessionId: "term_owned", conversationId: "conv_owned" },
    ]);
    expect(list).toHaveBeenCalledWith("conv_owned");
    await expect(ptyList("   ")).resolves.toEqual([]);
  });

  it("rejects spawn and snapshot payloads whose owner does not match", async () => {
    spawn.mockResolvedValue({
      session_id: "term_other",
      conversation_id: "conv_other",
      shell: "pwsh",
      cwd: "C:/other",
    });
    snapshot.mockResolvedValue({
      session_id: "term_other",
      conversation_id: "conv_other",
      shell: "pwsh",
      cwd: "C:/other",
      output: "secret",
    });

    await expect(ptySpawn("C:/owned", "conv_owned")).resolves.toBeNull();
    await expect(ptySnapshot("term_other", "conv_owned")).resolves.toBeNull();
    expect(spawn).toHaveBeenCalledWith("C:/owned", "conv_owned");
    expect(snapshot).toHaveBeenCalledWith("term_other", 80_000, "conv_owned");
  });
});

// Browser mode is a supported deployment. `openPath`/`revealPath` used to return
// undefined without throwing, so every "open with the default app" / "reveal in
// the file manager" action was a dead click that said nothing.
describe("desktop shell actions in browser mode", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    delete window.__MINICODE_RUNTIME__;
  });

  it("explains why a path cannot be opened when no desktop runtime is present", () => {
    expect(openPath("C:/repo/report.pdf")).toBeUndefined();
    expect(pushToast).toHaveBeenCalledWith(
      "使用默认应用打开文件需要桌面版 MiniCode，浏览器预览模式下不可用。",
      "warning",
      4000,
    );

    vi.mocked(pushToast).mockClear();
    expect(revealPath("C:/repo/report.pdf")).toBeUndefined();
    expect(pushToast).toHaveBeenCalledWith(
      "在文件管理器中显示需要桌面版 MiniCode，浏览器预览模式下不可用。",
      "warning",
      4000,
    );
  });

  it("forwards to the desktop bridge without a toast when the runtime is present", () => {
    const openPathBridge = vi.fn();
    const revealPathBridge = vi.fn();
    window.__MINICODE_RUNTIME__ = {
      desktop: {
        platformInfo: { isDesktop: true },
        openPath: openPathBridge,
        revealPath: revealPathBridge,
      } as never,
    };

    openPath("C:/repo/report.pdf");
    revealPath("C:/repo/report.pdf");

    expect(openPathBridge).toHaveBeenCalledWith("C:/repo/report.pdf");
    expect(revealPathBridge).toHaveBeenCalledWith("C:/repo/report.pdf");
    expect(pushToast).not.toHaveBeenCalled();
  });
});
