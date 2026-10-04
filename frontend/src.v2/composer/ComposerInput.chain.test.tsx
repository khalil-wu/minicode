/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  Object.defineProperty(globalThis, "matchMedia", { configurable: true, value: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  return { selected: "file:src/input.ts", send: vi.fn(() => true) };
});
vi.mock("../protocol/ws-outbox", async (original) => ({ ...await original<typeof import("../protocol/ws-outbox")>(), sendClientCommand: mocks.send }));
vi.mock("./MenuOverlay", () => ({ MenuOverlay: ({ open, kind, filter, onSelect }: { open: boolean; kind: string; filter: string; onSelect: (v: string) => void }) => open ? <button data-filter={filter} data-kind={kind} onClick={() => onSelect(mocks.selected)}>Select context</button> : null }));
vi.mock("./FooterRow", () => ({ FooterRow: () => null }));
vi.mock("./ActionChipRegion", () => ({ ContextChipRegion: () => null }));
vi.mock("./AttachmentStrip", () => ({ AttachmentStrip: () => null }));
import { useAppStore } from "../stores";
import { Composer } from "./Composer";
import { SideChatPanel } from "../panels/SideChatPanel";
import { appendPromptHistory } from "./prompt-history";

beforeEach(() => {
  localStorage.clear();
  mocks.selected = "file:src/input.ts";
  useAppStore.setState({ conversationId: "caret-owner", workingDirectory: "C:/original", draft: "", sideChats: {}, sideChatPendingContext: null,
    isConnected: false, isStreaming: false, currentModel: "gpt", sendShortcut: "enter", availableSkills: [{ name: "review", path: "C:/skills/review", description: "Review" }],
    selectedSkills: [], selectedMentions: [], attachments: [], slashPanelOpen: false, mentionPanelOpen: false, slashCommands: [], activeGoal: null,
    pendingApproval: null, approvalQueue: [], pendingAskUser: null, askUserQueue: [], pendingDiffReview: null, diffReviewQueue: [], quotedMessage: null });
});
afterEach(cleanup);

const typeAtCaret = (input: HTMLTextAreaElement, value: string, caret: number) => {
  fireEvent.change(input, { target: { value, selectionStart: caret, selectionEnd: caret } });
};

describe("actual composer input consumers", () => {
  it("reuses prompt history through the real input with a caret at the recalled end", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    appendPromptHistory("C:/original", "recalled prompt");
    render(<Composer />);
    const input = screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement;
    typeAtCaret(input, "current draft", 3);
    fireEvent.keyDown(input, { key: "r", ctrlKey: true });
    fireEvent.click(screen.getByRole("option", { name: "recalled prompt" }));
    expect(useAppStore.getState().draft).toBe("recalled prompt");
    expect(document.activeElement).toBe(input);
    expect(input.selectionStart).toBe("recalled prompt".length);
  });
  it("selects the mention at the caret, preserves following text and keeps the chosen line range", () => {
    render(<Composer />);
    const input = screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement;
    const token = "@input#L4-L8";
    typeAtCaret(input, `review ${token} after @later`, 7 + token.length);
    expect(screen.getByRole("button", { name: "Select context" }).getAttribute("data-filter")).toBe(token);
    fireEvent.click(screen.getByRole("button", { name: "Select context" }));
    expect(useAppStore.getState().draft).toBe("review  after @later");
    expect(useAppStore.getState().selectedMentions[0]).toMatchObject({ path: "src/input.ts#4-8", kind: "file" });
    expect(input.selectionStart).toBe(7);
    expect(document.activeElement).toBe(input);
  });

  it("selects a skill at the caret without erasing the suffix", () => {
    render(<Composer />);
    const input = screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement;
    typeAtCaret(input, "please $review carefully", 14);
    expect(screen.getByRole("button", { name: "Select context" }).getAttribute("data-kind")).toBe("skill");
    mocks.selected = `skill-path:${encodeURIComponent("C:/skills/review")}`;
    fireEvent.click(screen.getByRole("button", { name: "Select context" }));
    expect(useAppStore.getState().draft).toBe("please  carefully");
    expect(useAppStore.getState().selectedSkills[0].name).toBe("review");
    expect(input.selectionStart).toBe(7);
  });

  it("tracks caret movement and closes the picker for selected text and active composition", () => {
    render(<Composer />);
    const input = screen.getByRole("textbox", { name: "消息输入" }) as HTMLTextAreaElement;
    typeAtCaret(input, "@input suffix", 6);
    expect(screen.getByRole("button", { name: "Select context" })).toBeTruthy();
    input.setSelectionRange(0, 6); fireEvent.select(input);
    expect(screen.queryByRole("button", { name: "Select context" })).toBeNull();
    input.setSelectionRange(6, 6); fireEvent.select(input);
    expect(screen.getByRole("button", { name: "Select context" })).toBeTruthy();
    fireEvent.compositionStart(input);
    expect(screen.queryByRole("button", { name: "Select context" })).toBeNull();
    fireEvent.compositionEnd(input);
    expect(screen.getByRole("button", { name: "Select context" })).toBeTruthy();
  });

  it("keeps a side chat's caret selection, suffix and original workspace separate from the main draft", () => {
    useAppStore.setState({ draft: "main draft" });
    render(<SideChatPanel />);
    const input = screen.getByRole("textbox", { name: "侧边对话消息" }) as HTMLTextAreaElement;
    typeAtCaret(input, "side @input#4 after", 13);
    fireEvent.click(screen.getByRole("button", { name: "Select context" }));
    const side = Object.values(useAppStore.getState().sideChats)[0];
    expect(side.draft).toBe("side  after");
    expect(side.contextRefs[0]).toMatchObject({ path: "src/input.ts#4", workspaceRoot: "C:/original" });
    expect(useAppStore.getState().draft).toBe("main draft");
    expect(input.selectionStart).toBe(5);
  });

  it("opens an explicit side context picker and removes only its appended token", () => {
    render(<SideChatPanel />);
    const input = screen.getByRole("textbox", { name: "侧边对话消息" }) as HTMLTextAreaElement;
    typeAtCaret(input, "side note", 4);
    fireEvent.click(screen.getByRole("button", { name: "添加上下文" }));
    expect(input.selectionStart).toBe(input.value.length);
    fireEvent.click(screen.getByRole("button", { name: "Select context" }));
    expect(Object.values(useAppStore.getState().sideChats)[0].draft).toBe("side note ");
  });
});
