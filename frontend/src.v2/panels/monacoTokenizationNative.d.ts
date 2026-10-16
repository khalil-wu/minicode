// Monaco 0.56's public setTokensProvider replaces the registry entry. Its
// pinned internal registry is needed to retain and restore the native owner.
declare module "monaco-editor/editor/common/languages.js" {
  type State = import("monaco-editor").languages.IState;
  export class Token {
    constructor(offset: number, type: string, language: string);
    offset: number;
    type: string;
    language: string;
  }
  export class TokenizationResult {
    constructor(tokens: Token[], endState: State);
    tokens: Token[];
    endState: State;
  }
  export class EncodedTokenizationResult {
    constructor(tokens: Uint32Array, fontInfo: unknown[], endState: State);
    tokens: Uint32Array;
    fontInfo: unknown[];
    endState: State;
  }
  export interface NativeTokenizationSupport {
    getInitialState(): State;
    tokenize(line: string, hasEOL: boolean, state: State): TokenizationResult;
    tokenizeEncoded(line: string, hasEOL: boolean, state: State): EncodedTokenizationResult;
  }
  export const TokenizationRegistry: {
    getOrCreate(language: string): Promise<NativeTokenizationSupport | null>;
    register(language: string, support: NativeTokenizationSupport): import("monaco-editor").IDisposable;
  };
}
declare module "monaco-editor/editor/standalone/common/standaloneTheme.js" {
  export const IStandaloneThemeService: unknown;
}
