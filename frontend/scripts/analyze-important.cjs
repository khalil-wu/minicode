/* Conservative !important necessity analysis (no external deps, Node 12+).
 *
 * For each `prop: value !important` declaration in a rule R(selector S):
 *   candidates = rules in ANY css file whose rightmost compound selector
 *   matches S's rightmost compound AND which declare the same prop.
 *   If every candidate has lower specificity than S, or equal specificity but
 *   appears earlier in load order, then S already wins the cascade and the
 *   !important is provably unnecessary.
 *
 * Over-approximates competition (rightmost-compound matching), so it only
 * ever reports removals that are safe; it will keep some that are not needed.
 */
const fs = require("fs");
const path = require("path");

const SRC = path.resolve(__dirname, "..", "src.v2");
// Load order taken from main.tsx imports (styles first, then feature css is
// imported lazily per component; approximate: global styles before feature).
const GLOBAL_FIRST = ["styles/", "reset.css"];

function walk(dir, out) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const f = path.join(dir, e.name);
    if (e.isDirectory()) walk(f, out);
    else if (e.name.endsWith(".css")) out.push(f);
  }
  return out;
}

function stripComments(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, "");
}

function specificity(sel) {
  // Pseudo-elements add nothing; pseudo-classes (:hover, :focus-visible...)
  // count at class level (per CSS spec).
  const s = sel.replace(/::[a-zA-Z-]+/g, "");
  const ids = (s.match(/#[\w-]+/g) || []).length;
  const classes =
    (s.match(/\.[\w-]+|\[[^\]]*\]|:(?!:)[a-zA-Z-]+(\([^)]*\))?/g) || []).length;
  const elements = (s.replace(/[#.[:][^>\s]+/g, " ").match(/\b[a-zA-Z][\w-]*\b/g) || [])
    .filter((t) => !/^(from|to|not|is|where)$/.test(t)).length;
  return [ids, classes, elements];
}

function cmpSpec(a, b) {
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return 0;
}

function rightmostCompound(sel) {
  const parts = sel.split(/[>\s+~]+/).filter(Boolean);
  return parts[parts.length - 1] || sel;
}

function* rulesOf(file, text, orderBase, inMedia) {
  // Very small parser: match "selector { body }" blocks, skip @keyframes bodies.
  // Anchor on the PREVIOUS closing brace via a lookbehind-free trick:
  // consume it as m[1] but leave THIS rule's closing brace unconsumed
  // (lookahead), so the next rule can anchor on it. Without the lookahead
  // every other rule is silently skipped.
  const re = /(^|\})\s*([^{}@]+)\{([^{}]*)(?=\})/g;
  let m;
  let idx = 0;
  while ((m = re.exec(text))) {
    const selector = m[2].trim();
    const body = m[3];
    if (!selector || selector.startsWith("@")) continue;
    const sels = selector.split(",").map((s) => s.trim()).filter(Boolean);
    const decls = [];
    const declRe = /([\w-]+)\s*:\s*([^;]+?)(\s*!important)?\s*(?:;|$)/g;
    let d;
    while ((d = declRe.exec(body))) {
      decls.push({ prop: d[1].toLowerCase(), important: !!d[3] });
    }
    yield { file, selector: sels, decls, order: orderBase + idx++, inMedia };
  }
}

// Split a stylesheet into top-level text and @media/@supports inner texts.
function splitMedia(text) {
  const mediaBlocks = [];
  let top = "";
  let i = 0;
  while (i < text.length) {
    const at = text.indexOf("@", i);
    if (at === -1) { top += text.slice(i); break; }
    top += text.slice(i, at);
    const headerEnd = text.indexOf("{", at);
    if (headerEnd === -1) { top += text.slice(at); break; }
    const header = text.slice(at, headerEnd).trim();
    if (!/^@(media|supports)/.test(header)) {
      // Non-grouping at-rule: keep inline, skip its body braces naively.
      top += text.slice(at, headerEnd + 1);
      i = headerEnd + 1;
      continue;
    }
    // Find matching closing brace.
    let depth = 1;
    let j = headerEnd + 1;
    while (j < text.length && depth > 0) {
      if (text[j] === "{") depth++;
      else if (text[j] === "}") depth--;
      j++;
    }
    mediaBlocks.push(text.slice(headerEnd + 1, j - 1));
    i = j;
  }
  return { top, mediaBlocks };
}

const files = walk(SRC, []);
const allRules = [];
files.forEach((file, fi) => {
  const text = stripComments(fs.readFileSync(file, "utf8"));
  const { top, mediaBlocks } = splitMedia(text);
  for (const rule of rulesOf(file, top, fi * 100000, false)) {
    allRules.push(rule);
  }
  for (const inner of mediaBlocks) {
    for (const rule of rulesOf(file, inner, fi * 100000, true)) {
      allRules.push(rule);
    }
  }
});

// Index by rightmost compound -> rules (for competition lookup)
const byRightmost = new Map();
for (const rule of allRules) {
  for (const sel of rule.selector) {
    const rm = rightmostCompound(sel);
    if (!byRightmost.has(rm)) byRightmost.set(rm, []);
    byRightmost.get(rm).push({ sel, rule });
  }
}

const removable = [];
for (const rule of allRules) {
  for (const sel of rule.selector) {
    if (rule.inMedia) continue; // never touch media-nested rules
    const specS = specificity(sel);
    const rm = rightmostCompound(sel);
    const competitors = byRightmost.get(rm) || [];
    for (const decl of rule.decls) {
      if (!decl.important) continue;
      let needed = false;
      for (const { sel: sel2, rule: rule2 } of competitors) {
        if (rule2 === rule) continue;
        if (!rule2.decls.some((d2) => d2.prop === decl.prop)) continue;
        // Any !important competitor at all -> keep.
        if (rule2.decls.some((d2) => d2.prop === decl.prop && d2.important)) {
          needed = true;
          break;
        }
        const cmp = cmpSpec(specificity(sel2), specS);
        const sameFile = rule2.file === rule.file;
        // Cross-file: Vite's real bundle order follows the module graph, not
        // our walk order — never rely on it. Only specificity wins count.
        // Same-file: source order is real, so earlier losers are safe.
        // Media-nested competitors may activate under unknown conditions:
        // treat them as winning ties regardless of file.
        if (
          cmp > 0 ||
          (cmp === 0 && rule2.inMedia) ||
          (cmp === 0 && sameFile && rule2.order > rule.order) ||
          (cmp === 0 && !sameFile)
        ) {
          needed = true;
          break;
        }
      }
      if (!needed) {
        removable.push({
          file: path.relative(SRC, rule.file),
          selector: sel,
          prop: decl.prop,
        });
      }
    }
  }
}

console.log(JSON.stringify(removable, null, 1));
console.error(`total removable: ${removable.length}`);
