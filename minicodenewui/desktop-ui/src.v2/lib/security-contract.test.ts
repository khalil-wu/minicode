import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

describe("frontend security contract", () => {
  it("declares a CSP for the app shell", () => {
    const indexPath = path.resolve(__dirname, "..", "..", "index.html");
    const html = fs.readFileSync(indexPath, "utf8");

    expect(html).toContain("Content-Security-Policy");
    expect(html).toContain("base-uri 'self'");
    expect(html).toContain("object-src 'none'");
    expect(html).toContain("img-src 'self' data: blob: https: http://localhost:* http://127.0.0.1:*");
    expect(html).not.toContain("frame-ancestors");
    expect(html).toContain("worker-src 'self' blob:");
    expect(html).toContain("script-src 'self'");
    expect(html).toContain('<script type="module" src="/theme-boot.js"></script>');
    expect(html).not.toContain("script-src 'unsafe-inline'");
  });
});
