import { useEffect, useLayoutEffect, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Annotation, Compartment, EditorSelection, EditorState, StateEffect, StateField, Text, Transaction, type Range } from "@codemirror/state";
import { Decoration, EditorView, keymap, placeholder, WidgetType, type DecorationSet } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab, invertedEffects, isolateHistory, redo, undo } from "@codemirror/commands";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { ensureSyntaxTree, syntaxTree } from "@codemirror/language";
import { gotoLine, openSearchPanel, search, searchKeymap } from "@codemirror/search";
import { decodeMarkdownFragment, markdownHeadingSlug } from "../lib/markdown";
import type { EditorTextSurface } from "./editor-text-surface";
import "./LiveMarkdownEditor.css";

interface LiveMarkdownEditorProps {
  documentId: string;
  sessions: Map<string, MarkdownEditorSession>;
  value: string;
  readOnly: boolean;
  textScale: number;
  wordWrap?: boolean;
  components: Components;
  urlTransform: (url: string) => string;
  resolveUrl: (url: string) => string;
  scopeId: string;
  onChange: (value: string) => void;
  onMount: (editor: EditorTextSurface) => void;
}

export type MarkdownEditorSession = {
  state: EditorState;
  scrollTop: number;
  liveConfig: Compartment;
  editConfig: Compartment;
  wrapConfig: Compartment;
  eventConfig: Compartment;
  lineConfig: Compartment;
};

type SourceRange = { from: number; to: number };
type LineRange = { startLineNumber: number; startColumn: number; endLineNumber: number; endColumn: number };
type ReviewDecoration = { range: LineRange; options: { className?: string } };

const focusChanged = StateEffect.define<boolean>();
const reviewChanged = StateEffect.define<ReviewDecoration[]>();
const externalSource = Annotation.define<boolean>();
const sourceEolChanged = StateEffect.define<string>();
const sourceLineSeparator = (source: string): string => source.includes("\r\n") ? "\r\n" : source.includes("\r") ? "\r" : "\n";
const externalSourceUpdate = (state: EditorState, source: string, lineConfig: Compartment) => {
  const separator = sourceLineSeparator(source);
  return {
    // Text bypasses the old state's string splitter. Reconfiguring the facet
    // alone would still split the incoming text with the previous separator.
    changes: { from: 0, to: state.doc.length, insert: Text.of(source.split(separator)) },
    effects: lineConfig.reconfigure(EditorState.lineSeparator.of(separator)),
    annotations: [externalSource.of(true), Transaction.addToHistory.of(false)],
  };
};
const focused = StateField.define<boolean>({
  create: () => false,
  update: (value, transaction) => transaction.effects.reduce((next, effect) => effect.is(focusChanged) ? effect.value : next, value),
});
const reviewLines = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(value, transaction) {
    const update = transaction.effects.find((effect): effect is StateEffect<ReviewDecoration[]> => effect.is(reviewChanged));
    if (!update) return value.map(transaction.changes);
    const decorations: Range<Decoration>[] = [];
    for (const item of update.value) {
      for (let line = item.range.startLineNumber; line <= Math.min(item.range.endLineNumber, transaction.state.doc.lines); line += 1) {
        decorations.push(Decoration.line({ class: item.options.className }).range(transaction.state.doc.line(line).from));
      }
    }
    return Decoration.set(decorations, true);
  },
  provide: (field) => EditorView.decorations.from(field),
});

const sourceOffset = (doc: Text, line: number, column = 1): number => {
  const target = doc.line(Math.min(Math.max(line, 1), doc.lines));
  return Math.min(target.from + Math.max(column - 1, 0), target.to);
};

const sourceRange = (doc: Text, range: unknown): SourceRange => {
  if ("from" in (range as SourceRange)) return range as SourceRange;
  const location = range as LineRange;
  return {
    from: sourceOffset(doc, location.startLineNumber, location.startColumn),
    to: sourceOffset(doc, location.endLineNumber, location.endColumn),
  };
};

const markdownNodeText = (node: ReturnType<typeof syntaxTree>["topNode"], doc: Text): string => {
  let text = "";
  let offset = node.from;
  for (let child = node.firstChild; child; child = child.nextSibling) {
    text += doc.sliceString(offset, child.from);
    if (!["HeaderMark", "EmphasisMark", "StrikethroughMark", "CodeMark", "LinkMark", "URL", "LinkTitle"].includes(child.name)) {
      text += markdownNodeText(child, doc);
    }
    offset = child.to;
  }
  return `${text}${doc.sliceString(offset, node.to)}`;
};

function MarkdownBlock({ source, props, measure }: { source: string; props: LiveMarkdownEditorProps; measure: () => void }) {
  useLayoutEffect(measure, [source, measure]);
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={props.components} urlTransform={props.urlTransform}>{source}</ReactMarkdown>;
}

class RenderedMarkdown extends WidgetType {
  private root: Root | null = null;
  constructor(readonly source: string, readonly from: number, readonly props: LiveMarkdownEditorProps, readonly block: boolean) { super(); }
  eq(other: RenderedMarkdown) {
    return this.source === other.source && this.from === other.from
      && this.props.components === other.props.components
      && this.props.resolveUrl === other.props.resolveUrl
      && this.props.urlTransform === other.props.urlTransform
      && this.props.readOnly === other.props.readOnly;
  }
  toDOM(view: EditorView) {
    const element: HTMLElement = document.createElement(this.block ? "div" : "span");
    element.className = `md-prose live-md-rendered ${this.block ? "live-md-block" : "live-md-inline-image"}`;
    element.setAttribute("data-markdown-source", String(this.from));
    element.addEventListener("mousedown", (event) => {
      if (this.props.readOnly || event.button !== 0 || (event.target as HTMLElement).closest("a")) return;
      event.preventDefault();
      // Establish DOM focus before revealing a block. Focusing while the
      // target is still a replacement widget lets the stale DOM caret replace
      // its source position during CodeMirror's focus/selection synchronization.
      view.focus();
      view.dispatch({ selection: { anchor: this.from }, effects: focusChanged.of(true) });
    });
    element.addEventListener("load", () => view.requestMeasure(), true);
    this.root = createRoot(element);
    this.root.render(<MarkdownBlock source={this.source} props={this.props} measure={() => view.requestMeasure()} />);
    return element;
  }
  destroy() { const root = this.root; queueMicrotask(() => root?.unmount()); }
  ignoreEvent() { return true; }
}

class MarkdownBullet extends WidgetType {
  toDOM() {
    const element = document.createElement("span");
    element.className = "live-md-bullet";
    element.textContent = "•";
    return element;
  }
  ignoreEvent() { return false; }
}

class MarkdownCheckbox extends WidgetType {
  constructor(readonly checked: boolean, readonly from: number, readonly readOnly: boolean) { super(); }
  eq(other: MarkdownCheckbox) { return this.checked === other.checked && this.from === other.from && this.readOnly === other.readOnly; }
  toDOM(view: EditorView) {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = this.checked;
    input.disabled = this.readOnly;
    input.className = "live-md-checkbox";
    input.setAttribute("aria-label", this.checked ? "标记为未完成" : "标记为完成");
    input.addEventListener("mousedown", (event) => event.stopPropagation());
    input.addEventListener("change", () => view.dispatch({ changes: { from: this.from, to: this.from + 3, insert: input.checked ? "[x]" : "[ ]" } }));
    return input;
  }
  ignoreEvent() { return true; }
}

function liveMarkdown(props: LiveMarkdownEditorProps) {
  return StateField.define<DecorationSet>({
    create: (state) => buildMarkdownDecorations(state, props),
    update: (_value, transaction) => buildMarkdownDecorations(transaction.state, props),
    provide: (field) => EditorView.decorations.from(field),
  });
}

function buildMarkdownDecorations(state: EditorState, props: LiveMarkdownEditorProps): DecorationSet {
  const decorations: Range<Decoration>[] = [];
  const tree = ensureSyntaxTree(state, state.doc.length, 100) ?? syntaxTree(state);
  const editing = (from: number, to: number) => !props.readOnly && state.field(focused)
    && state.selection.ranges.some((range) => range.from <= to && range.to >= from);
  const hide = (from: number, to: number) => { if (to > from) decorations.push(Decoration.replace({}).range(from, to)); };
  const lineClass = (from: number, to: number, className: string) => {
    for (let line = state.doc.lineAt(from).number; line <= state.doc.lineAt(to).number; line += 1) {
      decorations.push(Decoration.line({ class: className }).range(state.doc.line(line).from));
    }
  };
  const headingOrdinals = new Map<string, number>();
  tree.iterate({
    enter(node) {
      const name = node.name;
      const text = state.doc.sliceString(node.from, node.to);
      const active = editing(node.from, node.to);
      if (/^(ATX|Setext)Heading[1-6]$/.test(name)) {
        const level = Number(name.at(-1));
        const label = markdownNodeText(node.node, state.doc).trim();
        const slug = markdownHeadingSlug(label);
        const ordinal = (headingOrdinals.get(slug) ?? 0) + 1;
        headingOrdinals.set(slug, ordinal);
        const id = `${props.scopeId}-${slug}${ordinal > 1 ? `-${ordinal}` : ""}`;
        decorations.push(Decoration.line({ class: `live-md-heading live-md-h${level}`, attributes: { id, role: "heading", "aria-level": String(level), "aria-label": label } }).range(state.doc.lineAt(node.from).from));
        if (!active) {
          for (let child = node.node.firstChild; child; child = child.nextSibling) {
            if (child.name === "HeaderMark") hide(child.from, child.to + (state.doc.sliceString(child.to, child.to + 1) === " " ? 1 : 0));
          }
        }
      } else if (name === "Table" || name === "FencedCode" || name === "CodeBlock" || name === "HorizontalRule") {
        if (!active) {
          decorations.push(Decoration.replace({ widget: new RenderedMarkdown(text, node.from, props, true), block: true }).range(node.from, node.to));
          return false;
        }
        lineClass(node.from, node.to, "live-md-source-block");
      } else if (name === "Image") {
        if (!active) {
          decorations.push(Decoration.replace({ widget: new RenderedMarkdown(text, node.from, props, false) }).range(node.from, node.to));
          return false;
        }
      } else if (["StrongEmphasis", "Emphasis", "Strikethrough", "InlineCode"].includes(name)) {
        const classes: Record<string, string> = { StrongEmphasis: "live-md-strong", Emphasis: "live-md-emphasis", Strikethrough: "live-md-strike", InlineCode: "live-md-code" };
        decorations.push(Decoration.mark({ class: classes[name] }).range(node.from, node.to));
        if (!active) {
          for (let child = node.node.firstChild; child; child = child.nextSibling) {
            if (["EmphasisMark", "StrikethroughMark", "CodeMark"].includes(child.name)) hide(child.from, child.to);
          }
        }
      } else if (name === "Link" || name === "Autolink") {
        const urlNode = node.node.getChild("URL");
        if (urlNode && !active) {
          const raw = props.urlTransform(state.doc.sliceString(urlNode.from, urlNode.to));
          decorations.push(Decoration.mark({ tagName: "a", class: "live-md-link", attributes: { href: raw ? props.resolveUrl(raw) : "", target: raw.startsWith("#") ? "_self" : "_blank", rel: "noreferrer" } }).range(node.from, node.to));
          if (name === "Link") {
            const marks = node.node.getChildren("LinkMark");
            hide(marks[0].from, marks[0].to);
            hide(marks[1].from, node.to);
          } else {
            for (const mark of node.node.getChildren("LinkMark")) hide(mark.from, mark.to);
          }
          return false;
        }
      } else if (name === "Blockquote") {
        lineClass(node.from, node.to, "live-md-quote");
      } else if (name === "QuoteMark" && !editing(node.node.parent!.from, node.node.parent!.to)) {
        hide(node.from, node.to + (state.doc.sliceString(node.to, node.to + 1) === " " ? 1 : 0));
      } else if (name === "ListMark" && !editing(node.node.parent!.from, node.node.parent!.to) && /^[*+-]$/.test(text)) {
        decorations.push(Decoration.replace({ widget: new MarkdownBullet() }).range(node.from, node.to));
      } else if (name === "TaskMarker" && !active) {
        decorations.push(Decoration.replace({ widget: new MarkdownCheckbox(text.toLowerCase() === "[x]", node.from, props.readOnly) }).range(node.from, node.to));
      }
    },
  });
  return Decoration.set(decorations, true);
}

export const LiveMarkdownEditor = (props: LiveMarkdownEditorProps) => {
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const propsRef = useRef(props);
  propsRef.current = props;
  const liveConfig = useRef(new Compartment());
  const editConfig = useRef(new Compartment());
  const wrapConfig = useRef(new Compartment());
  const eventConfig = useRef(new Compartment());
  const lineConfig = useRef(new Compartment());

  useEffect(() => {
    const selectionListeners = new Set<() => void>();
    const contentListeners = new Set<() => void>();
    const positionListeners = new Set<(event: { position: { lineNumber: number; column: number } }) => void>();
    const disposeListeners = new Set<() => void>();
    const notifyPosition = (view: EditorView) => {
      const position = view.state.selection.main.head;
      const line = view.state.doc.lineAt(position);
      for (const listener of positionListeners) listener({ position: { lineNumber: line.number, column: position - line.from + 1 } });
    };
    const events = [
        EditorView.updateListener.of((update) => {
          if (update.docChanged) {
            if (update.transactions.some((transaction) => transaction.docChanged && !transaction.annotation(externalSource))) {
              propsRef.current.onChange(update.state.sliceDoc());
            }
            for (const listener of contentListeners) listener();
          }
          if (update.selectionSet || update.docChanged) {
            notifyPosition(update.view);
            for (const listener of selectionListeners) listener();
          }
        }),
        EditorView.domEventHandlers({
          focus: (_event, view) => { view.dispatch({ effects: focusChanged.of(true) }); return false; },
          blur: (_event, view) => { if (!view.composing) view.dispatch({ effects: focusChanged.of(false) }); return false; },
          click: (event, view) => {
            const anchor = (event.target as HTMLElement).closest<HTMLAnchorElement>("a.live-md-link");
            if (!anchor) return false;
            const href = anchor.getAttribute("href") ?? "";
            if (href.startsWith("#")) {
              event.preventDefault();
              const slug = markdownHeadingSlug(decodeMarkdownFragment(href.replace(`#${propsRef.current.scopeId}-`, "")));
              let target: number | null = null;
              const ordinals = new Map<string, number>();
              syntaxTree(view.state).iterate({ enter(node) {
                if (!/^(ATX|Setext)Heading[1-6]$/.test(node.name)) return;
                const label = markdownNodeText(node.node, view.state.doc).trim();
                const base = markdownHeadingSlug(label);
                const ordinal = (ordinals.get(base) ?? 0) + 1;
                ordinals.set(base, ordinal);
                if (`${base}${ordinal > 1 ? `-${ordinal}` : ""}` === slug && target === null) target = node.from;
              } });
              if (target === null) return true;
              view.dispatch({ selection: { anchor: target }, effects: EditorView.scrollIntoView(target, { y: "center" }) });
              view.focus();
            } else if (!(event.ctrlKey || event.metaKey || propsRef.current.readOnly)) {
              event.preventDefault();
            }
            return true;
          },
        }),
    ];
    const createState = () => EditorState.create({
      doc: props.value,
      extensions: [
        markdown({ base: markdownLanguage }), history(), search(), focused, reviewLines,
        lineConfig.current.of(EditorState.lineSeparator.of(sourceLineSeparator(props.value))),
        EditorState.transactionExtender.of((transaction) => {
          const eol = transaction.effects.find((effect) => effect.is(sourceEolChanged));
          return eol ? { effects: lineConfig.current.reconfigure(EditorState.lineSeparator.of(eol.value)) } : null;
        }),
        invertedEffects.of((transaction) => transaction.effects.filter((effect) => effect.is(sourceEolChanged))
          .map(() => sourceEolChanged.of(transaction.startState.lineBreak))),
        keymap.of([...searchKeymap, ...historyKeymap, ...defaultKeymap, indentWithTab]),
        placeholder("开始写 Markdown…"),
        liveConfig.current.of(liveMarkdown(props)),
        editConfig.current.of([EditorState.readOnly.of(props.readOnly), EditorView.editable.of(!props.readOnly)]),
        wrapConfig.current.of(props.wordWrap === false ? [] : EditorView.lineWrapping),
        eventConfig.current.of(events),
        EditorView.contentAttributes.of({ "aria-label": "Markdown 实时编辑器", "aria-multiline": "true", spellcheck: "false" }),
      ],
    });
    const cached = props.sessions.get(props.documentId);
    if (cached) {
      liveConfig.current = cached.liveConfig;
      editConfig.current = cached.editConfig;
      wrapConfig.current = cached.wrapConfig;
      eventConfig.current = cached.eventConfig;
      lineConfig.current = cached.lineConfig;
    }
    const view = new EditorView({ state: cached?.state ?? createState(), parent: hostRef.current! });
    viewRef.current = view;
    if (cached) {
      // Each saved document owns its compartments, including callbacks. Mounting
      // a different document restores history without retaining the old view.
      const effects = [
        focusChanged.of(false), liveConfig.current.reconfigure(liveMarkdown(props)), eventConfig.current.reconfigure(events),
        editConfig.current.reconfigure([EditorState.readOnly.of(props.readOnly), EditorView.editable.of(!props.readOnly)]),
        wrapConfig.current.reconfigure(props.wordWrap === false ? [] : EditorView.lineWrapping),
      ];
      if (view.state.sliceDoc() !== props.value) {
        const sourceUpdate = externalSourceUpdate(view.state, props.value, lineConfig.current);
        view.dispatch({ ...sourceUpdate, effects: [...effects, sourceUpdate.effects] });
      } else {
        view.dispatch({ effects });
      }
      view.scrollDOM.scrollTop = cached.scrollTop;
    }
    const scroll = (line: number, column = 1) => view.dispatch({ effects: EditorView.scrollIntoView(sourceOffset(view.state.doc, line, column), { y: "center" }) });
    const handle: EditorTextSurface = {
      getSelection: () => {
        const { from, to } = view.state.selection.main;
        const start = view.state.doc.lineAt(from);
        const end = view.state.doc.lineAt(to);
        return { startLineNumber: start.number, startColumn: from - start.from + 1, endLineNumber: end.number, endColumn: to - end.from + 1 };
      },
      getPosition: () => {
        const head = view.state.selection.main.head;
        const line = view.state.doc.lineAt(head);
        return { lineNumber: line.number, column: head - line.from + 1 };
      },
      getModel: () => ({
        uri: { path: `/${props.documentId}` },
        getValueInRange: (range) => { const { from, to } = sourceRange(view.state.doc, range); return view.state.sliceDoc(from, to); },
        getLineCount: () => view.state.doc.lines,
        getLineMaxColumn: (line) => view.state.doc.line(line).length + 1,
        getEOL: () => view.state.lineBreak,
      }),
      getAction: (id) => {
        const commands: Record<string, (editor: EditorView) => boolean> = { "actions.find": openSearchPanel, "editor.action.startFindReplaceAction": openSearchPanel, "editor.action.gotoLine": gotoLine, undo, redo };
        return commands[id] ? { run: () => { commands[id](view); } } : null;
      },
      focus: () => view.focus(),
      executeEdits: (_source, edits) => {
        const eol = edits.find((edit) => edit.eol)?.eol;
        const changes = edits.map((edit) => ({ ...sourceRange(view.state.doc, edit.range), insert: Text.of(edit.text.split(/\r\n|\r|\n/)) }));
        view.dispatch({ changes, effects: eol && eol !== view.state.lineBreak ? sourceEolChanged.of(eol) : [], annotations: isolateHistory.of("full") });
      },
      setPosition: ({ lineNumber, column }) => view.dispatch({ selection: EditorSelection.cursor(sourceOffset(view.state.doc, lineNumber, column)) }),
      setSelection: (range) => view.dispatch({ selection: EditorSelection.range(
        sourceOffset(view.state.doc, range.startLineNumber, range.startColumn),
        sourceOffset(view.state.doc, range.endLineNumber, range.endColumn),
      ) }),
      revealLineInCenter: (line) => scroll(line),
      revealPositionInCenter: ({ lineNumber, column }) => scroll(lineNumber, column),
      createDecorationsCollection: (items) => {
        const set = (decorations: unknown[]) => view.dispatch({ effects: reviewChanged.of(decorations as ReviewDecoration[]) });
        set(items);
        return { set, clear: () => { if (viewRef.current === view) set([]); } };
      },
      onDidChangeCursorPosition: (handler) => { positionListeners.add(handler); },
      onDidChangeCursorSelection: (handler) => { selectionListeners.add(handler); },
      onDidChangeModelContent: (handler) => { contentListeners.add(handler); },
      onDidChangeModel: () => {},
      onDidDispose: (handler) => { disposeListeners.add(handler); },
    };
    propsRef.current.onMount(handle);
    notifyPosition(view);
    return () => {
      props.sessions.set(props.documentId, { state: view.state, scrollTop: view.scrollDOM.scrollTop, liveConfig: liveConfig.current, editConfig: editConfig.current, wrapConfig: wrapConfig.current, eventConfig: eventConfig.current, lineConfig: lineConfig.current });
      viewRef.current = null;
      for (const listener of disposeListeners) listener();
      view.destroy();
    };
  }, [props.documentId]);

  useEffect(() => {
    const view = viewRef.current!;
    if (view.state.sliceDoc() !== props.value) view.dispatch(externalSourceUpdate(view.state, props.value, lineConfig.current));
  }, [props.value]);
  useEffect(() => {
    viewRef.current!.dispatch({ effects: liveConfig.current.reconfigure(liveMarkdown(props)) });
  }, [props.components, props.resolveUrl, props.urlTransform, props.scopeId, props.readOnly]);
  useEffect(() => {
    viewRef.current!.dispatch({ effects: editConfig.current.reconfigure([EditorState.readOnly.of(props.readOnly), EditorView.editable.of(!props.readOnly)]) });
  }, [props.readOnly]);
  useEffect(() => {
    viewRef.current!.dispatch({ effects: wrapConfig.current.reconfigure(props.wordWrap === false ? [] : EditorView.lineWrapping) });
  }, [props.wordWrap]);

  return <div ref={hostRef} className="live-markdown-editor flex-1 min-h-0" style={{ "--live-md-scale": props.textScale } as React.CSSProperties} />;
};
