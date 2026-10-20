"use strict";

const fs = require("node:fs/promises");
const { execFile } = require("node:child_process");

// Enumerate registered font files, not synthesized Drawing.FontStyle variants.
const registeredFilesScript = `
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$paths = foreach ($key in @('HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Fonts','HKCU:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Fonts')) {
  if (Test-Path -LiteralPath $key) {
    $registry = Get-Item -LiteralPath $key
    foreach ($name in $registry.GetValueNames()) {
      $value = $registry.GetValue($name)
      if ($value -is [string]) {
        $fontPath = if ([IO.Path]::IsPathRooted($value)) { $value } else { Join-Path $env:WINDIR ('Fonts\\' + $value) }
        if (Test-Path -LiteralPath $fontPath -PathType Leaf) { $fontPath }
      }
    }
  }
}
ConvertTo-Json -Compress -InputObject @($paths | Sort-Object -Unique)
`;

function fontTables(buffer, offset) {
  const count = buffer.readUInt16BE(offset + 4);
  const tables = new Map();
  if (offset + 12 + count * 16 > buffer.length) throw new Error("Invalid font table directory");
  for (let index = 0; index < count; index++) {
    const entry = offset + 12 + index * 16;
    const name = buffer.toString("ascii", entry, entry + 4);
    const start = buffer.readUInt32BE(entry + 8), length = buffer.readUInt32BE(entry + 12);
    if (start + length > buffer.length) throw new Error(`Invalid ${name} font table`);
    tables.set(name, buffer.subarray(start, start + length));
  }
  return tables;
}

function namesFromTable(table) {
  const count = table.readUInt16BE(2), strings = table.readUInt16BE(4);
  const names = new Map();
  if (6 + count * 12 > table.length) throw new Error("Invalid font name records");
  for (let index = 0; index < count; index++) {
    const record = 6 + index * 12;
    const platform = table.readUInt16BE(record), language = table.readUInt16BE(record + 4);
    const id = table.readUInt16BE(record + 6), length = table.readUInt16BE(record + 8);
    const start = strings + table.readUInt16BE(record + 10);
    if (start + length > table.length) throw new Error("Invalid font name string");
    if (![1, 2, 4, 6, 16, 17].includes(id) || ![0, 1, 3].includes(platform)) continue;
    let text;
    if (platform === 1) text = new TextDecoder("macintosh").decode(table.subarray(start, start + length));
    else {
      const codes = [];
      for (let at = start; at < start + length; at += 2) codes.push(table.readUInt16BE(at));
      text = String.fromCharCode(...codes);
    }
    text = text.replace(/\0/g, "").trim();
    const score = (language === 0x0409 ? 8 : 0) + (platform === 3 ? 4 : platform === 0 ? 2 : 1);
    if (text && (!names.has(id) || names.get(id).score < score)) names.set(id, { text, score });
  }
  return new Map([...names].map(([key, value]) => [key, value.text]));
}

function fixedWidths(tables) {
  const post = tables.get("post");
  if (post && post.readUInt32BE(12) !== 0) return true;
  const hhea = tables.get("hhea"), metrics = tables.get("hmtx");
  if (!hhea || !metrics) return false;
  const count = hhea.readUInt16BE(34);
  if (count * 4 > metrics.length) throw new Error("Invalid font horizontal metrics");
  const widths = new Set();
  for (let index = 0; index < count; index++) {
    const width = metrics.readUInt16BE(index * 4);
    if (width > 0) widths.add(width);
  }
  return widths.size === 1;
}

function parseFontFaces(buffer) {
  const signature = buffer.toString("ascii", 0, 4);
  let offsets;
  if (signature === "ttcf") {
    const count = buffer.readUInt32BE(8);
    if (12 + count * 4 > buffer.length) throw new Error("Invalid font collection");
    offsets = Array.from({ length: count }, (_, index) => buffer.readUInt32BE(12 + index * 4));
  } else if (buffer.readUInt32BE(0) === 0x00010000 || ["OTTO", "true", "typ1"].includes(signature)) offsets = [0];
  else return [];
  return offsets.map(offset => {
    const tables = fontTables(buffer, offset), names = namesFromTable(tables.get("name"));
    const family = names.get(16) || names.get(1), styleName = names.get(17) || names.get(2);
    const fullName = names.get(4), postscriptName = names.get(6);
    if (!family || !styleName || !fullName || !postscriptName) throw new Error("Font face is missing its registered names");
    const os2 = tables.get("OS/2"), head = tables.get("head");
    const selection = os2 ? os2.readUInt16BE(62) : 0;
    return { family, fullName, postscriptName, styleName,
      weight: os2 ? os2.readUInt16BE(4) : head && (head.readUInt16BE(44) & 1) ? 700 : 400,
      style: selection & 0x200 ? "oblique" : selection & 1 || head && (head.readUInt16BE(44) & 2) ? "italic" : "normal",
      isMonospaced: fixedWidths(tables) };
  });
}

function groupFontFamilies(faces) {
  const grouped = new Map();
  for (const face of faces) {
    const key = face.family.toLocaleLowerCase("en-US");
    const family = grouped.get(key) || { family: face.family, faces: [] };
    if (!family.faces.some(item => item.postscriptName === face.postscriptName)) family.faces.push(face);
    grouped.set(key, family);
  }
  for (const family of grouped.values()) family.faces.sort((left, right) =>
    (left.style !== "normal") - (right.style !== "normal")
      || Number(/^(regular|normal|roman|book)$/i.test(right.styleName)) - Number(/^(regular|normal|roman|book)$/i.test(left.styleName))
      || Math.abs(left.weight - 400) - Math.abs(right.weight - 400)
      || left.weight - right.weight || left.styleName.localeCompare(right.styleName));
  return [...grouped.values()].sort((left, right) => left.family.localeCompare(right.family));
}

async function listFontFamilies() {
  if (process.platform !== "win32") return null;
  const files = await new Promise((resolve, reject) => execFile("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", registeredFilesScript],
    { windowsHide: true, encoding: "utf8", maxBuffer: 4 * 1024 * 1024 }, (error, stdout) => error ? reject(error) : resolve(JSON.parse(stdout))));
  const faces = [];
  for (const file of files) faces.push(...parseFontFaces(await fs.readFile(file)));
  return groupFontFamilies(faces);
}

module.exports = { listFontFamilies, parseFontFaces, groupFontFamilies };
