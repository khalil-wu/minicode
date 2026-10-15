/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AssistantMarkdownCell } from "./AssistantMarkdownCell";
import { useAppStore } from "../../stores";

const mocks = vi.hoisted(() => ({ write: vi.fn(async (_items: unknown[]) => {}), writeText: vi.fn(async (_text: string) => {}), toast: vi.fn() }));
vi.mock("../../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
const initial = useAppStore.getState();
class Item {
  readonly types: string[];
  constructor(readonly contents: Record<string, Blob>) { this.types = Object.keys(contents); }
}
const blobText = (blob: Blob) => new Promise<string>((resolve, reject) => {
  const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(reader.error); reader.readAsText(blob);
});
const markdown = "## Result\n\nA **bold** reply with [documentation](https://example.test/docs).\n\n```ts\nconst value = 1;\nconsole.log(value);\n```\n\n- [x] Complete\n- [ ] Pending\n\n| Name | Value |\n| --- | --- |\n| One | Two |";
const view = (content = markdown) => render(<AssistantMarkdownCell conversationId="owner" workspaceRoot="C:/project" cell={{
  kind: "assistant_markdown", id: "answer", messageId: "answer", markdownSource: content, phase: "final", copyable: true, createdAt: 1,
}} />);
beforeEach(() => {
  mocks.write.mockReset(); mocks.write.mockResolvedValue(); mocks.writeText.mockReset(); mocks.writeText.mockResolvedValue(); mocks.toast.mockClear();
  vi.stubGlobal("ClipboardItem", Item);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { write: mocks.write, writeText: mocks.writeText } });
  useAppStore.setState({ ...initial, appMode: "cowork", conversationId: "owner", workingDirectory: "C:/project" }, true);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); useAppStore.setState(initial, true); });

describe("rendered reply to actual clipboard API chain", () => {
  it("copies semantic plain text and rich HTML without code controls, line numbers or UI classes", async () => {
    const { container } = view(); fireEvent.click(screen.getByRole("button", { name: "复制回复" }));
    await waitFor(() => expect(mocks.write).toHaveBeenCalledTimes(1));
    const item = mocks.write.mock.calls[0][0][0] as Item;
    expect(item.types).toEqual(["text/plain", "text/html"]);
    const plain = await blobText(item.contents["text/plain"]), html = await blobText(item.contents["text/html"]);
    expect(plain).toContain("A bold reply with documentation.");
    expect(plain).toContain("const value = 1;\nconsole.log(value);");
    expect(plain).toContain("- [x]  Complete\n- [ ]  Pending");
    expect(plain).toContain("Name\tValue\nOne\tTwo");
    expect(plain).not.toMatch(/复制|已复制|插入|typescript|```|\*\*bold\*\*/);
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain('<a href="https://example.test/docs">documentation</a>');
    expect(html).toContain('<pre dir="ltr"><code>const value = 1;\nconsole.log(value);</code></pre>');
    expect(html).not.toMatch(/class=|style=|<button|aria-hidden|line-number/);
    expect(mocks.writeText).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: "已复制" })).toBeTruthy();
    expect(container.querySelector('[data-reply-action-icon="copied"]')).toBeTruthy();
  });

  it("copies the actual plan body while excluding its heading and original-output controls", async () => {
    view("<proposed_plan>\n## Change\n\n- Read source.\n- Preserve drafts.\n</proposed_plan>");
    fireEvent.click(screen.getByRole("button", { name: "复制回复" }));
    await waitFor(() => expect(mocks.write).toHaveBeenCalledTimes(1));
    const item = mocks.write.mock.calls[0][0][0] as Item;
    const plain = await blobText(item.contents["text/plain"]);
    expect(plain).toBe("Change\n\n- Read source.\n- Preserve drafts.");
    expect(plain).not.toMatch(/proposed_plan|方案|原始输出/);
  });

  it("uses Source plain capability branch when ClipboardItem is unavailable", async () => {
    vi.stubGlobal("ClipboardItem", undefined); view("A **bold** reply.");
    fireEvent.click(screen.getByRole("button", { name: "复制回复" }));
    await waitFor(() => expect(mocks.writeText).toHaveBeenCalledWith("A bold reply."));
    expect(mocks.write).not.toHaveBeenCalled();
  });

  it("keeps the real denied clipboard error and never claims a copy succeeded", async () => {
    mocks.write.mockRejectedValueOnce(new DOMException("Permission denied", "NotAllowedError")); view();
    fireEvent.click(screen.getByRole("button", { name: "复制回复" }));
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith("复制回复失败：NotAllowedError: Permission denied", "error", 3000));
    expect(screen.queryByRole("button", { name: "已复制" })).toBeNull();
    expect(mocks.writeText).not.toHaveBeenCalled();
  });

  it("preserves the legacy chat raw Markdown copy behavior", async () => {
    useAppStore.setState({ appMode: "chat" }); view("A **bold** reply.");
    fireEvent.click(screen.getByRole("button", { name: "复制回复" }));
    await waitFor(() => expect(mocks.writeText).toHaveBeenCalledWith("A **bold** reply."));
    expect(mocks.write).not.toHaveBeenCalled();
  });

  it("waits for the real write and prevents repeated copies while pending or showing success", async () => {
    let finish!: () => void;
    mocks.write.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
    view("Literal answer"); const copy = screen.getByRole("button", { name: "复制回复" });
    fireEvent.click(copy); fireEvent.click(copy);
    expect(mocks.write).toHaveBeenCalledTimes(1);
    expect(copy.getAttribute("aria-busy")).toBe("true"); expect(copy.getAttribute("aria-disabled")).toBe("true");
    await act(async () => finish());
    const succeeded = screen.getByRole("button", { name: "已复制" });
    expect(succeeded.getAttribute("aria-busy")).toBeNull();
    fireEvent.click(succeeded); expect(mocks.write).toHaveBeenCalledTimes(1);
  });

  it("keeps Source jGi default copied feedback for two seconds", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] }); view("Literal answer");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "复制回复" })));
    expect(screen.getByRole("button", { name: "已复制" })).toBeTruthy();
    act(() => vi.advanceTimersByTime(1999)); expect(screen.getByRole("button", { name: "已复制" })).toBeTruthy();
    act(() => vi.advanceTimersByTime(1)); expect(screen.getByRole("button", { name: "复制回复" })).toBeTruthy();
  });
});
