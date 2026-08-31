/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ComposerTextarea } from "./ComposerTextarea";
import { MAX_EDITABLE_PASTE_CHARS } from "./pastedText";
import { useAppStore } from "../stores";

afterEach(cleanup);

const renderTextarea = (overrides?: {
  value?: string;
  onDropFiles?: (files: File[]) => void;
  onChange?: (v: string) => void;
  onHistorySearch?: () => void;
  compact?: boolean;
  onSubmit?: () => void;
}) => {
  const onDropFiles = overrides?.onDropFiles ?? vi.fn();
  const onChange = overrides?.onChange ?? vi.fn();
  render(
    <ComposerTextarea
      value={overrides?.value ?? ""}
      onChange={onChange}
      onSubmit={overrides?.onSubmit ?? vi.fn()}
      onDropFiles={onDropFiles}
      onHistorySearch={overrides?.onHistorySearch}
      compact={overrides?.compact}
    />,
  );
  return { onDropFiles, onChange, textarea: screen.getByRole("textbox") as HTMLTextAreaElement };
};

// Build a ClipboardEvent-like paste with the given text and optional files.
const firePaste = (textarea: HTMLTextAreaElement, opts: { text?: string; files?: File[] }) => {
  const items = (opts.files ?? []).map((file) => ({
    kind: "file" as const,
    getAsFile: () => file,
  }));
  const clipboardData = {
    items,
    getData: (type: string) => (type === "text" || type === "text/plain" ? opts.text ?? "" : ""),
    files: opts.files ?? [],
  };
  fireEvent.paste(textarea, { clipboardData });
};

describe("ComposerTextarea paste-to-attachment", () => {
  it("uses Enter to send by default and Shift+Enter for a newline", () => {
    const onSubmit = vi.fn();
    useAppStore.setState({ sendShortcut: "enter" });
    const { textarea } = renderTextarea({ onSubmit });

    fireEvent.keyDown(textarea, { key: "Enter" });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });

    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("uses Ctrl/Cmd+Enter when the alternate send shortcut is selected", () => {
    const onSubmit = vi.fn();
    useAppStore.setState({ sendShortcut: "mod-enter" });
    const { textarea } = renderTextarea({ onSubmit });

    fireEvent.keyDown(textarea, { key: "Enter" });
    fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });

    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("uses one font metric set for Latin placeholder and CJK input", () => {
    const { textarea } = renderTextarea({ value: "你好 Hello" });

    expect(textarea.style.fontFamily).toBe("var(--font-prose)");
    expect(textarea.style.fontSize).toBe("var(--text-md)");
    expect(textarea.style.lineHeight).toBe("var(--leading-relaxed)");
    expect(textarea.style.fontWeight).toBe("var(--fw-regular)");
    expect(textarea.style.letterSpacing).toBe("0px");
  });

  it("keeps the Code mode input aligned with the compact desktop composer", () => {
    const { textarea } = renderTextarea({ compact: true });

    expect(textarea.style.minHeight).toBe("44px");
    expect(textarea.style.padding).toBe("8px 12px 6px");
    expect(textarea.getAttribute("placeholder")).toBe("描述任务或提出问题…");
  });

  it("keeps vertical scrolling enabled when an editable draft exceeds the height cap", () => {
    const { textarea } = renderTextarea({ value: Array.from({ length: 80 }, (_, i) => `line ${i}`).join("\n") });

    expect(textarea.classList.contains("overflow-y-auto")).toBe(true);
    expect(textarea.classList.contains("overflow-y-hidden")).toBe(false);
  });

  it("inserts a short text paste normally (no diversion)", () => {
    const { onDropFiles, textarea } = renderTextarea();
    firePaste(textarea, { text: "a short note" });
    // Short paste must NOT be converted to an attachment.
    expect(onDropFiles).not.toHaveBeenCalled();
  });

  it("keeps substantial long-form text editable below the limit", () => {
    const { onDropFiles, textarea } = renderTextarea();
    const bigText = "x".repeat(2500);
    firePaste(textarea, { text: bigText });

    expect(onDropFiles).not.toHaveBeenCalled();
  });

  it("keeps many-line text editable when it is below the character limit", () => {
    const { onDropFiles, textarea } = renderTextarea();
    const manyLines = Array.from({ length: 30 }, (_, i) => `line ${i}`).join("\n");
    firePaste(textarea, { text: manyLines });

    expect(onDropFiles).not.toHaveBeenCalled();
  });

  it("diverts only text that would exceed the editable draft limit", async () => {
    const { onDropFiles, textarea } = renderTextarea();
    const bigText = "长".repeat(MAX_EDITABLE_PASTE_CHARS + 1);
    firePaste(textarea, { text: bigText });

    expect(onDropFiles).toHaveBeenCalledTimes(1);
    const files = onDropFiles.mock.calls[0][0] as File[];
    expect(files).toHaveLength(1);
    expect(files[0].name).toMatch(/^pasted-\d+\.txt$/);
    expect(files[0].type).toBe("text/plain");
    const content = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(reader.error);
      reader.onload = () => resolve(String(reader.result ?? ""));
      reader.readAsText(files[0]);
    });
    expect(content).toBe(bigText);
  });

  it("uses the selected range when deciding whether the resulting draft fits", () => {
    const existing = "x".repeat(MAX_EDITABLE_PASTE_CHARS);
    const { onDropFiles, textarea } = renderTextarea({ value: existing });
    textarea.setSelectionRange(0, 100);

    firePaste(textarea, { text: "replacement" });

    expect(onDropFiles).not.toHaveBeenCalled();
  });

  it("prioritizes file-clipboard items over the text branch", () => {
    const { onDropFiles, textarea } = renderTextarea();
    const img = new File(["binary"], "screenshot.png", { type: "image/png" });
    // Even with a large accompanying text payload, a real file wins.
    firePaste(textarea, { text: "x".repeat(5000), files: [img] });

    expect(onDropFiles).toHaveBeenCalledTimes(1);
    const files = onDropFiles.mock.calls[0][0] as File[];
    expect(files).toHaveLength(1);
    expect(files[0].name).toBe("screenshot.png");
  });

  it("opens workspace prompt history with Ctrl+R without submitting", () => {
    const onHistorySearch = vi.fn();
    const { textarea } = renderTextarea({ onHistorySearch });
    fireEvent.keyDown(textarea, { key: "r", ctrlKey: true });
    expect(onHistorySearch).toHaveBeenCalledTimes(1);
  });
});
