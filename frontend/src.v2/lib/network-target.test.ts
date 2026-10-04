import { describe, expect, it } from "vitest";
import { parseHttpUrl } from "./network-target";

describe("browser HTTP input", () => {
  it.each([
    ["http://127.42.0.7:3000", "127.42.0.7"],
    ["https://fda.gov/docs", "fda.gov"],
    ["http://[::ffff:192.168.1.10]/app", "[::ffff:c0a8:10a]"],
    ["http://[fe90::1]/app", "[fe90::1]"],
  ])("preserves valid target %s for the desktop permission owner", (url, hostname) => {
    expect(parseHttpUrl(url)?.hostname).toBe(hostname);
  });

  it.each(["file:///C:/private", "https://user:password@example.test", "http://example.test:invalid", ""])(
    "rejects invalid browser input %s", (url) => expect(parseHttpUrl(url)).toBeNull(),
  );
});
