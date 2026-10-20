/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useRef } from "react";
import { useAppStore } from "../stores";
import { ComposerTextarea } from "./ComposerTextarea";
import { Composer } from "./Composer";
import { documentPosition, markdownOffset, parseComposerMarkdown, richComposerSchema as schema, richDocumentState, serializeComposerDocument } from "./rich-composer-document";

const mocks = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true,
    value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  return { send: vi.fn(async (_options: Record<string, unknown>) => true), toast: vi.fn(), upload: vi.fn() };
});
vi.mock("../chat/sendChatMessage", () => ({ sendChatMessage: mocks.send }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: mocks.toast }));
vi.mock("./contextPayload", () => ({ buildContextPayload: async () => "",
  buildContextNativeAttachments: async () => ({ attachments: [], attachmentRefs: [], notes: "" }) }));
vi.mock("./uploads", () => ({ uploadComposerFiles: mocks.upload, acceptAttachmentConversationOwner: vi.fn(() => true) }));
vi.mock("./ActionChipRegion", () => ({ ContextChipRegion: () => null }));
vi.mock("./AttachmentStrip", () => ({ AttachmentStrip: () => null }));
vi.mock("./ProjectScopePicker", () => ({ ProjectScopePicker: () => null }));
vi.mock("./FooterRow", () => ({ FooterRow: ({ onSend, sendState }: { onSend: () => void; sendState: string }) => <button disabled={sendState === "disabled"} onClick={onSend}>Send rich message</button> }));
vi.mock("./MenuOverlay", () => ({ MenuOverlay: () => null }));

const submit = vi.fn();
const fence = String.fromCharCode(96).repeat(3);
const paste = (target: HTMLElement, text: string, html = "") => fireEvent.paste(target, { clipboardData: {
  items: [], files: [], types: html ? ["text/plain", "text/html"] : ["text/plain"],
  getData: (type: string) => type === "text/html" ? html : text,
} });
const typeIntoParagraph = (input: HTMLElement, text: string) => {
  const paragraph = input.querySelector("p")!;
  paragraph.textContent = text;
  const range = document.createRange(); range.setStart(paragraph.firstChild!, text.length); range.collapse(true);
  window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
  fireEvent.input(input, { inputType: "insertText", data: text });
};
function Harness() {
  const draft = useAppStore(state => state.draft);
  const document = useAppStore(state => state.richDraft);
  const ref = useRef<HTMLButtonElement>(null);
  return <><button ref={ref} onClick={() => void useAppStore.getState().setComposerPlainTextMode(!useAppStore.getState().composerPlainTextMode)}>Mode</button>
    <ComposerTextarea rich value={draft} richDocument={document} onChange={(value, _selection, rich) => useAppStore.getState().setDraft(value, rich)}
      onSubmit={submit} onDropFiles={vi.fn()} /></>;
}
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  const rect = new DOMRect(0, 0, 600, 80);
  Object.defineProperty(Range.prototype, "getClientRects", { configurable: true, value: () => [rect] });
  Object.defineProperty(Range.prototype, "getBoundingClientRect", { configurable: true, value: () => rect });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(rect);
  vi.spyOn(window, "scrollBy").mockImplementation(() => {});
  useAppStore.setState({ composerDraftsReady: true, composerPlainTextMode: false,
    conversationId: "rich-owner", workingDirectory: "C:/rich-owner", appMode: "cowork", draft: "", richDraft: undefined,
    isConnected: true, isStreaming: false, currentModel: "gpt", runtimeSession: null, messages: [],
    attachments: [], selectedMentions: [], selectedSkills: [], quotedMessage: null,
    slashPanelOpen: false, mentionPanelOpen: false, slashCommands: [], activeGoal: null, goalMode: false,
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [],
    sendShortcut: "enter", conversationWorkbenchStates: {}, workspaceComposerDrafts: {}, sideChats: {} });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); });

it("round-trips real code, task lists, tables and links while keeping remote images out of the editor network", () => {
  const source = [fence + "ts", "const value = 1;", fence, "", "- [x] done", "- [ ] next", "",
    "| Key | Value |", "| --- | --- |", "| α | 你好 |", "", "[reference](https://example.test/path)", "", "![diagram](https://example.test/private.png)"].join("\n");
  const document = parseComposerMarkdown(source);
  const markdown = serializeComposerDocument(document);
  expect(markdown).toContain(fence + "ts");
  expect(markdown).toContain("* [x] done");
  expect(markdown).toContain("| α | 你好 |");
  expect(markdown).toContain("https://example.test/path");
  expect(parseComposerMarkdown(markdown).toJSON()).toEqual(document.toJSON());
  const saved = richDocumentState(document);
  useAppStore.setState({ draft: saved.markdown, richDraft: saved });
  render(<Harness />);
  const editor = screen.getByRole("textbox");
  expect(editor.querySelector("img[src], img[srcset]")).toBeNull();
  expect(editor.querySelector("[data-rich-image]")?.getAttribute("data-rich-image")).toBe("https://example.test/private.png");
  expect(screen.getByRole("textbox").querySelectorAll('input[type="checkbox"]')).toHaveLength(2);
});

it("maps a mention caret to the actual Markdown inside marked list text", () => {
  const document = parseComposerMarkdown("- **review @src/main.ts**");
  let caret = 0;
  document.descendants((node, position) => { if (node.isText && node.text!.includes("@src")) caret = position + node.text!.length; });
  const offset = markdownOffset(document, caret);
  expect(serializeComposerDocument(document).slice(0, offset)).toContain("@src/main.ts");
  const position = documentPosition(document, offset);
  expect(markdownOffset(document, position)).toBe(offset);
});

it("pastes rich blocks through the real editor and preserves a structured owned draft", async () => {
  render(<Harness />);
  const input = screen.getByRole("textbox");
  paste(input, ["- item", "", fence + "js", "console.log(1)", fence].join("\n"));
  await waitFor(() => expect(input.querySelector("ul li")).toBeTruthy());
  expect(input.querySelector("pre code")!.textContent).toBe("console.log(1)");
  expect(useAppStore.getState().richDraft?.document.type).toBe("doc");
  expect(useAppStore.getState().draft).toContain(fence + "js");
});

it("converts typed list and code fence boundaries before the send key", async () => {
  const listEditor = render(<Harness />);
  let input = screen.getByRole("textbox");
  typeIntoParagraph(input, "- ");
  await waitFor(() => expect(input.querySelector("ul")).toBeTruthy());
  listEditor.unmount();
  act(() => useAppStore.getState().setDraft(""));
  render(<Harness />);
  input = screen.getByRole("textbox");
  typeIntoParagraph(input, fence + "python");
  await waitFor(() => expect(useAppStore.getState().richDraft?.document.content?.[0].type).toBe("paragraph"));
  fireEvent.keyDown(input, { key: "Enter", keyCode: 13 });
  await waitFor(() => expect(input.querySelector("pre")).toBeTruthy());
  expect(submit).not.toHaveBeenCalled();
  expect(useAppStore.getState().draft).toContain(fence + "python");
});

it("keeps URL paste literal in the sent Markdown while rendering an actual rich link", async () => {
  render(<Harness />);
  const input = screen.getByRole("textbox");
  paste(input, "https://example.test/a_b?q=x");
  await waitFor(() => expect(input.querySelector("a")).toBeTruthy());
  expect(useAppStore.getState().draft).toBe("https://example.test/a_b?q=x");
});

it("keeps IME Enter out of submit and does not replace the editor for unrelated draft context updates", async () => {
  render(<Harness />);
  const input = screen.getByRole("textbox");
  paste(input, "你好");
  fireEvent.compositionStart(input);
  fireEvent.keyDown(input, { key: "Enter", isComposing: true, keyCode: 229 });
  act(() => useAppStore.setState({ quotedMessage: { id: "q", role: "assistant", content: "Keep composition" } }));
  expect(screen.getByRole("textbox")).toBe(input);
  expect(submit).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input);
  // Safari's native commit Enter is consumed once after compositionend.
  fireEvent.keyDown(input, { key: "Enter", keyCode: 13 });
  expect(submit).not.toHaveBeenCalled();
  await act(async () => { await Promise.resolve(); });
  fireEvent.keyDown(input, { key: "Enter", keyCode: 13 });
  expect(submit).toHaveBeenCalledOnce();
});

it("uploads a file dropped on the real rich main input once without bubbling into a second upload", () => {
  render(<Composer />);
  const input = screen.getByRole("textbox", { name: "消息输入" });
  expect(input.getAttribute("data-rich-composer")).toBe("");
  const file = new File(["real native drop"], "native-drop.txt", { type: "text/plain" });
  expect(fireEvent.drop(input, { dataTransfer: { files: [file], items: [], types: ["Files"], getData: () => "" } })).toBe(false);
  expect(mocks.upload).toHaveBeenCalledTimes(1);
  expect(mocks.upload).toHaveBeenCalledWith([file]);
});

it("switches literal mode without losing Markdown, owned rich state, or focus from the settings control", async () => {
  render(<Harness />);
  const input = screen.getByRole("textbox");
  paste(input, "**keep this**");
  const original = useAppStore.getState().richDraft;
  const markdown = useAppStore.getState().draft;
  const mode = screen.getByRole("button", { name: "Mode" });
  mode.focus();
  fireEvent.click(mode);
  const literal = await screen.findByRole("textbox");
  expect(literal).toBe(input);
  expect(literal.getAttribute("contenteditable")).toBe("true");
  expect(literal.getAttribute("data-plain")).toBe("true");
  expect(literal.textContent).toBe(markdown);
  expect(literal.querySelector("strong")).toBeNull();
  expect(useAppStore.getState().richDraft?.plainTextMode).toBe(true);
  expect(document.activeElement).toBe(mode);
  fireEvent.click(mode);
  await waitFor(() => expect(input.getAttribute("data-plain")).toBe("false"));
  expect(screen.getByRole("textbox")).toBe(input);
  expect(screen.getByRole("textbox").querySelector("strong")!.textContent).toBe("keep this");
  expect(useAppStore.getState().richDraft?.document).toEqual(original?.document);
  expect(document.activeElement).toBe(mode);
});

it("keeps ordinary undo and redo across both mode switches on the same focused editor", async () => {
  render(<Harness />);
  const input = screen.getByRole("textbox");
  paste(input, "Undo survives");
  await act(() => useAppStore.getState().setComposerPlainTextMode(true));
  await act(() => useAppStore.getState().setComposerPlainTextMode(false));
  expect(screen.getByRole("textbox")).toBe(input);
  expect(document.activeElement).toBe(input);
  fireEvent.keyDown(input, { key: "z", ctrlKey: true });
  expect(useAppStore.getState().draft).toBe("");
  fireEvent.keyDown(input, { key: "z", ctrlKey: true, shiftKey: true });
  expect(useAppStore.getState().draft).toBe("Undo survives");
});

it("preserves code-body undo when converting code to literal fence paragraphs", async () => {
  const source = [fence + "ts", "first", "second", fence].join("\n");
  const saved = richDocumentState(parseComposerMarkdown(source));
  useAppStore.setState({ draft: saved.markdown, richDraft: saved });
  render(<Harness />);
  const input = screen.getByRole("textbox");
  paste(input, "X");
  expect(input.querySelector("pre code")!.textContent).toBe("first\nsecondX");
  await act(() => useAppStore.getState().setComposerPlainTextMode(true));
  expect(input.querySelector("pre")).toBeNull();
  expect(useAppStore.getState().draft).toBe(source.replace("second", "secondX"));
  fireEvent.keyDown(input, { key: "z", ctrlKey: true });
  expect(useAppStore.getState().draft).toBe(source);
  fireEvent.keyDown(input, { key: "z", ctrlKey: true, shiftKey: true });
  expect(useAppStore.getState().draft).toBe(source.replace("second", "secondX"));
});

it("keeps Markdown, URL and HTML clipboard content literal in plain mode", async () => {
  useAppStore.setState({ composerPlainTextMode: true });
  render(<Harness />);
  const input = screen.getByRole("textbox");
  const text = ["**literal**", "- item", fence + "ts", "code", fence, "https://example.test/a_b"].join("\n");
  paste(input, text, '<p><strong>literal</strong></p><ul><li>item</li></ul><a href="https://example.test/a_b">link</a>');
  expect(useAppStore.getState().draft).toBe(text);
  expect(useAppStore.getState().richDraft?.plainTextMode).toBe(true);
  expect(input.querySelector("strong, em, ul, pre, a")).toBeNull();
  expect(fireEvent.keyDown(input, { key: "b", ctrlKey: true })).toBe(false);
  expect(fireEvent.keyDown(input, { key: "i", ctrlKey: true })).toBe(false);
  expect(input.querySelector("strong, em")).toBeNull();
});

it("uses literal newline and submit rules for a code fence in plain mode", () => {
  useAppStore.setState({ composerPlainTextMode: true, sendShortcut: "mod-enter" });
  render(<Harness />);
  const input = screen.getByRole("textbox");
  paste(input, fence + "ts");
  fireEvent.keyDown(input, { key: "Enter" });
  expect(useAppStore.getState().draft).toBe(fence + "ts\n");
  expect(input.querySelector("pre")).toBeNull();
  expect(submit).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: "Enter", ctrlKey: true });
  expect(submit).toHaveBeenCalledOnce();
});

it("defers a mode change until the actual IME text is committed without replacing its view or stealing focus", async () => {
  render(<Harness />);
  const input = screen.getByRole("textbox");
  paste(input, "你");
  const textNode = input.querySelector("p")!.firstChild!;
  fireEvent.compositionStart(input);
  textNode.textContent = "你好";
  const range = document.createRange(); range.setStart(textNode, 2); range.collapse(true);
  window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
  fireEvent.input(input, { inputType: "insertCompositionText", data: "好", isComposing: true });
  await act(() => useAppStore.getState().setComposerPlainTextMode(true));
  expect(input.getAttribute("data-plain")).toBe("false");
  expect(input.querySelector("p")!.firstChild).toBe(textNode);
  fireEvent.keyDown(input, { key: "Enter", isComposing: true, keyCode: 229 });
  expect(submit).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input, { data: "好" });
  await waitFor(() => expect(input.getAttribute("data-plain")).toBe("true"));
  expect(useAppStore.getState().draft).toBe("你好");
  expect(screen.getByRole("textbox")).toBe(input);
  expect(document.activeElement).toBe(input);
});

it.each(["code", "task"])("does not submit an empty formatted %s draft", kind => {
  const document = schema.node("doc", null, kind === "code" ? schema.node("code_block", { params: "ts" })
    : schema.node("bullet_list", null, schema.node("list_item", { checked: false }, schema.node("paragraph"))));
  const saved = richDocumentState(document);
  useAppStore.setState({ draft: saved.markdown, richDraft: saved });
  render(<Composer />);
  const button = screen.getByRole("button", { name: "Send rich message" });
  expect(button).toHaveProperty("disabled", true);
  fireEvent.click(button);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "消息输入" }), { key: "Enter", ctrlKey: true });
  expect(mocks.send).not.toHaveBeenCalled();
});

it("uses the actual main turn for the alternate follow-up keyboard action", async () => {
  useAppStore.setState({ followUpBehavior: "queue", isStreaming: true,
    messages: [{ id: "live-main-answer", turnId: "real-main-turn", role: "assistant", content: "working", artifacts: [], timestamp: 1, isStreaming: true }] });
  render(<Composer />);
  paste(screen.getByRole("textbox", { name: "消息输入" }), "change this task");
  fireEvent.keyDown(screen.getByRole("textbox", { name: "消息输入" }), { key: "Enter", ctrlKey: true });
  await waitFor(() => expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({ busyBehavior: "steer", expectedTurnId: "real-main-turn" })));
});

it("sends serialized Markdown through the existing Composer and restores the rich draft after a real rejected admission", async () => {
  mocks.send.mockImplementationOnce(async options => {
    const id = options.userMessageId as string;
    useAppStore.setState(state => ({ messages: [...state.messages, { id, role: "user", content: options.displayContent as string, timestamp: 1, artifacts: [] }] }));
    (options.onReceived as () => void)();
    (options.onRejected as () => void)();
    return false;
  });
  render(<Composer />);
  paste(screen.getByRole("textbox", { name: "消息输入" }), [fence + "js", "const n = 3;", fence].join("\n"));
  await waitFor(() => expect(useAppStore.getState().richDraft).toBeTruthy());
  const saved = useAppStore.getState().richDraft;
  const markdown = useAppStore.getState().draft;
  fireEvent.click(screen.getByRole("button", { name: "Send rich message" }));
  await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
  await waitFor(() => expect(useAppStore.getState().draft).toBe(markdown));
  expect(mocks.send.mock.calls[0][0]).toMatchObject({ displayContent: markdown, backendContent: markdown });
  expect(useAppStore.getState().richDraft?.document).toEqual(saved?.document);
  expect(screen.getByRole("textbox", { name: "消息输入" }).querySelector("pre")).toBeTruthy();
});
