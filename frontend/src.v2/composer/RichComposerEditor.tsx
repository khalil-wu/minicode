import { forwardRef, useImperativeHandle, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { EditorState, Plugin, TextSelection, type Command } from "prosemirror-state";
import { EditorView } from "prosemirror-view";
import { Fragment, Slice } from "prosemirror-model";
import { baseKeymap, chainCommands, createParagraphNear, exitCode, liftEmptyBlock, newlineInCode, splitBlock, toggleMark } from "prosemirror-commands";
import { history, redo, undo } from "prosemirror-history";
import { keymap } from "prosemirror-keymap";
import { InputRule, inputRules, textblockTypeInputRule, undoInputRule, wrappingInputRule } from "prosemirror-inputrules";
import { liftListItem, sinkListItem, splitListItem } from "prosemirror-schema-list";
import { goToNextCell, tableEditing } from "prosemirror-tables";
import type { ComposerSelection } from "./inputSelection";
import { convertComposerReferenceLinks, documentPosition, literalComposerDocument, markdownOffset, parseComposerMarkdown, restoreComposerDocument,
  richComposerSchema as schema, richDocumentState, serializeComposerDocument, type RichComposerDocument } from "./rich-composer-document";
import { openPreferredWebLink } from "../lib/open-preferred-target";
import { registerComposerEditHistory } from "./composer-edit-history";
import { composerReferenceKey, composerReferenceLabel, conversationIdFromReference, type ComposerReferenceInsertion, type InlineComposerReference } from "./composer-references";
import { ComposerReferenceIcon } from "./ComposerReferenceIcon";
import { openContextReference } from "../chat/contextReferenceActions";
import { useAppStore } from "../stores";
import "prosemirror-view/style/prosemirror.css";
import "./RichComposerEditor.css";

export interface ComposerInputHandle {
  focus(options?: FocusOptions): void;
  selectionStart: number;
  selectionEnd: number;
  setSelectionRange(start: number, end: number): void;
}
interface Props {
  value: string;
  document?: RichComposerDocument;
  disabled: boolean;
  placeholder: string;
  ariaLabel: string;
  enterBehavior: string;
  plainTextMode: boolean;
  initialSelection: ComposerSelection;
  initialFocus: boolean;
  references?: InlineComposerReference[];
  referenceInsertion?: ComposerReferenceInsertion;
  workspaceRoot?: string;
  conversationId?: string;
  onFocusChange(focused: boolean): void;
  onChange(value: string, selection: ComposerSelection, document: RichComposerDocument): void;
  onSelectionChange(value: string, selection: ComposerSelection): void;
  onKeyDown(event: KeyboardEvent): void;
  onPaste(event: ClipboardEvent): void;
  onDrop(event: DragEvent): void;
  onCompositionChange(composing: boolean): void;
}

const splitList: Command = (state, dispatch) => splitListItem(schema.nodes.list_item,
  state.selection.$from.depth > 1 && state.selection.$from.node(-1).attrs.checked !== null ? { checked: false } : undefined)(state, dispatch);
const lineBreak = chainCommands(newlineInCode, splitList,
  createParagraphNear, liftEmptyBlock, splitBlock);
const codeArrow = (direction: -1 | 1): Command => (state, dispatch) => {
  const { $from, empty } = state.selection;
  if (!empty || $from.parent.type !== schema.nodes.code_block) return false;
  if (direction === 1 && $from.parentOffset === $from.parent.content.size) return exitCode(state, dispatch);
  if (direction === -1 && $from.parentOffset === 0 && $from.index(-1) === 0) {
    const start = $from.before();
    const transaction = state.tr.insert(start, schema.node("paragraph"));
    dispatch?.(transaction.setSelection(TextSelection.create(transaction.doc, start + 1)).scrollIntoView());
    return true;
  }
  return false;
};
const closeCodeFence: Command = (state, dispatch) => {
  const { $from, empty } = state.selection;
  if (!empty || $from.parent.type !== schema.nodes.code_block || $from.parentOffset !== $from.parent.content.size) return false;
  const text = $from.parent.textContent;
  if (!/(?:^|\n)\x60{3}$/.test(text)) return false;
  const code = schema.nodes.code_block.create($from.parent.attrs,
    text.replace(/(?:^|\n)\x60{3}$/, "") ? schema.text(text.replace(/(?:^|\n)\x60{3}$/, "")) : null);
  const start = $from.before();
  const transaction = state.tr.replaceWith(start, $from.after(), [code, schema.node("paragraph")]);
  dispatch?.(transaction.setSelection(TextSelection.near(transaction.doc.resolve(start + code.nodeSize + 1))).scrollIntoView());
  return true;
};
const openCodeFence: Command = (state, dispatch) => {
  const { $from, empty } = state.selection;
  if (!empty || $from.parent.type !== schema.nodes.paragraph) return false;
  const fence = /^\x60{3}([\w+-]*)$/.exec($from.parent.textContent);
  if (!fence) return false;
  dispatch?.(state.tr.delete($from.start(), $from.end()).setBlockType($from.start(), $from.start(), schema.nodes.code_block, { params: fence[1] }));
  return true;
};
const openTable: Command = (state, dispatch) => {
  const { $from, empty } = state.selection;
  if (!empty || $from.depth !== 1 || $from.parent.type !== schema.nodes.paragraph || $from.index(0) === 0
    || !/^[\s:|-]+$/.test($from.parent.textContent) || !$from.parent.textContent.includes("---")) return false;
  const previous = state.doc.child($from.index(0) - 1);
  if (previous.type !== schema.nodes.paragraph || !previous.textContent.includes("|")) return false;
  const parsed = parseComposerMarkdown(previous.textContent + "\n" + $from.parent.textContent);
  if (parsed.firstChild?.type !== schema.nodes.table) return false;
  const header = parsed.firstChild.firstChild!;
  const cells = Array.from({ length: header.childCount }, () => schema.node("table_cell", null, schema.node("paragraph")));
  const table = schema.node("table", null, [header, schema.node("table_row", null, cells)]);
  const start = $from.before() - previous.nodeSize;
  const transaction = state.tr.replaceWith(start, $from.after(), table);
  dispatch?.(transaction.setSelection(TextSelection.near(transaction.doc.resolve(start + header.nodeSize + 4))).scrollIntoView());
  return true;
};

function plugins(placeholder: () => string, plainTextMode: boolean, editorHistory: Plugin) {
  return [
    ...plainTextMode ? [] : [inputRules({ rules: [
      wrappingInputRule(/^\s*([-+*])\s$/, schema.nodes.bullet_list),
      wrappingInputRule(/^(\d+)\.\s$/, schema.nodes.ordered_list, match => ({ order: Number(match[1]) })),
      wrappingInputRule(/^\s*>\s$/, schema.nodes.blockquote),
      textblockTypeInputRule(/^(#{1,6})\s$/, schema.nodes.heading, match => ({ level: match[1].length })),
      textblockTypeInputRule(/^\x60{3}([\w+-]*)\s$/, schema.nodes.code_block, match => ({ params: match[1] })),
      new InputRule(/(https?:\/\/[^\s<>]+)\s$/, (state, match, start, end) =>
        state.tr.insertText(" ", end).addMark(start, end, schema.marks.link.create({ href: match[1], automatic: true }))),
      new InputRule(/^\[([ xX])\]\s$/, (state, match, start, end) => {
        const parent = state.selection.$from;
        if (parent.depth < 2 || parent.node(-1).type !== schema.nodes.list_item) return null;
        return state.tr.delete(start, end).setNodeMarkup(parent.before(-1), undefined, { checked: match[1] !== " " });
      }),
      ...(["strong", "em", "code", "strike"] as const).map(name => {
        const patterns = { strong: /(\*\*)([^*\n]+)\*\*$/, em: /(\*)([^*\n]+)\*$/, code: /(\x60)([^\x60\n]+)\x60$/, strike: /(~~)([^~\n]+)~~$/ };
        return new InputRule(patterns[name], (state, match, start, end) => {
          const text = match[2];
          return state.tr.insertText(text, start, end).addMark(start, start + text.length, schema.marks[name].create());
        });
      }),
    ] })],
    editorHistory,
    keymap({ "Mod-z": undo, "Mod-Shift-z": redo, "Mod-y": redo }),
    ...plainTextMode ? [keymap({ Enter: splitBlock, "Shift-Enter": splitBlock, "Alt-Enter": splitBlock })] : [keymap({ Backspace: undoInputRule,
      Enter: lineBreak, "Shift-Enter": lineBreak, "Alt-Enter": lineBreak,
      ArrowDown: codeArrow(1), ArrowUp: codeArrow(-1),
      "Mod-a": (state, dispatch) => {
        const { $from, $to } = state.selection;
        if ($from.parent.type !== schema.nodes.code_block || !$from.sameParent($to)
          || state.selection.from === $from.start() && state.selection.to === $from.end()) return false;
        dispatch?.(state.tr.setSelection(TextSelection.create(state.doc, $from.start(), $from.end()))); return true;
      },
      Tab: chainCommands(goToNextCell(1), sinkListItem(schema.nodes.list_item), (state, dispatch) => {
        if (state.selection.$from.parent.type !== schema.nodes.code_block) return false;
        dispatch?.(state.tr.insertText("  ")); return true;
      }), "Shift-Tab": chainCommands(goToNextCell(-1), liftListItem(schema.nodes.list_item)),
      "Mod-b": toggleMark(schema.marks.strong), "Mod-i": toggleMark(schema.marks.em), "Mod-e": toggleMark(schema.marks.code) })],
    keymap(baseKeymap),
    ...plainTextMode ? [] : [tableEditing()],
    new Plugin({ props: { attributes: (state): Record<string, string> => state.doc.textContent.length === 0 && state.doc.childCount === 1
      ? { "data-placeholder": placeholder() } : {} } }),
  ];
}

function convertMode(view: EditorView, plainTextMode: boolean) {
  const value = serializeComposerDocument(view.state.doc, !plainTextMode);
  const document = plainTextMode ? literalComposerDocument(value) : parseComposerMarkdown(value);
  const transaction = view.state.tr;
  // Converting to plain text retains the code body in history while adding literal fences.
  if (plainTextMode) {
    view.state.doc.descendants((node, position) => {
      if (node.type !== schema.nodes.code_block) return;
      const lines = serializeComposerDocument(schema.node("doc", null, node)).split("\n");
      const start = transaction.mapping.map(position);
      transaction.setNodeMarkup(start, schema.nodes.paragraph);
      let boundary = start + node.textContent.length + 1;
      for (const line of lines.slice(2, -1).reverse()) {
        boundary -= line.length + 1;
        transaction.delete(boundary, boundary + 1).split(boundary);
      }
      transaction.insert(start, schema.node("paragraph", null, schema.text(lines[0])));
      transaction.insert(transaction.mapping.map(position + node.nodeSize), schema.node("paragraph", null, schema.text(lines.at(-1)!)));
    });
    transaction.removeMark(0, transaction.doc.content.size);
  }
  if (!transaction.doc.eq(document)) transaction.replaceWith(0, transaction.doc.content.size, document.content);
  return transaction.setMeta("addToHistory", false);
}

export const RichComposerEditor = forwardRef<ComposerInputHandle, Props>((props, ref) => {
  const host = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView>();
  const latest = useRef(props); latest.current = props;
  const committed = useRef(props.value);
  const committedDocument = useRef(props.document);
  const composing = useRef(false);
  const plainTextMode = useRef(props.plainTextMode);
  const editorHistory = useRef<Plugin>();
  const compositionEndTimer = useRef<ReturnType<typeof setTimeout>>();
  const referenceViews = useRef(new Map<HTMLElement, { id: number; reference: InlineComposerReference }>());
  const referenceViewId = useRef(0);
  const [, updateReferenceViews] = useState(0);
  const previousReferences = useRef<InlineComposerReference[]>([]);
  const readSelection = (): ComposerSelection => {
    const view = viewRef.current!;
    return { start: markdownOffset(view.state.doc, view.state.selection.from, plainTextMode.current),
      end: markdownOffset(view.state.doc, view.state.selection.to, plainTextMode.current), composing: composing.current };
  };
  const publishDocument = () => {
    const document = { ...richDocumentState(viewRef.current!.state.doc, plainTextMode.current), selection: readSelection() };
    committed.current = document.markdown;
    committedDocument.current = document;
    latest.current.onChange(document.markdown, readSelection(), document);
  };
  const synchronizeView = () => {
    const view = viewRef.current!, current = latest.current;
    if (view.composing || composing.current) return;
    // Empty drafts omit their document in the store; that echo is not a replace.
    if (current.value !== committed.current || current.document && current.document.document !== committedDocument.current?.document) {
      const document = restoreComposerDocument(current.value, current.document, plainTextMode.current);
      const selection = current.document?.selection;
      view.updateState(EditorState.create({ schema, doc: document,
        plugins: plugins(() => latest.current.placeholder, plainTextMode.current, editorHistory.current!),
        selection: selection ? TextSelection.between(document.resolve(documentPosition(document, selection.start, plainTextMode.current)),
          document.resolve(documentPosition(document, selection.end, plainTextMode.current))) : TextSelection.atEnd(document) }));
      committed.current = current.value;
      committedDocument.current = current.document;
    }
    if (current.plainTextMode !== plainTextMode.current) {
      const selection = readSelection();
      view.updateState(view.state.reconfigure({ plugins: plugins(() => latest.current.placeholder, current.plainTextMode, editorHistory.current!) }));
      const transaction = convertMode(view, current.plainTextMode);
      plainTextMode.current = current.plainTextMode;
      transaction.setSelection(TextSelection.between(
        transaction.doc.resolve(documentPosition(transaction.doc, selection.start, plainTextMode.current)),
        transaction.doc.resolve(documentPosition(transaction.doc, selection.end, plainTextMode.current))));
      view.dispatch(transaction);
      if (!transaction.docChanged) publishDocument();
    }
    const references = current.references ?? [];
    const removed = new Set(previousReferences.current.map(composerReferenceKey));
    const byKey = new Map(references.map(reference => [composerReferenceKey(reference), reference]));
    for (const key of byKey.keys()) removed.delete(key);
    const transaction = view.state.tr;
    const present = new Set<string>();
    const nodes: Array<{ node: import("prosemirror-model").Node; position: number }> = [];
    view.state.doc.descendants((node, position) => { if (node.type.name === "context_mention") nodes.push({ node, position }); });
    for (const { node, position } of nodes.reverse()) {
      const key = composerReferenceKey(node.attrs.reference);
      if (removed.has(key)) transaction.delete(position, position + node.nodeSize);
      else {
        present.add(key);
        const reference = byKey.get(key);
        if (reference && JSON.stringify(reference) !== JSON.stringify(node.attrs.reference)) transaction.setNodeMarkup(position, undefined, { reference });
      }
    }
    for (const reference of references) if (!present.has(composerReferenceKey(reference))) {
      transaction.replaceSelectionWith(schema.nodes.context_mention.create({ reference })).insertText(" ");
      present.add(composerReferenceKey(reference));
    }
    previousReferences.current = references;
    if (transaction.docChanged) view.dispatch(transaction);
    view.setProps({ editable: () => !latest.current.disabled, attributes: { ...view.props.attributes as Record<string, string>,
      "aria-label": current.ariaLabel, "aria-disabled": String(current.disabled), "data-enter-behavior": current.enterBehavior,
      "data-plain": String(plainTextMode.current) } });
  };
  useImperativeHandle(ref, () => ({
    focus: () => viewRef.current!.focus(),
    get selectionStart() { return readSelection().start; },
    get selectionEnd() { return readSelection().end; },
    setSelectionRange(start, end) {
      const view = viewRef.current!;
      view.dispatch(view.state.tr.setSelection(TextSelection.between(
        view.state.doc.resolve(documentPosition(view.state.doc, start, plainTextMode.current)),
        view.state.doc.resolve(documentPosition(view.state.doc, end, plainTextMode.current)))));
    },
  }), []);
  useLayoutEffect(() => {
    let disposed = false;
    const initial = restoreComposerDocument(props.value, props.document, props.plainTextMode);
    const initialSelection = TextSelection.between(initial.resolve(documentPosition(initial, props.initialSelection.start, props.plainTextMode)),
      initial.resolve(documentPosition(initial, props.initialSelection.end, props.plainTextMode)));
    editorHistory.current = history();
    const view = new EditorView(host.current!, {
      state: EditorState.create({ schema, doc: initial, selection: initialSelection,
        plugins: plugins(() => latest.current.placeholder, props.plainTextMode, editorHistory.current) }),
      editable: () => !latest.current.disabled,
      attributes: { role: "textbox", "aria-multiline": "true", "aria-label": props.ariaLabel,
        "data-composer-input": "", "data-enter-behavior": props.enterBehavior, "data-rich-composer": "",
        "data-plain": String(props.plainTextMode),
        class: "composer-textarea composer-rich-text ProseMirror", spellcheck: "true" },
      dispatchTransaction(transaction) {
        view.updateState(view.state.apply(transaction));
        if (transaction.docChanged) publishDocument();
        else if (transaction.selectionSet) latest.current.onSelectionChange(committed.current, readSelection());
      },
      handleKeyDown(view, event) {
        if (event.isComposing || view.composing || composing.current || event.keyCode === 229) return false;
        if (!plainTextMode.current && event.key === "Enter" && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey
          && (closeCodeFence(view.state, view.dispatch) || openCodeFence(view.state, view.dispatch) || openTable(view.state, view.dispatch))) return true;
        if (!plainTextMode.current && event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey
          && latest.current.enterBehavior === "enter" && exitCode(view.state, view.dispatch)) return true;
        latest.current.onKeyDown(event);
        if (plainTextMode.current && (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey
          && ["b", "i", "e"].includes(event.key.toLowerCase())) {
          event.preventDefault(); return true;
        }
        return event.defaultPrevented;
      },
      handlePaste(view, event) {
        latest.current.onPaste(event);
        if (event.defaultPrevented) return true;
        const text = event.clipboardData!.getData("text/plain");
        const conversationId = conversationIdFromReference(text.trim());
        const inCode = view.state.selection.$from.parent.type.spec.code || view.state.selection.$from.marks().some(mark => mark.type.spec.code);
        if (conversationId && !inCode) {
          const conversation = useAppStore.getState().conversations.find(item => item.id === conversationId);
          const reference = { kind: "url" as const, name: conversation?.title || text.trim(), path: text.trim() };
          view.dispatch(view.state.tr.replaceSelectionWith(schema.nodes.context_mention.create({ reference })).insertText(" ").scrollIntoView());
          return true;
        }
        if (plainTextMode.current) {
          if (text) view.dispatch(view.state.tr.replaceSelection(Slice.maxOpen(literalComposerDocument(text.replace(/\r\n?/g, "\n")).content))
            .scrollIntoView().setMeta("paste", true).setMeta("uiEvent", "paste"));
          return true;
        }
        if (!view.state.selection.empty && /^(https?:\/\/|mailto:)\S+$/i.test(text)) {
          view.dispatch(view.state.tr.addMark(view.state.selection.from, view.state.selection.to, schema.marks.link.create({ href: text })));
          return true;
        }
        if (view.state.selection.empty && /^(https?:\/\/|mailto:)\S+$/i.test(text)) {
          const { from } = view.state.selection;
          view.dispatch(view.state.tr.insertText(text).addMark(from, from + text.length, schema.marks.link.create({ href: text, automatic: true })));
          return true;
        }
        return false;
      },
      transformPasted: slice => new Slice(Fragment.fromArray(slice.content.content.map(convertComposerReferenceLinks)), slice.openStart, slice.openEnd),
      clipboardTextParser(text, context, plain) {
        if (context.parent.type.spec.code) return new Slice(Fragment.from(schema.text(text)), 0, 0);
        return Slice.maxOpen((plain || plainTextMode.current ? literalComposerDocument(text) : parseComposerMarkdown(text)).content);
      },
      clipboardTextSerializer: slice => slice.content.textBetween(0, slice.content.size, plainTextMode.current ? "\n" : "\n\n"),
      handleDOMEvents: {
        focus() { latest.current.onFocusChange(true); return false; },
        blur() { latest.current.onFocusChange(false); return false; },
        drop(_view, event) { latest.current.onDrop(event); return event.defaultPrevented; },
        compositionstart() { composing.current = true; latest.current.onCompositionChange(true); return false; },
        compositionend() {
          composing.current = false; latest.current.onCompositionChange(false);
          // ProseMirror flushes the final composition records after this handler.
          clearTimeout(compositionEndTimer.current);
          compositionEndTimer.current = setTimeout(synchronizeView, 0);
          return false;
        },
        click(_view, event) {
          const anchor = (event.target as Element).closest<HTMLAnchorElement>("a[href]");
          if (!anchor) return false;
          event.preventDefault();
          if ((event.ctrlKey || event.metaKey) && /^(https?:|mailto:)/i.test(anchor.href)) void openPreferredWebLink(anchor.href);
          return false;
        },
      },
      handleClickOn(view, _position, node, nodePosition, event) {
        if (node.type === schema.nodes.context_mention && (event.ctrlKey || event.metaKey)) {
          event.preventDefault(); openContextReference(node.attrs.reference, latest.current.conversationId); return true;
        }
        if (plainTextMode.current || node.type !== schema.nodes.list_item || node.attrs.checked === null || !(event.target as Element).matches('input[type="checkbox"]')) return false;
        event.preventDefault();
        if (!latest.current.disabled) view.dispatch(view.state.tr.setNodeMarkup(nodePosition, undefined, { ...node.attrs, checked: !node.attrs.checked }));
        return true;
      },
      nodeViews: {
        context_mention(node) {
          const dom = document.createElement("span");
          dom.className = "composer-inline-reference";
          dom.setAttribute("contenteditable", "false");
          const entry = { id: referenceViewId.current++, reference: node.attrs.reference as InlineComposerReference };
          const update = () => {
            dom.setAttribute("data-composer-reference", JSON.stringify(entry.reference));
            referenceViews.current.set(dom, entry);
            if (!disposed) updateReferenceViews(value => value + 1);
          };
          update();
          return { dom, ignoreMutation: () => true,
            update(next) {
              if (next.type !== schema.nodes.context_mention) return false;
              entry.reference = next.attrs.reference; update(); return true;
            },
            destroy() { referenceViews.current.delete(dom); if (!disposed) updateReferenceViews(value => value + 1); },
          };
        },
      },
    });
    viewRef.current = view;
    const unregisterHistory = registerComposerEditHistory(view.dom, { hasText: () => !!view.state.doc.textContent,
      undo: () => undo(view.state, view.dispatch), redo: () => redo(view.state, view.dispatch) });
    const initialState = richDocumentState(view.state.doc, plainTextMode.current);
    committed.current = initialState.markdown;
    committedDocument.current = initialState;
    synchronizeView();
    if ((props.value || props.document) && committedDocument.current === initialState) latest.current.onChange(initialState.markdown, readSelection(), initialState);
    if (props.initialFocus && !view.dom.closest('[hidden], [aria-hidden="true"]')) view.focus();
    return () => { disposed = true; unregisterHistory(); clearTimeout(compositionEndTimer.current); view.destroy(); viewRef.current = undefined; };
  }, []);
  useLayoutEffect(() => {
    synchronizeView();
  }, [props.value, props.document, props.disabled, props.ariaLabel, props.enterBehavior, props.placeholder, props.plainTextMode, props.references]);
  useLayoutEffect(() => {
    if (!props.referenceInsertion) return;
    const view = viewRef.current!, { reference, selection } = props.referenceInsertion;
    const transaction = view.state.tr.setSelection(TextSelection.between(
      view.state.doc.resolve(documentPosition(view.state.doc, selection.start, plainTextMode.current)),
      view.state.doc.resolve(documentPosition(view.state.doc, selection.end, plainTextMode.current))));
    view.dispatch(transaction.replaceSelectionWith(schema.nodes.context_mention.create({ reference })).insertText(" ").scrollIntoView());
    view.focus();
  }, [props.referenceInsertion]);
  return <><div ref={host} className="composer-rich-host" />{[...referenceViews.current].map(([dom, { id, reference }]) => createPortal(<>
    <span className="composer-inline-reference-icon"><ComposerReferenceIcon reference={reference} workspaceRoot={props.workspaceRoot ?? ""} /></span>
    <span className="composer-inline-reference-label" title={reference.kind === "skill" ? reference.description || reference.path : reference.path}>{composerReferenceLabel(reference)}</span>
  </>, dom, String(id)))}</>;
});
RichComposerEditor.displayName = "RichComposerEditor";
