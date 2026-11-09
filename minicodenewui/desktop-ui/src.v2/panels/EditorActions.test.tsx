/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EditorActions } from "./EditorActions";

afterEach(cleanup);
const props = {
  onAction: vi.fn(), supportedActions: [], wordWrap: true, onToggleWordWrap: vi.fn(),
  minimap: false, onToggleMinimap: vi.fn(), showMinimap: false, readOnly: false,
};

describe("editor Save All action", () => {
  it("shows the actual dirty-file count and invokes one explicit Save All action", () => {
    const onSaveAll = vi.fn();
    render(<EditorActions {...props} onSaveAll={onSaveAll} dirtyCount={3} />);
    fireEvent.click(screen.getByRole("button", { name: "更多编辑器操作" }));
    const save = screen.getByRole("menuitem", { name: /保存全部（3）/ });
    expect(save.textContent).toContain("Ctrl+Shift+S");
    fireEvent.click(save);
    expect(onSaveAll).toHaveBeenCalledOnce();
  });

  it("disables Save All when every file is already saved", () => {
    const onSaveAll = vi.fn();
    render(<EditorActions {...props} onSaveAll={onSaveAll} dirtyCount={0} />);
    fireEvent.click(screen.getByRole("button", { name: "更多编辑器操作" }));
    const save = screen.getByRole("menuitem", { name: /保存全部（0）/ }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(save);
    expect(onSaveAll).not.toHaveBeenCalled();
  });
});
