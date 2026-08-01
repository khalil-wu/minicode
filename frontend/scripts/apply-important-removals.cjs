/* Apply provably-safe !important removals listed by analyze-important.cjs.
 * Reuses the same parser; only rewrites the exact declaration lines of
 * top-level (non-media) rules whose (selector, prop) pair is allowlisted.
 * Prints a per-file diff count for review.
 */
const fs = require("fs");
const path = require("path");

const SRC = path.resolve(__dirname, "..", "src.v2");
const list = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

const allow = new Map(); // file -> Set("selector\0prop")
for (const r of list) {
  if (!allow.has(r.file)) allow.set(r.file, new Set());
  allow.get(r.file).add(`${r.selector.replace(/…$/, "")}\0${r.prop}`);
}

function stripCommentsKeepLength(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, (m) => " ".repeat(m.length));
}

function splitMedia(text) {
  const mediaRanges = [];
  let i = 0;
  while (i < text.length) {
    const at = text.indexOf("@", i);
    if (at === -1) break;
    const headerEnd = text.indexOf("{", at);
    if (headerEnd === -1) break;
    const header = text.slice(at, headerEnd).trim();
    if (!/^@(media|supports)/.test(header)) { i = headerEnd + 1; continue; }
    let depth = 1, j = headerEnd + 1;
    while (j < text.length && depth > 0) {
      if (text[j] === "{") depth++;
      else if (text[j] === "}") depth--;
      j++;
    }
    mediaRanges.push([at, j]);
    i = j;
  }
  return mediaRanges;
}

let totalRemoved = 0;
for (const [relFile, keys] of allow) {
  const file = path.join(SRC, relFile);
  if (!fs.existsSync(file)) continue;
  const original = fs.readFileSync(file, "utf8");
  const masked = stripCommentsKeepLength(original);
  const mediaRanges = splitMedia(masked);
  const inMedia = (idx) => mediaRanges.some(([a, b]) => idx >= a && idx < b);

  // Leave the closing brace unconsumed (lookahead) so the following rule
  // can anchor on it; otherwise every other rule is silently skipped.
  const re = /(^|\})\s*([^{}@]+)\{([^{}]*)(?=\})/g;
  let m;
  let out = "";
  let last = 0;
  let removed = 0;
  while ((m = re.exec(masked))) {
    const ruleStart = m.index;
    const selector = m[2].trim();
    const body = m[3];
    const bodyStart = m.index + m[0].length - body.length - 1;
    // Test at the opening brace, not m.index: m.index may be the closing
    // brace of a preceding @media block, which would falsely mark this
    // top-level rule as media-nested.
    if (inMedia(bodyStart)) continue;
    const sels = selector.split(",").map((s) => s.trim()).filter(Boolean);
    const wanted = new Set();
    for (const sel of sels) {
      for (const key of keys) {
        const [selKey, prop] = key.split("\0");
        if (sel === selKey || sel.startsWith(selKey)) wanted.add(prop);
      }
    }
    if (wanted.size === 0) continue;
    const newBody = body.replace(
      /([\w-]+)(\s*:\s*[^;]+?)(\s*!important)(\s*(?:;|$))/g,
      (full, prop, mid, imp, tail) => {
        if (wanted.has(prop.toLowerCase())) {
          removed++;
          return `${prop}${mid}${tail}`;
        }
        return full;
      },
    );
    if (newBody !== body) {
      out += original.slice(last, bodyStart + 1);
      out += newBody;
      last = bodyStart + body.length;
    }
  }
  if (removed > 0) {
    out += original.slice(last);
    fs.writeFileSync(file, out);
    console.log(`${relFile}: removed ${removed}`);
    totalRemoved += removed;
  }
}
console.log(`TOTAL removed: ${totalRemoved}`);
