export const MAX_EDITABLE_PASTE_CHARS = 20_000;

export const PASTED_TEXT_INPUT_SOURCE = "pasted_text" as const;

interface PastedTextMetadata {
  inputSource: typeof PASTED_TEXT_INPUT_SOURCE;
  charCount: number;
}

const pastedTextMetadata = new WeakMap<File, PastedTextMetadata>();
let pastedTextCounter = 0;

export const shouldAttachPastedText = (
  currentValue: string,
  pastedText: string,
  selectionStart: number,
  selectionEnd: number,
): boolean => {
  if (!pastedText) return false;
  const replacedLength = Math.max(0, selectionEnd - selectionStart);
  return currentValue.length - replacedLength + pastedText.length > MAX_EDITABLE_PASTE_CHARS;
};

export const buildPastedTextFile = (text: string): File => {
  pastedTextCounter += 1;
  const file = new File([text], `pasted-${pastedTextCounter}.txt`, { type: "text/plain" });
  pastedTextMetadata.set(file, {
    inputSource: PASTED_TEXT_INPUT_SOURCE,
    charCount: text.length,
  });
  return file;
};

export const getPastedTextMetadata = (file: File): PastedTextMetadata | undefined =>
  pastedTextMetadata.get(file);
