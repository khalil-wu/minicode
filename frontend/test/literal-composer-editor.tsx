import { forwardRef } from "react";
import type { ComposerSelection } from "../src.v2/composer/inputSelection";
import type { RichComposerDocument } from "../src.v2/composer/rich-composer-document";

// Workflow unit suites drive the supported literal mode through native textarea
// events. RichComposer.chain and the model suite exercise the real PM editor.
export const LiteralComposerEditor = forwardRef<HTMLTextAreaElement, {
  value: string; ariaLabel: string; disabled: boolean; placeholder: string; initialFocus: boolean; enterBehavior: string;
  onChange(value: string, selection: ComposerSelection, document?: RichComposerDocument): void;
  onSelectionChange(value: string, selection: ComposerSelection): void;
  onKeyDown(event: KeyboardEvent): void; onPaste(event: ClipboardEvent): void; onDrop(event: DragEvent): void;
  onCompositionChange(value: boolean): void; onFocusChange(value: boolean): void;
}>((props, ref) => <textarea ref={ref} value={props.value} aria-label={props.ariaLabel} disabled={props.disabled}
  placeholder={props.placeholder} autoFocus={props.initialFocus} data-composer-input data-enter-behavior={props.enterBehavior}
  className="composer-textarea"
  onChange={event => props.onChange(event.currentTarget.value, { start: event.currentTarget.selectionStart, end: event.currentTarget.selectionEnd })}
  onSelect={event => props.onSelectionChange(event.currentTarget.value, { start: event.currentTarget.selectionStart, end: event.currentTarget.selectionEnd })}
  onKeyDown={event => props.onKeyDown(event as unknown as KeyboardEvent)} onPaste={event => props.onPaste(event as unknown as ClipboardEvent)}
  onDrop={event => props.onDrop(event as unknown as DragEvent)}
  onCompositionStart={() => props.onCompositionChange(true)} onCompositionEnd={() => props.onCompositionChange(false)}
  onFocus={() => props.onFocusChange(true)} onBlur={() => props.onFocusChange(false)} />);
LiteralComposerEditor.displayName = "LiteralComposerEditor";
