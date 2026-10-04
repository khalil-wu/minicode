// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { useAppStore } from "../../stores";

const runtime = vi.hoisted(() => {
  Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }) });
  return { clipboard: vi.fn(), toast: vi.fn() };
});
vi.mock("../../overlays/ToastContainer", () => ({ pushToast: runtime.toast }));
const markdown = (code: string) => `\`\`\`ts\n${code}\n\`\`\``;
beforeEach(() => {
  runtime.clipboard.mockReset().mockResolvedValue(undefined);
  runtime.toast.mockClear();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: runtime.clipboard } });
  useAppStore.setState({ appMode: "code", workingDirectory: "/project", editorTabs: [{ id: "editable", path: "src/main.ts", content: "original", original: "original", loading: false }], activeTabPath: "src/main.ts" });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("actual Markdown code action consumers", () => {
  it("reports a clipboard denial and retries the exact code without line numbers or blank-line padding", async () => {
    const code = "const value = 1;\n\nconsole.log(value);";
    runtime.clipboard.mockRejectedValueOnce(new Error("Clipboard denied"));
    render(<MarkdownRenderer content={markdown(code)} />);
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    await waitFor(() => expect(runtime.toast).toHaveBeenCalledWith("复制代码失败：Clipboard denied", "error", 3000));
    expect(screen.getByRole("button", { name: "复制" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    await screen.findByRole("button", { name: "已复制" });
    expect(runtime.clipboard.mock.calls).toEqual([[code], [code]]);
  });
  it("does not label new code as copied when an earlier clipboard action completes late", async () => {
    let finish!: () => void;
    runtime.clipboard.mockImplementationOnce(() => new Promise<void>((resolve) => { finish = resolve; }));
    const view = render(<MarkdownRenderer content={markdown("const OLD = 1;")} />);
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    view.rerender(<MarkdownRenderer content={markdown("const NEW = 2;")} />);
    await act(async () => finish());
    expect(screen.getByRole("button", { name: "复制" })).toBeTruthy();
    expect(runtime.clipboard).toHaveBeenCalledWith("const OLD = 1;");
  });
  it("inserts only through the mounted editor authority and retains the exact code payload", () => {
    const code = "const value = 1;\n\nconsole.log(value);";
    const delivered: string[] = [];
    const handler = (event: Event) => { const detail = (event as CustomEvent<{ text: string; handled: boolean }>).detail; delivered.push(detail.text); detail.handled = true; };
    window.addEventListener("editor:insert-text", handler);
    const view = render(<MarkdownRenderer content={markdown(code)} />);
    fireEvent.click(screen.getByRole("button", { name: "插入" }));
    expect(delivered).toEqual([code]);
    expect(useAppStore.getState().editorTabs[0].content).toBe("original");
    expect(runtime.toast).toHaveBeenCalledWith("已插入 src/main.ts", "success", 1200);
    view.unmount(); window.removeEventListener("editor:insert-text", handler);
  });
  it("does not mutate the store before a real editor has mounted", () => {
    render(<MarkdownRenderer content={markdown("const value = 1;")} />);
    fireEvent.click(screen.getByRole("button", { name: "插入" }));
    expect(useAppStore.getState().editorTabs[0].content).toBe("original");
    expect(runtime.toast).toHaveBeenCalledWith("编辑器尚未就绪，请稍后重试。", "warning", 1800);
  });
  it.each(["image.png", "file.pdf", "loading.ts", "readonly.ts"])("does not expose an insert action for %s", (path) => {
    useAppStore.setState({ editorTabs: [{ id: "uneditable", path, content: "", original: "", loading: path === "loading.ts", readOnly: path === "readonly.ts" }], activeTabPath: path });
    render(<MarkdownRenderer content={markdown("const value = 1;")} />);
    expect(screen.queryByRole("button", { name: "插入" })).toBeNull();
  });
  it("preserves ordinary memory citation markup and fenced samples beside valid citation metadata", () => {
    const literal = "<minicode-memory-citation>literal</minicode-memory-citation>";
    const fenced = "<minicode-memory-citation>[1] code sample</minicode-memory-citation>";
    const view = render(<MarkdownRenderer content={`Known [1].\n\n${literal}\n\n\`\`\`text\n${fenced}\n\`\`\``} citations={[{ source: "https://source.test", range: [0, 0] }]} />);
    expect(view.container.textContent).toContain("Known .");
    expect(view.container.textContent).toContain(literal);
    expect(view.container.querySelector("pre")?.textContent).toContain(fenced);
  });
});
