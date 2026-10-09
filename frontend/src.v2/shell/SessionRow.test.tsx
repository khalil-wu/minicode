/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionRow } from "./SessionRow";

const callbacks = {
  onSwitch: vi.fn(),
  onSetMenuFor: vi.fn(),
  onStartRename: vi.fn(),
  onCommitRename: vi.fn(),
  onCancelRename: vi.fn(),
  onSetRenameValue: vi.fn(),
  onArchive: vi.fn(),
  onClone: vi.fn(),
  onMerge: vi.fn(),
  onExport: vi.fn(),
  onDelete: vi.fn(),
  onCleanup: vi.fn(),
  onHandoff: vi.fn(),
  onReveal: vi.fn(),
  onCopy: vi.fn(),
};

describe("SessionRow hydration status", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it.each([false, true])("animates a real running row with active=%s", (active) => {
    render(<SessionRow
      conversation={{ id: "live", title: "Live task", updatedAt: "2026-10-08T00:00:00Z" }}
      sessionStatus="running"
      active={active}
      menuOpen={false}
      renaming={false}
      renameValue=""
      waitingLabel={null}
      {...callbacks}
    />);

    const indicator = screen.getByLabelText("任务运行中");
    expect(indicator.classList.contains("session-status-spinner")).toBe(true);
    expect(indicator.classList.contains("mc-session-status-active")).toBe(false);
  });

  it("does not add a running indicator to an idle conversation", () => {
    render(
      <SessionRow
        conversation={{
          id: "conv-1",
          title: "Audit",
          updatedAt: "2026-08-15T00:00:00Z",
        }}
        sessionStatus="idle"
        active
        deleting={false}
        menuOpen={false}
        renaming={false}
        renameValue=""
        waitingLabel={null}
        {...callbacks}
      />,
    );

    expect(screen.queryByLabelText("正在恢复会话上下文")).toBeNull();
    expect(screen.queryByLabelText("任务运行中")).toBeNull();
  });

  it("shows only the conversation title even when summary metadata exists", () => {
    render(
      <SessionRow
        conversation={{
          id: "conv-summary",
          title: "MiniCode release audit",
          updatedAt: "2026-08-15T00:00:00Z",
          summary: "User: audit all agents | Assistant: event contracts aligned",
        }}
        sessionStatus="idle"
        active={false}
        deleting={false}
        menuOpen={false}
        renaming={false}
        renameValue=""
        waitingLabel={null}
        {...callbacks}
      />,
    );

    expect(screen.getByRole("button", { name: "MiniCode release audit" })).toBeTruthy();
    expect(screen.queryByText("audit all agents · event contracts aligned")).toBeNull();
    expect(screen.queryByText(/User:|Assistant:/)).toBeNull();
  });

  it("makes a deleting conversation visibly pending and non-interactive", () => {
    render(
      <SessionRow
        conversation={{
          id: "conv-delete",
          title: "Delete me",
          updatedAt: "2026-08-15T00:00:00Z",
        }}
        sessionStatus="running"
        active={false}
        deleting
        menuOpen={false}
        renaming={false}
        renameValue=""
        waitingLabel={null}
        {...callbacks}
      />,
    );

    expect(screen.queryByLabelText("正在删除会话")).toBeNull();
    expect(screen.queryByLabelText("任务运行中")).toBeNull();
    expect((screen.getByRole("button", { name: "Delete me" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "会话操作" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("switches the active conversation on normal and modified clicks", () => {
    render(
      <SessionRow
        conversation={{ id: "conv-b", title: "Task B", updatedAt: "2026-08-15T00:00:00Z" }}
        sessionStatus="idle"
        active={false}
        menuOpen={false}
        renaming={false}
        renameValue=""
        waitingLabel={null}
        {...callbacks}
      />,
    );

    const row = screen.getByRole("button", { name: "Task B" });
    fireEvent.click(row);
    fireEvent.click(row, { ctrlKey: true });
    fireEvent.click(row, { metaKey: true });
    expect(callbacks.onSwitch).toHaveBeenCalledTimes(3);
    expect(callbacks.onSwitch).toHaveBeenCalledWith("conv-b");
  });

  it("does not re-render an unchanged row when another row menu changes", () => {
    let unchangedTitleReads = 0;
    const firstConversation = {
      id: "conv-first",
      title: "First task",
      updatedAt: "2026-08-15T00:00:00Z",
    };
    const unchangedConversation = {
      id: "conv-unchanged",
      get title() {
        unchangedTitleReads += 1;
        return "Unchanged task";
      },
      updatedAt: "2026-08-15T00:00:01Z",
    };
    const renderRows = (firstMenuOpen: boolean) => (
      <>
        <SessionRow
          conversation={firstConversation}
          sessionStatus="idle"
          active={false}
          deleting={false}
          menuOpen={firstMenuOpen}
          renaming={false}
          renameValue=""
          waitingLabel={null}
          {...callbacks}
        />
        <SessionRow
          conversation={unchangedConversation}
          sessionStatus="idle"
          active={false}
          deleting={false}
          menuOpen={false}
          renaming={false}
          renameValue=""
          waitingLabel={null}
          {...callbacks}
        />
      </>
    );
    const { rerender } = render(renderRows(false));
    const readsAfterInitialRender = unchangedTitleReads;

    rerender(renderRows(true));

    expect(unchangedTitleReads).toBe(readsAfterInitialRender);
  });
});
