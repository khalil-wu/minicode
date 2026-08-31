/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { pushToast, ToastContainer } from "./ToastContainer";

describe("ToastContainer", () => {
  afterEach(() => cleanup());

  it("exposes a named dismiss button", async () => {
    render(<ToastContainer />);
    act(() => pushToast("Saved", "success", 0));

    const toast = screen.getByRole("status");
    expect(toast.textContent).toContain("Saved");
    expect(toast.getAttribute("title")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "关闭通知" }));

    await waitFor(() => expect(screen.queryByText("Saved")).toBeNull());
  });

  it("keeps transient success feedback compact", () => {
    render(<ToastContainer />);
    act(() => pushToast("Saved", "success", 1200));

    expect(screen.getByRole("status").textContent).toContain("Saved");
    // Every toast is dismissible now, including transient success ones.
    expect(screen.queryByRole("button", { name: "关闭通知" })).not.toBeNull();
  });

  it("deduplicates notifications and keeps only the newest three", () => {
    render(<ToastContainer />);
    act(() => {
      pushToast("First", "info", 0);
      pushToast("Second", "info", 0);
      pushToast("Third", "info", 0);
      pushToast("Fourth", "info", 0);
      pushToast("Fourth", "info", 0);
    });

    expect(screen.queryByText("First")).toBeNull();
    expect(screen.getByText("Second")).toBeTruthy();
    expect(screen.getByText("Third")).toBeTruthy();
    expect(screen.getAllByText("Fourth")).toHaveLength(1);
  });
});
