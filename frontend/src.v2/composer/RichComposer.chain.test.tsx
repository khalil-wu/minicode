/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useRef } from "react";
import { useAppStore } from "../stores";
import { adoptConversationDraftContent, newConversationDraftContext } from "../stores/conversation-drafts";
import { emptyComposerDraft } from "../stores/composer-drafts";
import { ComposerTextarea } from "./ComposerTextarea";
import { Composer } from "./Composer";
import { documentPosition, markdownOffset, parseComposerMarkdown, richComposerSchema as schema, richDocumentState, serializeComposerDocument } from "./rich-composer-document";
import { composerReferenceMarkdown, type InlineComposerReference } from "./composer-references";
import { pluginAssetResourceUrlWithToken } from "../protocol/api";
import { EditorView } from "prosemirror-view";
import * as contextActions from "../chat/contextReferenceActions";

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

const inlineReferences: InlineComposerReference[] = [
  { kind: "plugin", name: "电脑", configName: "computer-use@runtime", path: "plugin://computer-use@runtime", pluginPath: "C:/plugins/computer-use", iconVariant: "composer" },
  { kind: "skill", name: "playwright", displayName: "Playwright CLI Skill", path: "C:/skills/playwright/SKILL.md" },
  { kind: "url", name: "继续完成 MiniCode UI 复刻", path: "codex://threads/another-chat" },
  { kind: "file", name: "main.ts", path: "C:/rich-owner/main.ts" },
  { kind: "folder", name: "src", path: "C:/rich-owner/src" },
];
const referenceDocument = () => richDocumentState(schema.node("doc", null, schema.node("paragraph", null, [
  ...inlineReferences.flatMap(reference => [schema.nodes.context_mention.create({ reference }), schema.text(" ")]), schema.text("123"),
])));
const seedInlineReferences = () => {
  const document = referenceDocument();
  useAppStore.setState({ draft: document.markdown, richDraft: document,
    selectedSkills: inlineReferences.filter((reference): reference is Extract<InlineComposerReference, { kind: "skill" }> => reference.kind === "skill"),
    selectedMentions: inlineReferences.filter((reference): reference is Exclude<InlineComposerReference, { kind: "skill" }> => reference.kind !== "skill") });
  return document;
};

it("renders plugin, Skill, conversation, file and folder atoms beside ordinary text in the same actual paragraph", async () => {
  seedInlineReferences();
  render(<Composer />);
  const input = screen.getByRole("textbox");
  await waitFor(() => expect(input.querySelectorAll(".composer-inline-reference")).toHaveLength(5));
  expect(input.querySelectorAll("p")).toHaveLength(1);
  expect(input.querySelector("p")!.textContent).toBe("电脑 Playwright CLI Skill 继续完成 MiniCode UI 复刻 main.ts src 123");
  for (const reference of input.querySelectorAll("[data-composer-reference]")) expect(reference.querySelector("svg, img")).toBeTruthy();
  expect(input.querySelector(".brand-icon img[src]")?.getAttribute("src")).toBe(pluginAssetResourceUrlWithToken("C:/plugins/computer-use", "composer", undefined, "C:/rich-owner"));
  expect(input.querySelectorAll("[data-composer-reference][contenteditable=false]")).toHaveLength(5);
  expect(screen.queryByText("@computer-use@runtime")).toBeNull();
  expect(document.querySelector(".composer-context-prefix-row")).toBeNull();
});

it("removes the matching implicit plugin selection with native Backspace and restores it with the editor's real undo", async () => {
  seedInlineReferences();
  render(<Composer />);
  const input = screen.getByRole("textbox");
  act(() => {
    input.focus();
    const range = document.createRange(); range.selectNode(input.querySelector("[data-composer-reference]")!);
    window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });
  await waitFor(() => expect(useAppStore.getState().richDraft!.selection!.end).toBeGreaterThan(useAppStore.getState().richDraft!.selection!.start));
  fireEvent.keyDown(input, { key: "Backspace", keyCode: 8 });
  expect(useAppStore.getState().selectedMentions.some(reference => reference.kind === "plugin")).toBe(false);
  expect(useAppStore.getState().draft).not.toContain("plugin://computer-use@runtime");
  expect(useAppStore.getState().selectedSkills).toHaveLength(1);
  fireEvent.keyDown(input, { key: "z", ctrlKey: true });
  await waitFor(() => expect(useAppStore.getState().selectedMentions.find(reference => reference.kind === "plugin")).toMatchObject({ name: "电脑", configName: "computer-use@runtime", iconVariant: "composer" }));
  expect(useAppStore.getState().draft).toContain(composerReferenceMarkdown(inlineReferences[0]));
  act(() => {
    const skill = [...input.querySelectorAll("[data-composer-reference]")].find(element => JSON.parse(element.getAttribute("data-composer-reference")!).kind === "skill")!;
    const range = document.createRange(); range.selectNode(skill);
    window.getSelection()!.removeAllRanges(); window.getSelection()!.addRange(range);
    document.dispatchEvent(new Event("selectionchange"));
  });
  await waitFor(() => expect(useAppStore.getState().richDraft!.selection!.start).toBeGreaterThan(0));
  fireEvent.keyDown(input, { key: "Backspace", keyCode: 8 });
  expect(useAppStore.getState().selectedSkills).toEqual([]);
  expect(useAppStore.getState().selectedMentions.find(reference => reference.kind === "plugin")).toMatchObject({ name: "电脑" });
});

it("sends the original model paths and display metadata from the same rich document then clears only the submitted input", async () => {
  const saved = seedInlineReferences();
  render(<Composer />);
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter", keyCode: 13 });
  await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
  expect(mocks.send.mock.calls[0][0]).toMatchObject({ displayContent: saved.markdown, contextRefs: inlineReferences });
  expect(mocks.send.mock.calls[0][0].backendContent).toContain("plugin://computer-use@runtime");
  expect(useAppStore.getState()).toMatchObject({ draft: "", selectedSkills: [], selectedMentions: [] });
});

it("turns a pasted known chat deep link into its real title without replacing the surrounding draft", async () => {
  useAppStore.setState({ conversations: [{ id: "other-chat", title: "继续完成 MiniCode UI 复刻", workspaceRoot: "C:/other" }], draft: "before " });
  render(<Composer />);
  const input = screen.getByRole("textbox");
  paste(input, "minicode://conversation?conversation_id=other-chat");
  await waitFor(() => expect(input.textContent).toBe("before 继续完成 MiniCode UI 复刻 "));
  expect(useAppStore.getState().selectedMentions).toMatchObject([{ kind: "url", name: "继续完成 MiniCode UI 复刻", path: "minicode://conversation?conversation_id=other-chat" }]);
});

it.each(["code_block", "code_mark"])("keeps a pasted known chat URL literal inside an actual %s", async kind => {
  const url = "minicode://conversation?conversation_id=literal-code-chat";
  const body = kind === "code_block" ? schema.nodes.code_block.create({ params: "text" }, schema.text("before "))
    : schema.nodes.paragraph.create(null, schema.text("before ", [schema.marks.code.create()]));
  const saved = richDocumentState(schema.node("doc", null, body));
  useAppStore.setState({ draft: saved.markdown, richDraft: saved,
    conversations: [{ id: "literal-code-chat", title: "This label must not replace code" }] });
  render(<Composer />);
  const input = screen.getByRole("textbox");
  paste(input, url);
  await waitFor(() => expect(input.querySelector(kind === "code_block" ? "pre code" : "p")?.textContent).toBe("before " + url));
  expect(input.querySelector("[data-composer-reference]")).toBeNull();
  expect(input.querySelectorAll("p")).toHaveLength(kind === "code_block" ? 0 : 1);
  expect(useAppStore.getState().selectedMentions).toEqual([]);
});

it("passes the original side conversation owner through the real atom Ctrl-click handler", () => {
  const reference = { kind: "file" as const, name: "side.ts", path: "C:/side-owner/side.ts", workspaceRoot: "C:/side-owner" };
  const saved = richDocumentState(schema.node("doc", null, schema.node("paragraph", null, schema.nodes.context_mention.create({ reference }))));
  const open = vi.spyOn(contextActions, "openContextReference").mockImplementation(() => {});
  vi.spyOn(EditorView.prototype, "posAtCoords").mockReturnValue({ pos: 1, inside: 1 });
  render(<ComposerTextarea rich value={saved.markdown} richDocument={saved} documentKey="side-source" conversationId="side-source"
    workspaceRoot="C:/side-owner" onChange={() => {}} onSubmit={() => {}} />);
  const atom = screen.getByRole("textbox").querySelector("[data-composer-reference]")!;
  fireEvent.mouseDown(atom, { button: 0, clientX: 8, clientY: 8, ctrlKey: true });
  fireEvent.mouseUp(atom, { button: 0, clientX: 8, clientY: 8, ctrlKey: true });
  expect(open).toHaveBeenCalledWith(reference, "side-source");
  expect(useAppStore.getState()).toMatchObject({ conversationId: "rich-owner", workingDirectory: "C:/rich-owner" });
});

it("restores one selected-code atom from history with its original range and descriptor", async () => {
  const reference = { kind: "file" as const, name: "main.ts", path: "C:/rich-owner/main.ts", workspaceRoot: "C:/rich-owner",
    range: { startLineNumber: 4, startColumn: 2, endLineNumber: 8, endColumn: 1 }, text: "selected code" };
  useAppStore.setState({ draft: composerReferenceMarkdown(reference) + " review", richDraft: undefined, selectedMentions: [reference] });
  render(<Composer />);
  await waitFor(() => expect(screen.getByRole("textbox").querySelectorAll("[data-composer-reference]")).toHaveLength(1));
  expect(screen.getByRole("textbox").textContent).toBe("main.ts:4–7 review");
  expect(useAppStore.getState().selectedMentions).toEqual([reference]);
});

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
    (options.onSubmitted as () => void)();
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

it("clears the actual rich editor on local acceptance and keeps identical new typing through late completion", async () => {
  let finish!: (value: boolean) => void;
  mocks.send.mockImplementationOnce(options => {
    useAppStore.setState(state => ({ messages: [...state.messages, { id: options.userMessageId as string,
      role: "user", content: options.displayContent as string, timestamp: 1, artifacts: [] }] }));
    (options.onSubmitted as () => void)();
    return new Promise<boolean>(resolve => { finish = resolve; });
  });
  render(<Composer />);
  const input = screen.getByRole("textbox", { name: "消息输入" });
  paste(input, "**同样的新内容**");
  await waitFor(() => expect(useAppStore.getState().richDraft).toBeTruthy());
  const submitted = useAppStore.getState().draft;
  fireEvent.keyDown(input, { key: "Enter", code: "Enter", ctrlKey: true });
  await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
  expect(useAppStore.getState().draft).toBe("");
  expect(input.textContent).toBe("");
  paste(input, "**同样的新内容**");
  await waitFor(() => expect(useAppStore.getState().draft).toBe(submitted));
  await act(async () => finish(true));
  expect(useAppStore.getState().draft).toBe(submitted);
  expect(input.querySelector("strong")?.textContent).toBe("同样的新内容");
});

it.each([false, true])("Enter clears the real rich editor while first-conversation creation is pending, newer input=%s", async newer => {
  const materialize = useAppStore.getState().materializeConversationDraft;
  const context = newConversationDraftContext(useAppStore.getState(), "C:/rich-owner", true);
  let finishCreation!: () => void;
  const creation = new Promise<void>(resolve => { finishCreation = resolve; });
  const owner = "created-rich-owner";
  useAppStore.setState({ conversationId: null, activeConversationDraftId: context.id, conversations: [], messages: [],
    conversationDrafts: { [context.id]: context }, conversationWorkbenchStates: {},
    workspaceComposerDrafts: { [context.id]: emptyComposerDraft() }, draft: "", richDraft: undefined,
    materializeConversationDraft: async id => {
      await creation;
      adoptConversationDraftContent(id, owner);
      useAppStore.setState({ conversations: [{ id: owner, title: "first rich prompt", workspaceRoot: "C:/rich-owner" }] });
      useAppStore.getState().applyConversationSwitched({ conversationId: owner });
      return owner;
    } });
  mocks.send.mockImplementationOnce(async options => { (options.onSubmitted as () => void)(); return true; });
  try {
    render(<Composer />);
    const input = screen.getByRole("textbox", { name: "消息输入" });
    paste(input, "**first rich prompt**");
    fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 13 });
    expect(useAppStore.getState().draft).toBe("");
    expect(input.textContent).toBe("");
    expect(mocks.send).not.toHaveBeenCalled();
    expect(useAppStore.getState().conversationDrafts[context.id].pendingUserMessage?.content).toBe("**first rich prompt**");
    if (newer) paste(input, "**new unsent prompt**");
    await act(async () => finishCreation());
    await waitFor(() => expect(mocks.send).toHaveBeenCalledOnce());
    expect(mocks.send).toHaveBeenCalledWith(expect.objectContaining({ displayContent: "**first rich prompt**", conversationId: owner }));
    expect(useAppStore.getState().draft).toBe(newer ? "**new unsent prompt**" : "");
    expect(input.textContent).toBe(newer ? "new unsent prompt" : "");
    expect(useAppStore.getState().conversationWorkbenchStates[owner]?.draft).toBe(newer ? "**new unsent prompt**" : "");
  } finally {
    useAppStore.setState({ materializeConversationDraft: materialize });
  }
});
