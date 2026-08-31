import { describe, expect, it } from "vitest";
import { assessNetworkTargetUrl } from "./network-target";

describe("assessNetworkTargetUrl", () => {
  it("normalizes public http targets without review", () => {
    const result = assessNetworkTargetUrl("example.com/docs");

    expect(result.normalizedUrl).toBe("http://example.com/docs");
    expect(result.risk).toBe("public");
    expect(result.requiresReview).toBe(false);
  });

  it("requires review for localhost targets", () => {
    const result = assessNetworkTargetUrl("http://127.0.0.1:3000");

    expect(result.host).toBe("127.0.0.1");
    expect(result.risk).toBe("local");
    expect(result.requiresReview).toBe(true);
  });

  it("treats the full 127.0.0.0/8 range as local", () => {
    const result = assessNetworkTargetUrl("http://127.42.0.7:3000");

    expect(result.risk).toBe("local");
    expect(result.requiresReview).toBe(true);
  });

  it("requires review for private network targets", () => {
    const result = assessNetworkTargetUrl("https://192.168.1.10/app");

    expect(result.host).toBe("192.168.1.10");
    expect(result.risk).toBe("private");
    expect(result.requiresReview).toBe(true);
  });

  it("rejects non-http protocols", () => {
    const result = assessNetworkTargetUrl("file:///C:/Windows/System32/drivers/etc/hosts");

    expect(result.risk).toBe("invalid");
    expect(result.reason).toContain("http(s)");
  });
});
