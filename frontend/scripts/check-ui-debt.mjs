import { readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const src = path.join(root, "src.v2");
const distAssets = path.join(root, "dist", "assets");
const limits = {
  // Frozen after the 2026-07-17 workbench visual consolidation. New changes
  // must reduce or preserve this count; they must not silently raise it.
  importantTotal: 1034,
  tsWorkerBytes: 0,
  mainJsBytes: 1_280_000,
  // Icon entry-point consolidation (2026-07-25): semantic icons must come
  // from src.v2/lib/icons.ts. Direct lucide-react imports may only shrink.
  lucideDirectImportFiles: 86,
  hardcodedMotionValues: 0,
  brokenTransitionTokens: 0,
};

async function filesUnder(dir, suffix) {
  const result = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) result.push(...await filesUnder(full, suffix));
    else if (entry.name.endsWith(suffix)) result.push(full);
  }
  return result;
}

const cssFiles = await filesUnder(src, ".css");
let importantTotal = 0;
let hardcodedMotionValues = 0;
let brokenTransitionTokens = 0;
for (const file of cssFiles) {
  const text = await readFile(file, "utf8");
  importantTotal += text.match(/!important/g)?.length ?? 0;

  // Malformed transition token usage: var(--transition-*)-in or -out suffix
  // appends to an already-complete token (which includes easing), producing
  // invalid CSS like "150ms ease-in-out both" → drops the whole declaration.
  // Fixed 2026-07-25 (13 occurrences).
  const malformed = text.match(/var\(--transition-[a-z]+\)-(?:in|out)\b/g);
  if (malformed) {
    console.error(`${path.relative(src, file)}: malformed token usage ${malformed.join(", ")}`);
    brokenTransitionTokens += malformed.length;
  }

  // Motion tokens (2026-07-25): finite transition/animation durations must
  // use the --duration-*/--transition-*/--easing-* registry. Continuous
  // ambient loops (infinite spinners/pulses) and the documented
  // preview-flash attention exception are excluded.
  const declarations = text.match(/(?:transition|animation)\s*:[^;]+;/g) ?? [];
  for (const decl of declarations) {
    if (!/\b\d+(?:\.\d+)?m?s\b/.test(decl)) continue;
    if (decl.includes("var(--")) continue;
    if (decl.includes("infinite")) continue;
    if (decl.includes("preview-flash")) continue;
    hardcodedMotionValues += 1;
  }
}

const tsFiles = [
  ...(await filesUnder(src, ".tsx")),
  ...(await filesUnder(src, ".ts")),
];
let lucideDirectImportFiles = 0;
for (const file of tsFiles) {
  if (file.endsWith(`${path.sep}lib${path.sep}icons.ts`)) continue;
  const text = await readFile(file, "utf8");
  if (/from ["']lucide-react["']/.test(text)) lucideDirectImportFiles += 1;
}

const assets = await readdir(distAssets);
const tsWorker = assets.find((name) => name.startsWith("ts.worker-") && name.endsWith(".js"));
const mainJs = assets.find((name) => name.startsWith("index-") && name.endsWith(".js"));
if (!mainJs) throw new Error("Run npm run build before checking UI debt budgets.");

const measurements = {
  importantTotal,
  hardcodedMotionValues,
  brokenTransitionTokens,
  lucideDirectImportFiles,
  tsWorkerBytes: tsWorker ? (await stat(path.join(distAssets, tsWorker))).size : 0,
  mainJsBytes: (await stat(path.join(distAssets, mainJs))).size,
};

const failures = Object.entries(limits)
  .filter(([key, limit]) => measurements[key] > limit)
  .map(([key, limit]) => `${key}: ${measurements[key]} > ${limit}`);

console.log(JSON.stringify({ measurements, limits }, null, 2));
if (failures.length) {
  console.error(`UI debt budget exceeded:\n${failures.join("\n")}`);
  process.exitCode = 1;
}
