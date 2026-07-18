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
for (const file of cssFiles) {
  const text = await readFile(file, "utf8");
  importantTotal += text.match(/!important/g)?.length ?? 0;
}

const assets = await readdir(distAssets);
const tsWorker = assets.find((name) => name.startsWith("ts.worker-") && name.endsWith(".js"));
const mainJs = assets.find((name) => name.startsWith("index-") && name.endsWith(".js"));
if (!mainJs) throw new Error("Run npm run build before checking UI debt budgets.");

const measurements = {
  importantTotal,
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
