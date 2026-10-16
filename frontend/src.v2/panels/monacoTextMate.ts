import type * as Monaco from "monaco-editor";
import type { HighlighterCore } from "@shikijs/core";
import { EncodedTokenMetadata } from "@shikijs/vscode-textmate";
import { Token, TokenizationResult, EncodedTokenizationResult, TokenizationRegistry } from "monaco-editor/editor/common/languages.js";
import { StandaloneServices } from "monaco-editor/editor/standalone/browser/standaloneServices.js";
import { IStandaloneThemeService } from "monaco-editor/editor/standalone/common/standaloneTheme.js";
import { prepareCodeHighlighting, codeHighlightLanguage } from "../lib/code-highlighting";
import type { CodeThemeId } from "../lib/code-themes";
import type { ResolvedTheme } from "../stores/types";

type Stack = ReturnType<ReturnType<HighlighterCore["getLanguage"]>["tokenizeLine2"]>["ruleStack"];
class TextMateState implements Monaco.languages.IState {
  constructor(readonly stack: Stack | null = null, readonly native: Monaco.languages.IState | null = null) {}
  clone() { return new TextMateState(this.stack, this.native?.clone() ?? null); }
  equals(other: Monaco.languages.IState) {
    return other instanceof TextMateState && this.stack === other.stack
      && (this.native === null ? other.native === null : other.native !== null && this.native.equals(other.native));
  }
}
type Owner = {
  revision: number;
  id: CodeThemeId | null;
  variant: ResolvedTheme;
  providers: Map<string, Monaco.IDisposable>;
};
const owners = new WeakMap<typeof Monaco, Owner>();
const scope = (color: string) => `shiki.c${color.slice(1).toLowerCase()}`;
const FOREGROUND_MASK = 0x00ff8000, FONT_STYLE_MASK = 0x00007800;
type ThemeService = { getColorTheme(): { tokenTheme: { match(languageId: number, token: string): number } } };

async function installLanguage(monaco: typeof Monaco, owner: Owner, language: string) {
  if (!owner.id || codeHighlightLanguage(language) === "text") return;
  const revision = owner.revision;
  const [{ highlighter, loaded, lang }, original] = await Promise.all([
    prepareCodeHighlighting(owner.id, owner.variant, language),
    TokenizationRegistry.getOrCreate(language),
  ]);
  if (owner.revision !== revision || owner.providers.has(language)) return;
  const themeService = StandaloneServices.get<ThemeService>(IStandaloneThemeService);
  const shikiLine = (line: string, state: TextMateState) => {
      const { colorMap } = highlighter.setTheme(loaded.registrationName);
      return { result: highlighter.getLanguage(lang).tokenizeLine2(line, state.stack), colorMap };
  };
  const provider = TokenizationRegistry.register(language, {
    getInitialState: () => new TextMateState(null, original?.getInitialState() ?? null),
    tokenize(line, hasEOL, state) {
      const current = state as TextMateState;
      const { result, colorMap } = shikiLine(line, current);
      const native = original?.tokenize(line, hasEOL, current.native!);
      const tokens = native ? native.tokens : Array.from({ length: result.tokens.length / 2 }, (_, index) =>
        new Token(result.tokens[index * 2], scope(colorMap[EncodedTokenMetadata.getForeground(result.tokens[index * 2 + 1])]), language));
      return new TokenizationResult(tokens, new TextMateState(result.ruleStack, native?.endState ?? null));
    },
    tokenizeEncoded(line, hasEOL, state) {
      const current = state as TextMateState;
      const { result, colorMap } = shikiLine(line, current);
      const tokenTheme = themeService.getColorTheme().tokenTheme;
      const native = original?.tokenizeEncoded(line, hasEOL, current.native!);
      // A language without a native provider keeps its previous registered-
      // language/Other classification; no native semantics are invented.
      const nativeTokens = native?.tokens ?? new Uint32Array([0,
        tokenTheme.match(monaco.languages.getEncodedLanguageId(language), "") | 1024]);
      const tokens: number[] = [];
      let nativeIndex = 0, shikiIndex = 0, start = 0;
      while (true) {
        const metadata = nativeTokens[nativeIndex + 1];
        const color = colorMap[EncodedTokenMetadata.getForeground(result.tokens[shikiIndex + 1])];
        const foreground = tokenTheme.match(metadata & 255, scope(color)) & FOREGROUND_MASK;
        const merged = ((metadata & ~(FOREGROUND_MASK | FONT_STYLE_MASK)) | foreground) >>> 0;
        if (tokens.length === 0 || tokens[tokens.length - 1] !== merged) tokens.push(start, merged);
        const nextNative = nativeIndex + 2 < nativeTokens.length ? nativeTokens[nativeIndex + 2] : Infinity;
        const nextShiki = shikiIndex + 2 < result.tokens.length ? result.tokens[shikiIndex + 2] : Infinity;
        start = Math.min(nextNative, nextShiki);
        if (start === Infinity) break;
        if (start === nextNative) nativeIndex += 2;
        if (start === nextShiki) shikiIndex += 2;
      }
      return new EncodedTokenizationResult(new Uint32Array(tokens), native?.fontInfo ?? [], new TextMateState(result.ruleStack, native?.endState ?? null));
    },
  });
  // Monaco's registry replaces rather than stacks providers. Restore the
  // resolved native provider when this theme relinquishes the language.
  owner.providers.set(language, { dispose() {
    provider.dispose();
    if (original) TokenizationRegistry.register(language, original);
  } });
}

/** Keep grammar ownership reversible: no global setTheme patch, model swap,
 * source rewrite, or change to the user's font family/size/weight. */
export async function applyMonacoTextMate(monaco: typeof Monaco, variant: ResolvedTheme, id: CodeThemeId | null, data: Monaco.editor.IStandaloneThemeData) {
  let owner = owners.get(monaco);
  if (!owner) {
    owner = { revision: 0, id, variant, providers: new Map() };
    owners.set(monaco, owner);
    const current = owner;
    monaco.editor.onDidCreateModel(model => { void installLanguage(monaco, current, model.getLanguageId()); });
    monaco.editor.onDidChangeModelLanguage(event => { void installLanguage(monaco, current, event.model.getLanguageId()); });
  }
  const revision = ++owner.revision;
  owner.id = id; owner.variant = variant;
  for (const provider of owner.providers.values()) provider.dispose();
  owner.providers.clear();
  if (id === null) return;
  const { highlighter, loaded } = await prepareCodeHighlighting(id, variant);
  if (owner.revision !== revision) return;
  const { colorMap } = highlighter.setTheme(loaded.registrationName);
  monaco.editor.defineTheme(`minicode-${variant}`, { ...data, rules: [
    ...data.rules,
    ...colorMap.flatMap(color => color ? [{ token: scope(color), foreground: color.slice(1) }] : []),
  ] });
  await Promise.all(monaco.editor.getModels().map(model => installLanguage(monaco, owner!, model.getLanguageId())));
}
