/** Current desktop Source C4/SWc/CWc + XWc/ZWc; static typed port, no UI or IO. */
interface MatchRange { startOffset: number; endOffset: number }
type MatchingMode = "IGNORE_CASE" | "MATCH_CASE" | "FIRST_LETTER";
const MAX_PATTERN_LENGTH = 100, NO_MATCH = -2147483648, PATH_SEPARATOR = "\0", START_MATCH_BOOST = 10000;
const PATH_SEPARATORS = ["/", "\\"];

export function createChangedFileMatcher(e: string): (target: string) => number { let t = e.trim(); if (t.length === 0)
    return () => 0; let n = createMinusculeMatcher(t), r = hasPathSeparator(t); return e => scoreCandidate(e, n, r); }

function scoreCandidate(e: string, t: MinusculeMatcher, n: boolean): number { let r = n ? unifySeparators(e) : e, i = t.matchingDegree(r); if (i === NO_MATCH)
    return 0; let a = i * 10 - e.length; return a <= 0 ? 1 : a; }

function createMinusculeMatcher(e: string): MinusculeMatcher { let t = Array.from(e, e => { let t = e.toLowerCase(); return t.length === e.length ? t : e; }).join(``), n = hasPathSeparator(t), r = n ? pathPattern(t) : `*${t}`, i = lastPathSegment(t), a = new MinusculePattern(r, `IGNORE_CASE`, PATH_SEPARATORS.join(``)), o = n && t !== i ? new MinusculePattern(i, `IGNORE_CASE`, PATH_SEPARATORS.join(``)) : null; return new MinusculeMatcher(a, o); }

function pathPattern(e: string): string { let t = `*${e}`; for (let e of PATH_SEPARATORS)
    t = t.split(e).join(`*${PATH_SEPARATOR}*`); return t; }

function lastPathSegment(e: string): string { let t = -1; for (let n of PATH_SEPARATORS) {
    let r = e.lastIndexOf(n);
    r >= 0 && r < e.length - 1 && (t = Math.max(t, r));
} return e.slice(t + 1); }

function unifySeparators(e: string): string { let t = e; for (let e of PATH_SEPARATORS)
    t = t.split(e).join(PATH_SEPARATOR); return t; }

function hasPathSeparator(e: string): boolean { for (let t of PATH_SEPARATORS)
    if (e.includes(t))
        return !0; return !1; }

function boostStartMatch(e: number, t: MatchRange[]): number { return t.length === 0 ? e : t[0].startOffset === 0 ? e + START_MATCH_BOOST : e; }

function isPatternSeparator(e: string): boolean { return e.trim().length === 0 || e === `_` || e === `-` || e === `:` || e === `+` || e === `.` || e === `/` || e === `\\`; }

function advanceWord(e: string, t: number): number { return t < e.length && isDigit(e[t]) ? t + 1 : nextWordStart(e, t); }

function nextWordStart(e: string, t: number): number { for (let n = t + 1; n <= e.length; n += 1) {
    if (n >= e.length)
        return e.length + 1;
    if (isWordStart(e, n))
        return n;
} return e.length + 1; }

function isWordStart(e: string, t: number): boolean { if (t < 0 || t >= e.length)
    return !1; let n = e[t]; if (!isWordChar(n))
    return !1; if (t === 0)
    return !0; let r = e[t - 1]; return !!(!isWordChar(r) || isUppercase(n) && isLowercase(r) || isDigit(n) && !isDigit(r)); }

function findPatternChar(e: string[], t: string, n: number, r: number, i: boolean): number { if (!i) {
    for (let i = n; i < r; i += 1)
        if (e[i] === t)
            return i;
    return -1;
} let a = t.toLowerCase(), o = t.toUpperCase(); for (let t = n; t < r; t += 1) {
    let n = e[t];
    if (n === a || n === o)
        return t;
} return -1; }

function isWildcardChar(e: string): boolean { return e === ` ` || e === `*`; }

function findAnyChar(e: string, t: readonly string[], n: number, r: number): number { for (let i = n; i < r; i += 1)
    if (t.includes(e[i]))
        return i; return -1; }

function findChar(e: string, t: string, n: number, r: number): number { for (let i = n; i < r; i += 1)
    if (e[i] === t)
        return i; return -1; }

function indexOfIgnoreCase(e: string, t: string, n: number, r: number): number { let i = e.toLowerCase(), a = t.toLowerCase(), o = i.indexOf(a, n); return o < 0 || o + t.length > r ? -1 : o; }

function startsWithIgnoreCase(e: string, t: number, n: number, r: string): boolean { return t + n > e.length ? !1 : e.slice(t, t + n).toLowerCase() === r.toLowerCase(); }

function removeWildcards(e: readonly string[]): string { let t = ``; for (let n of e)
    n !== `*` && (t += n); return t; }

function prependMatch(e: MatchRange[], t: number, n: number): MatchRange[] { if (e.length === 0)
    return [{ startOffset: t, endOffset: t + n }]; let r = e[e.length - 1]; return r.startOffset === t + n ? e[e.length - 1] = { startOffset: t, endOffset: r.endOffset } : e.push({ startOffset: t, endOffset: t + n }), e; }

function isUppercase(e: string): boolean { return e.toUpperCase() === e && e.toLowerCase() !== e; }

function isLowercase(e: string): boolean { return e.toLowerCase() === e && e.toUpperCase() !== e; }

function isDigit(e: string): boolean { return e >= `0` && e <= `9`; }

function isWordChar(e: string): boolean { return /[a-z0-9]/i.test(e); }

class MinusculeMatcher {
    mainMatcher: MinusculePattern;
    fallbackMatcher: MinusculePattern | null;
    constructor(e: MinusculePattern, t: MinusculePattern | null) { this.mainMatcher = e, this.fallbackMatcher = t; }
    matchingDegree(e: string): number { let t = this.mainMatcher.match(e); if (t != null)
        return boostStartMatch(this.mainMatcher.matchingDegree(e, !1, t), t); if (this.fallbackMatcher == null)
        return NO_MATCH; let n = this.fallbackMatcher.match(e); return n == null ? NO_MATCH : boostStartMatch(this.fallbackMatcher.matchingDegree(e, !1, n), n); }
    matchingFragments(e: string): MatchRange[] | null { return this.mainMatcher.match(e) ?? this.fallbackMatcher?.match(e) ?? null; }
}

class MinusculePattern {
    myPattern: string[];
    isLowerCase: boolean[];
    isUpperCase: boolean[];
    isWordSeparator: boolean[];
    toUpperCase: string[];
    toLowerCase: string[];
    hardSeparators: string[];
    matchingMode: MatchingMode;
    mixedCase: boolean;
    hasDots: boolean;
    meaningfulCharacters: string[];
    minNameLength: number;
    failedMatchStates?: Set<number>;
    constructor(e: string, t: MatchingMode, n: string) { let r = e.endsWith(`* `) ? e.slice(0, -2) : e; this.myPattern = r.split(``), this.isLowerCase = Array.from({ length: this.myPattern.length }, () => !1), this.isUpperCase = Array.from({ length: this.myPattern.length }, () => !1), this.isWordSeparator = Array.from({ length: this.myPattern.length }, () => !1), this.toUpperCase = Array.from({ length: this.myPattern.length }, () => ``), this.toLowerCase = Array.from({ length: this.myPattern.length }, () => ``), this.hardSeparators = Array.from(n), this.matchingMode = t; let i = [], a = !1, o = !1, s = !1, c = !1; for (let e = 0; e < this.myPattern.length; e += 1) {
        let t = this.myPattern[e], n = isPatternSeparator(t), r = isUppercase(t), l = isLowercase(t), u = t.toUpperCase(), d = t.toLowerCase();
        l && (o = !0), t === `.` && (c = !0), a && r && (s = !0), isWildcardChar(t) || (a = !0, i.push(d), i.push(u)), this.isWordSeparator[e] = n, this.isUpperCase[e] = r, this.isLowerCase[e] = l, this.toUpperCase[e] = u, this.toLowerCase[e] = d;
    } this.hasDots = c, this.mixedCase = o && s, this.meaningfulCharacters = i, this.minNameLength = i.length / 2; }
    get pattern(): string { return this.myPattern.join(``); }
    matchingDegree(e: string, t: boolean = !1, n: MatchRange[] | null = this.match(e)): number { if (n == null)
        return NO_MATCH; if (n.length === 0)
        return 0; let r = n[0], i = r.startOffset === 0, a = i && t, o = 0, s = -1, c = 0, l = 0, u = !1; for (let t of n)
        for (let n = t.startOffset; n < t.endOffset; n += 1) {
            let i = n === t.startOffset && t !== r, d = !1;
            for (; l <= n;)
                l === n ? d = !0 : i && (c += 1), l = advanceWord(e, l);
            let f = e[n];
            if (s = findPatternChar(this.myPattern, f, s + 1, this.myPattern.length, !0), s < 0)
                break;
            d && (u = f === this.myPattern[s] && this.isUpperCase[s]), o += this.evaluateCaseMatching(a, s, u, n, i, d, f);
        } let d = r.startOffset, f = findAnyChar(e, this.hardSeparators, 0, d) >= 0, p = d === 0 || isWordStart(e, d) && !isWordStart(e, d - 1), m = n[n.length - 1].endOffset === e.length; return (p ? 1e3 : 0) + o - n.length + -c * 10 + (f ? 0 : 2) + +!!i + +!!m; }
    match(e: string): MatchRange[] | null { if (e.length < this.minNameLength)
        return null; if (this.myPattern.length > MAX_PATTERN_LENGTH)
        return this.matchBySubstring(e); let t = 0; for (let n = 0; n < e.length && t < this.meaningfulCharacters.length; n += 1) {
        let r = e[n];
        if (r === this.meaningfulCharacters[t] || r === this.meaningfulCharacters[t + 1]) {
            let r = this.myPattern.length - 2;
            if (t === this.meaningfulCharacters.length - 2 && r >= 0 && isWordChar(this.myPattern[r + 1]) && !this.isWildcard(r) && (n === 0 || !this.charEquals(this.myPattern[r], r, e[n - 1], this.matchingMode !== `MATCH_CASE`)) && !isWordStart(e, n))
                continue;
            t += 2;
        }
    } if (t < this.minNameLength * 2)
        return null; this.failedMatchStates?.clear(); let n = this.matchWildcards(e, 0, 0); return n == null ? null : n.reverse(); }
    evaluateCaseMatching(e: boolean, t: number, n: boolean, r: number, i: boolean, a: boolean, o: string): number { return i && a && this.isLowerCase[t] ? -10 : o === this.myPattern[t] ? this.isUpperCase[t] ? 50 : r === 0 && e ? 150 : +!!a : a || this.isLowerCase[t] && n ? -1 : 0; }
    matchBySubstring(e: string): MatchRange[] | null { let t = this.isPatternChar(0, `*`), n = removeWildcards(this.myPattern); if (e.length < n.length)
        return null; if (t) {
        let t = indexOfIgnoreCase(e, n, 0, e.length);
        return t >= 0 ? [{ startOffset: t, endOffset: t + n.length }] : null;
    } return startsWithIgnoreCase(e, 0, n.length, n) ? [{ startOffset: 0, endOffset: n.length }] : null; }
    matchWildcards(e: string, t: number, n: number): MatchRange[] | null { let r = t; if (n < 0)
        return null; if (!this.isWildcard(r))
        return r === this.myPattern.length ? [] : this.matchFragment(e, r, n); do
        r += 1;
    while (this.isWildcard(r)); if (r === this.myPattern.length) {
        if (this.isTrailingSpacePattern() && n !== e.length && (r < 2 || !this.isUpperCaseOrDigit(r - 2))) {
            let t = e.indexOf(` `, n);
            return t >= 0 ? [{ startOffset: t, endOffset: t + 1 }] : null;
        }
        return [];
    } return this.matchSkippingWords(e, r, this.findNextPatternCharOccurrence(e, n, r), !0); }
    isTrailingSpacePattern(): boolean { return this.isPatternChar(this.myPattern.length - 1, ` `); }
    isUpperCaseOrDigit(e: number): boolean { return this.isUpperCase[e] || isDigit(this.myPattern[e]); }
    matchSkippingWords(e: string, t: number, n: number, r: boolean): MatchRange[] | null { if (n < 0)
        return null; let i = (t * (e.length + 1) + n) * 2 + +!!r; if (this.failedMatchStates?.has(i))
        return null; let a = n, o = 0; for (; a >= 0;) {
        let n = this.seemsLikeFragmentStart(e, t, a) ? this.maxMatchingFragment(e, t, a) : 0;
        if (n > o || a + n === e.length && this.isTrailingSpacePattern()) {
            this.isMiddleMatch(e, t, a) || (o = n);
            let r = this.matchInsideFragment(e, t, a, n);
            if (r != null)
                return r;
        }
        let i = this.findNextPatternCharOccurrence(e, a + 1, t);
        a = r ? i : this.checkForSpecialChars(e, a + 1, i, t);
    } return (this.failedMatchStates ??= new Set).add(i), null; }
    findNextPatternCharOccurrence(e: string, t: number, n: number): number { return !this.isPatternChar(n - 1, `*`) && !this.isWordSeparator[n] ? this.indexOfWordStart(e, n, t) : this.indexOfIgnoreCase(e, t, n); }
    checkForSpecialChars(e: string, t: number, n: number, r: number): number { return n < 0 || this.hasDots && !this.isPatternChar(r - 1, `.`) && findChar(e, `.`, t, n) !== -1 ? -1 : n; }
    seemsLikeFragmentStart(e: string, t: number, n: number): boolean { return !this.isUpperCase[t] || isUppercase(e[n]) || isWordStart(e, n) ? !0 : !this.mixedCase && this.matchingMode !== `MATCH_CASE`; }
    charEquals(e: string, t: number, n: string, r: boolean): boolean { return e === n ? !0 : r ? this.toLowerCase[t] === n || this.toUpperCase[t] === n : !1; }
    matchFragment(e: string, t: number, n: number): MatchRange[] | null { let r = this.maxMatchingFragment(e, t, n); return r === 0 ? null : this.matchInsideFragment(e, t, n, r); }
    maxMatchingFragment(e: string, t: number, n: number): number { if (!this.isFirstCharMatching(e, n, t))
        return 0; let r = 1, i = this.matchingMode !== `MATCH_CASE`; for (; n + r < e.length && t + r < this.myPattern.length;) {
        let a = e[n + r];
        if (!this.charEquals(this.myPattern[t + r], t + r, a, i)) {
            if (this.isSkippingDigitBetweenPatternDigits(t + r, a))
                return 0;
            break;
        }
        r += 1;
    } return r; }
    isSkippingDigitBetweenPatternDigits(e: number, t: string): boolean { return isDigit(this.myPattern[e]) && isDigit(this.myPattern[e - 1]) && isDigit(t); }
    matchInsideFragment(e: string, t: number, n: number, r: number): MatchRange[] | null { let i = this.isMiddleMatch(e, t, n) ? 3 : 1; return this.improveCamelHumps(e, t, n, r, i) ?? this.findLongestMatchingPrefix(e, t, n, r, i); }
    isMiddleMatch(e: string, t: number, n: number): boolean { return !this.isPatternChar(t - 1, `*`) || this.isWildcard(t + 1) || !isWordChar(e[n]) ? !1 : !isWordStart(e, n); }
    findLongestMatchingPrefix(e: string, t: number, n: number, r: number, i: number): MatchRange[] | null { if (t + r >= this.myPattern.length)
        return [{ startOffset: n, endOffset: n + r }]; let a = r; for (; a >= i || a > 0 && this.isWildcard(t + a);) {
        let r: MatchRange[] | null = null;
        if (this.isWildcard(t + a))
            r = this.matchWildcards(e, t + a, n + a);
        else {
            let i = this.findNextPatternCharOccurrence(e, n + a + 1, t + a);
            i = this.checkForSpecialChars(e, n + a, i, t + a), i >= 0 && (r = this.matchSkippingWords(e, t + a, i, !1));
        }
        if (r != null)
            return prependMatch(r, n, a);
        --a;
    } return null; }
    improveCamelHumps(e: string, t: number, n: number, r: number, i: number): MatchRange[] | null { for (let a = i; a < r; a += 1)
        if (this.isUppercasePatternVsLowercaseNameChar(e, t + a, n + a)) {
            let r = this.findUppercaseMatchFurther(e, t + a, n + a);
            if (r != null)
                return prependMatch(r, n, a);
        } return null; }
    isUppercasePatternVsLowercaseNameChar(e: string, t: number, n: number): boolean { return this.isUpperCase[t] && this.myPattern[t] !== e[n]; }
    findUppercaseMatchFurther(e: string, t: number, n: number): MatchRange[] | null { let r = this.indexOfWordStart(e, t, n); return this.matchWildcards(e, t, r); }
    isFirstCharMatching(e: string, t: number, n: number): boolean { if (t >= e.length)
        return !1; let r = this.matchingMode !== `MATCH_CASE`, i = this.myPattern[n]; return this.charEquals(i, n, e[t], r) ? this.matchingMode === `FIRST_LETTER` && (n === 0 || n === 1 && this.isWildcard(0)) && this.hasCase(n) ? this.isUpperCase[n] === isUppercase(e[0]) : !0 : !1; }
    hasCase(e: number): boolean { return this.isUpperCase[e] || this.isLowerCase[e]; }
    isWildcard(e: number): boolean { return e >= 0 && e < this.myPattern.length && isWildcardChar(this.myPattern[e]); }
    isPatternChar(e: number, t: string): boolean { return e < 0 || e >= this.myPattern.length ? !1 : this.myPattern[e] === t; }
    indexOfWordStart(e: string, t: number, n: number): number { let r = this.myPattern[t]; if (n >= e.length || this.mixedCase && this.isLowerCase[t] && !(t > 0 && this.isWordSeparator[t - 1]))
        return -1; let i = n, a = !isWordChar(r); for (;;) {
        if (i = this.indexOfIgnoreCase(e, i, t), i < 0)
            return -1;
        if (a || isWordStart(e, i))
            return i;
        i += 1;
    } }
    indexOfIgnoreCase(e: string, t: number, n: number): number { let r = this.toUpperCase[n], i = this.toLowerCase[n]; for (let n = t; n < e.length; n += 1) {
        let t = e[n];
        if (t === r || t === i)
            return n;
    } return -1; }
}

function fileParts(path: string): { fileName: string; parentPath: string } {
  const index = path.lastIndexOf("/");
  return index === -1 ? { fileName: path, parentPath: "" } : { fileName: path.slice(index + 1), parentPath: path.slice(0, index) };
}

/** Source fr/pr: filename score first, then full path; stable equal-score order. */
export function rankChangedFilePaths(paths: readonly string[], query: string): string[] {
  const trimmed = query.trim();
  if (trimmed.length === 0) return [...paths].sort((left, right) => {
    const a = fileParts(left), b = fileParts(right);
    return a.fileName.localeCompare(b.fileName) || a.parentPath.localeCompare(b.parentPath);
  });
  const matcher = createChangedFileMatcher(trimmed);
  return paths.map(path => {
    const fileScore = matcher(fileParts(path).fileName);
    return { path, score: fileScore > 0 ? fileScore : matcher(path) };
  }).filter(entry => entry.score > 0).sort((left, right) => right.score - left.score).map(entry => entry.path);
}
