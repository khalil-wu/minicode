import { isValidElement } from "react";
import { describe, expect, it } from "vitest";

import {
  fileGlyphKind,
  fileIcon,
  hasLoadedDirectoryNode,
  isMissingWorkspaceError,
  isPathInsideTreeRoot,
  normalizeDesktopExpandedPaths,
} from "./fileTreeHelpers";

describe("isMissingWorkspaceError", () => {
  it("recognizes explicit missing workspace errors", () => {
    expect(isMissingWorkspaceError(new Error("Workspace folder is missing: C:\\missing"))).toBe(true);
    expect(isMissingWorkspaceError(new Error("Path not found: src"))).toBe(true);
  });

  it("does not treat generic file tree loading failures as missing workspaces", () => {
    expect(isMissingWorkspaceError(new Error("Could not load file tree: API request failed."))).toBe(false);
  });
});

describe("fileIcon", () => {
  it("uses semantic vector glyphs for known file types", () => {
    const icon = fileIcon("noticeEvents.ts");

    expect(isValidElement(icon)).toBe(true);
    expect(isValidElement<{ className?: string; "data-file-kind"?: string }>(icon) ? icon.props.className : "").toBe("file-tree-file-icon");
    expect(isValidElement<{ className?: string; "data-file-kind"?: string }>(icon) ? icon.props["data-file-kind"] : "").toBe("code");
  });

  it("distinguishes documents from unknown files", () => {
    const document = fileIcon("LICENSE");
    const unknown = fileIcon("unknown.binary");

    expect(isValidElement<{ "data-file-kind"?: string }>(document) ? document.props["data-file-kind"] : undefined).toBe("document");
    expect(isValidElement<{ "data-file-kind"?: string }>(unknown) ? unknown.props["data-file-kind"] : undefined).toBe("generic");
  });
  it("classifies package/config/pdf/path basenames", () => {
    expect(fileGlyphKind("package.json")).toBe("package");
    expect(fileGlyphKind("tsconfig.json")).toBe("config");
    expect(fileGlyphKind("docs/manual.pdf")).toBe("pdf");
    expect(fileGlyphKind("src/components/Button.tsx")).toBe("code");
    expect(fileGlyphKind(".gitignore")).toBe("git");
  });

  it("honors custom size and className options", () => {
    const icon = fileIcon("styles.css", { size: 12, className: "custom-file-icon" });
    expect(isValidElement<{ width?: number; className?: string; "data-file-kind"?: string }>(icon) ? icon.props.width : undefined).toBe(12);
    expect(isValidElement<{ size?: number; className?: string; "data-file-kind"?: string }>(icon) ? icon.props.className : undefined).toBe("custom-file-icon");
    expect(isValidElement<{ size?: number; className?: string; "data-file-kind"?: string }>(icon) ? icon.props["data-file-kind"] : undefined).toBe("style");
  });
});

describe("normalizeDesktopExpandedPaths", () => {
  it("resolves persisted relative expanded folders against the active workspace", () => {
    const paths = normalizeDesktopExpandedPaths("C:\\Desktop\\MiniCode", new Set([
      "backend",
      "backend/tools",
      "C:\\Desktop\\MiniCode\\frontend",
    ]));

    expect(Array.from(paths).sort()).toEqual([
      "C:\\Desktop\\MiniCode\\frontend",
      "C:\\Desktop\\MiniCode/backend",
      "C:\\Desktop\\MiniCode/backend/tools",
    ].sort());
  });

  it("drops persisted expanded folders outside the active workspace", () => {
    const paths = normalizeDesktopExpandedPaths("C:\\Desktop\\temp", new Set([
      "apple-site",
      "..\\MiniCode",
      "C:\\Desktop\\MiniCode\\frontend",
      "C:\\Desktop\\temp\\valid",
    ]));

    expect(Array.from(paths).sort()).toEqual([
      "C:\\Desktop\\temp/apple-site",
      "C:\\Desktop\\temp\\valid",
    ].sort());
  });
});

describe("file tree path guards", () => {
  it("treats normalized traversal as outside the current root", () => {
    expect(isPathInsideTreeRoot("C:\\Desktop\\temp", "C:\\Desktop\\temp\\apple-site")).toBe(true);
    expect(isPathInsideTreeRoot("C:\\Desktop\\temp", "C:\\Desktop\\MiniCode")).toBe(false);
  });

  it("keeps POSIX workspace paths case-sensitive", () => {
    expect(isPathInsideTreeRoot("/tmp/Project", "/tmp/Project/src")).toBe(true);
    expect(isPathInsideTreeRoot("/tmp/Project", "/tmp/project/src")).toBe(false);
  });

  it("only considers directories already present in the loaded tree", () => {
    const tree = {
      name: "temp",
      path: "C:\\Desktop\\temp",
      is_dir: true,
      children: [
        { name: "existing", path: "C:\\Desktop\\temp\\existing", is_dir: true, children: [] },
        { name: "readme.md", path: "C:\\Desktop\\temp\\readme.md", is_dir: false },
      ],
    };

    expect(hasLoadedDirectoryNode(tree, "C:\\Desktop\\temp\\existing")).toBe(true);
    expect(hasLoadedDirectoryNode(tree, "C:\\Desktop\\temp\\missing")).toBe(false);
    expect(hasLoadedDirectoryNode(tree, "C:\\Desktop\\temp\\readme.md")).toBe(false);
  });
});
