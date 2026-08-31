/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  deletePath: vi.fn(),
  showConfirm: vi.fn(),
}));

vi.mock("../desktop/runtime", () => ({
  isDesktop: () => true,
  desktop: () => ({
    fs: {
      deletePath: mocks.deletePath,
    },
  }),
  revealPath: vi.fn(),
}));

vi.mock("../overlays/DialogService", () => ({
  showConfirm: mocks.showConfirm,
  showPrompt: vi.fn(),
  showAlert: vi.fn(),
}));

import { FileContextMenu } from "./FileTreeContextMenu";

describe("FileContextMenu desktop deletion", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("retries a large directory deletion only after the second confirmation", async () => {
    const onRefresh = vi.fn();
    const onClose = vi.fn();
    mocks.showConfirm.mockResolvedValueOnce(true).mockResolvedValueOnce(true);
    mocks.deletePath
      .mockResolvedValueOnce({
        needsConfirmation: true,
        path: "C:/repo/vendor",
        entryCount: 51,
      })
      .mockResolvedValueOnce({
        deleted: true,
        path: "C:/repo/vendor",
        is_dir: true,
      });

    render(
      <FileContextMenu
        menu={{ path: "C:/repo/vendor", isDir: true, x: 0, y: 0 }}
        workingDirectory="C:/repo"
        onRefresh={onRefresh}
        onClose={onClose}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => expect(mocks.deletePath).toHaveBeenCalledTimes(2));
    expect(mocks.deletePath).toHaveBeenNthCalledWith(1, "C:/repo/vendor", true, false);
    expect(mocks.deletePath).toHaveBeenNthCalledWith(2, "C:/repo/vendor", true, true);
    expect(mocks.showConfirm).toHaveBeenNthCalledWith(2, expect.objectContaining({
      title: "确认删除大型目录",
      message: expect.stringContaining("51+ 个项目"),
    }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalled();
  });

  it("keeps the directory when the second confirmation is cancelled", async () => {
    const onRefresh = vi.fn();
    const onClose = vi.fn();
    mocks.showConfirm.mockResolvedValueOnce(true).mockResolvedValueOnce(false);
    mocks.deletePath.mockResolvedValueOnce({
      needsConfirmation: true,
      path: "C:/repo/vendor",
      entryCount: 51,
    });

    render(
      <FileContextMenu
        menu={{ path: "C:/repo/vendor", isDir: true, x: 0, y: 0 }}
        workingDirectory="C:/repo"
        onRefresh={onRefresh}
        onClose={onClose}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => expect(mocks.showConfirm).toHaveBeenCalledTimes(2));
    expect(mocks.deletePath).toHaveBeenCalledTimes(1);
    expect(mocks.deletePath).toHaveBeenCalledWith("C:/repo/vendor", true, false);
    expect(onRefresh).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("offers the editor only for editable text files", () => {
    const common = { workingDirectory: "C:/repo", onRefresh: vi.fn(), onClose: vi.fn() };
    const { unmount } = render(
      <FileContextMenu menu={{ path: "src/main.py", isDir: false, x: 0, y: 0 }} {...common} />,
    );
    expect(screen.getByRole("button", { name: "在编辑器中打开" })).toBeTruthy();
    unmount();

    render(
      <FileContextMenu menu={{ path: "paper_draft.docx", isDir: false, x: 0, y: 0 }} {...common} />,
    );
    expect(screen.queryByRole("button", { name: "在编辑器中打开" })).toBeNull();
    expect(screen.queryByRole("button", { name: "在预览面板中打开" })).toBeNull();
  });
});
