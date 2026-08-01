import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(root, "dist");
const assets = path.join(dist, "assets");
const limits = {
  entry: 900 * 1024,
  javascript: 1500 * 1024,
  css: 350 * 1024,
};

if (!fs.existsSync(assets)) {
  console.error("Bundle budget check requires frontend/dist. Run npm run build first.");
  process.exit(1);
}

const html = fs.readFileSync(path.join(dist, "index.html"), "utf8");
const entryMatch = html.match(/<script[^>]+type=["']module["'][^>]+src=["']\.\/assets\/([^"']+\.js)["']/i)
  || html.match(/<script[^>]+src=["']\.\/assets\/([^"']+\.js)["'][^>]+type=["']module["']/i);
const entryName = entryMatch?.[1] ?? "";
const failures = [];
const rows = [];

for (const name of fs.readdirSync(assets)) {
  if (!name.endsWith(".js") && !name.endsWith(".css")) continue;
  const bytes = fs.statSync(path.join(assets, name)).size;
  const limit = name === entryName ? limits.entry : name.endsWith(".js") ? limits.javascript : limits.css;
  rows.push({ name, bytes, limit });
  if (bytes > limit) failures.push({ name, bytes, limit });
}

rows.sort((a, b) => b.bytes - a.bytes);
console.log(`Entry: ${entryName || "not detected"}`);
for (const row of rows.slice(0, 12)) {
  console.log(`${row.name}: ${(row.bytes / 1024).toFixed(1)} KiB / ${(row.limit / 1024).toFixed(0)} KiB`);
}

if (failures.length) {
  console.error("Bundle budget exceeded:");
  for (const item of failures) {
    console.error(`- ${item.name}: ${(item.bytes / 1024).toFixed(1)} KiB > ${(item.limit / 1024).toFixed(0)} KiB`);
  }
  process.exit(1);
}

console.log("Bundle budget check passed.");
