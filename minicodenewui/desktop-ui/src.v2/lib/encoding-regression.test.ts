import { readdirSync, readFileSync, statSync } from "node:fs";
import { extname, join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SOURCE_ROOT = resolve(process.cwd(), "src.v2");
const SOURCE_EXTENSIONS = new Set([".ts", ".tsx", ".css"]);
const SKIP_DIRECTORIES = new Set(["node_modules", "dist", "build"]);

const SUSPICIOUS_CODEPOINTS = new Map<number, string>([
  [0xfffd, "replacement character"],
  [0x00c2, "latin-1 mojibake prefix"],
  [0x00c3, "latin-1 mojibake prefix"],
  [0x00e2, "utf-8 mojibake prefix"],
  [0x9225, "CJK mojibake fragment"],
  [0x9239, "CJK mojibake fragment"],
  [0x93b4, "CJK mojibake fragment"],
  [0x7487, "CJK mojibake fragment"],
  [0x6769, "CJK mojibake fragment"],
  [0x9286, "CJK mojibake fragment"],
  [0x93bc, "CJK mojibake fragment"],
  [0x953b, "CJK mojibake fragment"],
]);

function sourceFiles(dir: string): string[] {
  const files: string[] = [];
  for (const name of readdirSync(dir)) {
    if (SKIP_DIRECTORIES.has(name)) continue;
    const path = join(dir, name);
    const stat = statSync(path);
    if (stat.isDirectory()) {
      files.push(...sourceFiles(path));
    } else if (SOURCE_EXTENSIONS.has(extname(path))) {
      files.push(path);
    }
  }
  return files;
}

function printableSnippet(line: string): string {
  return [...line]
    .map((char) => {
      const codepoint = char.codePointAt(0) ?? 0;
      if (codepoint < 0x20 || codepoint > 0x7e) {
        return `\\u${codepoint.toString(16).padStart(4, "0")}`;
      }
      return char;
    })
    .join("");
}

describe("source encoding", () => {
  it("does not contain known mojibake fragments", () => {
    const findings: string[] = [];

    for (const file of sourceFiles(SOURCE_ROOT)) {
      const lines = readFileSync(file, "utf8").split(/\r?\n/);
      lines.forEach((line, index) => {
        for (const char of line) {
          const codepoint = char.codePointAt(0) ?? 0;
          const reason = SUSPICIOUS_CODEPOINTS.get(codepoint);
          if (!reason) continue;
          findings.push(
            `${relative(process.cwd(), file)}:${index + 1}: U+${codepoint.toString(16).toUpperCase()} ${reason}: ${printableSnippet(line.slice(0, 180))}`,
          );
          break;
        }
      });
    }

    expect(findings).toEqual([]);
  });
});
