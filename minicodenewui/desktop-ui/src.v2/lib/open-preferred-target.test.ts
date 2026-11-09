// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import { isNativeExternalFile, openPreferredFile, preferredFileTargetForWorkspace } from "./open-preferred-target";
import type { DesktopPreferences } from "../desktop/runtime";
const mocks = vi.hoisted(() => ({ open: vi.fn() }));
vi.mock("../desktop/runtime", async load => ({ ...await load<typeof import("../desktop/runtime")>(), isDesktop: () => true, desktop: () => ({ openPreferredPath: mocks.open }) }));

beforeEach(() => { mocks.open.mockReset(); mocks.open.mockResolvedValue({ openInEditor: false }); });

it("passes the captured root and one-time file target while preserving line and column", async () => {
  await openPreferredFile("src/app.ts", "C:/workspace-A", 7, undefined, { targetId: "cursor", column: 4 });
  expect(mocks.open).toHaveBeenCalledWith("C:/workspace-A/src/app.ts", 7, { workspaceRoot: "C:/workspace-A", targetId: "cursor", column: 4 });
  expect(mocks.open.mock.calls[0][2]).not.toHaveProperty("persistPreferredTargetPath");
});

it("projects the native per-path choice for the same Windows workspace into menu labels", () => {
  const preferences = { fileOpenTarget: "cursor", fileOpenTargetPerPath: { "C:\\workspace-A": "vscode", "C:\\workspace-B": "fileManager" } } as DesktopPreferences;
  expect(preferredFileTargetForWorkspace(preferences, "C:/workspace-A")).toBe("vscode");
  expect(preferredFileTargetForWorkspace(preferences, "C:/workspace-B")).toBe("fileManager");
  expect(preferredFileTargetForWorkspace(preferences, "C:/workspace-C")).toBe("cursor");
});

it("classifies native files using the source filename suffix, including literal hash characters and dotfile names", () => {
  expect(isNativeExternalFile("C:/files/report #1.PDF")).toBe(true);
  expect(isNativeExternalFile("C:/files/.png")).toBe(false);
  expect(isNativeExternalFile("C:/files/.draft.xlsx")).toBe(true);
  expect(isNativeExternalFile("C:/files/image.svg")).toBe(false);
});
