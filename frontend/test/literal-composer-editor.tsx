import { forwardRef, useImperativeHandle, useLayoutEffect, useRef } from "react";
import type { ComposerSelection } from "../src.v2/composer/inputSelection";
import { documentPosition, literalComposerDocument, richComposerSchema, richDocumentState, type RichComposerDocument } from "../src.v2/composer/rich-composer-document";
import { composerReferenceMarkdown, type ComposerReferenceInsertion } from "../src.v2/composer/composer-references";
import { EditorState } from "prosemirror-state";

export const insertLiteralReference = (value: string, { reference, selection }: ComposerReferenceInsertion) => {
  const token = composerReferenceMarkdown(reference) + " ";
  const next = value.slice(0, selection.start) + token + value.slice(selection.end);
  const caret = selection.start + token.length;
  const document = literalComposerDocument(next);
  const transaction = EditorState.create({ schema: richComposerSchema, doc: document }).tr
    .setNodeMarkup(documentPosition(document, selection.start, true), undefined, { reference });
  return { value: next, selection: { start: caret, end: caret }, document: richDocumentState(transaction.doc, true) };
};

// Workflow unit suites drive the supported literal mode through native textarea
// events. RichComposer.chain and the model suite exercise the real PM editor.
export const LiteralComposerEditor = forwardRef<HTMLTextAreaElement, {
  value: string; ariaLabel: string; disabled: boolean; placeholder: string; initialFocus: boolean; enterBehavior: string;
  onChange(value: string, selection: ComposerSelection, document?: RichComposerDocument): void;
  onSelectionChange(value: string, selection: ComposerSelection): void;
  onKeyDown(event: KeyboardEvent): void; onPaste(event: ClipboardEvent): void; onDrop(event: DragEvent): void;
  onCompositionChange(value: boolean): void; onFocusChange(value: boolean): void;
  referenceInsertion?: ComposerReferenceInsertion;
}>((props, ref) => {
  const input = useRef<HTMLTextAreaElement>(null);
  const pendingCaret = useRef<number>();
  useImperativeHandle(ref, () => input.current!);
  useLayoutEffect(() => {
    if (!props.referenceInsertion) return;
    const next = insertLiteralReference(props.value, props.referenceInsertion);
    pendingCaret.current = next.selection.start;
    props.onChange(next.value, next.selection, next.document);
  }, [props.referenceInsertion]);
  useLayoutEffect(() => { if (pendingCaret.current !== undefined) {
    input.current!.setSelectionRange(pendingCaret.current, pendingCaret.current); input.current!.focus(); pendingCaret.current = undefined;
  } }, [props.value]);
  return <textarea ref={input} value={props.value} aria-label={props.ariaLabel} disabled={props.disabled}
  placeholder={props.placeholder} autoFocus={props.initialFocus} data-composer-input data-enter-behavior={props.enterBehavior}
  className="composer-textarea"
  onChange={event => props.onChange(event.currentTarget.value, { start: event.currentTarget.selectionStart, end: event.currentTarget.selectionEnd })}
  onSelect={event => props.onSelectionChange(event.currentTarget.value, { start: event.currentTarget.selectionStart, end: event.currentTarget.selectionEnd })}
  onKeyDown={event => props.onKeyDown(event as unknown as KeyboardEvent)} onPaste={event => props.onPaste(event as unknown as ClipboardEvent)}
  onDrop={event => props.onDrop(event as unknown as DragEvent)}
  onCompositionStart={() => props.onCompositionChange(true)} onCompositionEnd={() => props.onCompositionChange(false)}
  onFocus={() => props.onFocusChange(true)} onBlur={() => props.onFocusChange(false)} />;
});
LiteralComposerEditor.displayName = "LiteralComposerEditor";
