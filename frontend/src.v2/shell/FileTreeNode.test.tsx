/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    value: () => ({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    }),
  });
});

import { useAppStore } from "../stores";
import { TreeNode } from "./FileTreeNode";

const originalOpenEditorFile = useAppStore.getState().openEditorFile;

afterEach(() => {
  cleanup();
  useAppStore.setState({ openEditorFile: originalOpenEditorFile });
});

describe("TreeNode", () => {
  it("reports explicit navigation even when reopening the active file", () => {
    const openEditorFile = vi.fn();
    const onNavigate = vi.fn();
    useAppStore.setState({ openEditorFile });

    render(
      <TreeNode
        node={{ name: "app.ts", path: "app.ts", is_dir: false }}
        depth={0}
        gitMap={new Map()}
        loadingPaths={new Set()}
        expandedPaths={new Set()}
        query=""
        workingDirectory="C:\\Desktop\\MiniCode"
        activeEditorPath="app.ts"
        density="compact"
        onToggleExpanded={vi.fn()}
        onContextMenu={vi.fn()}
        onNavigate={onNavigate}
      />,
    );

    fireEvent.click(screen.getByRole("treeitem"));

    expect(openEditorFile).toHaveBeenCalledWith("app.ts", "app.ts");
    expect(onNavigate).toHaveBeenCalledTimes(1);
  });
});
