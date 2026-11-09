import { describe, expect, it } from "vitest";
import { agentIdentity } from "./agent-identity";

describe("agentIdentity", () => {
  it("does not depend on the order in which agents are displayed", () => {
    const keys = ["agent-research", "agent-editor", "agent-review", "agent-tests"];
    const original = new Map(keys.map((key) => [key, agentIdentity(key)]));
    for (const key of [...keys].reverse()) expect(agentIdentity(key)).toEqual(original.get(key));
  });

  it("gives a group of concurrent agents multiple distinguishable identities", () => {
    const identities = Array.from({ length: 12 }, (_, index) => agentIdentity(`agent-${index}`));
    expect(new Set(identities.map(({ index }) => index)).size).toBeGreaterThan(4);
    expect(new Set(identities.map(({ light }) => light)).size).toBeGreaterThan(4);
  });

  // Values executed directly from the installed desktop's pure qOc function.
  it.each([
    ["", 0], ["agent-research", 25], ["agent-editor", 23], ["agent-review", 18],
    ["agent-tests", 11], ["/root/files_browser_finish", 2], ["智能体-编辑", 8], ["agent-🚀", 17],
  ])("uses the actual desktop identity for %s", (key, index) => {
    expect(agentIdentity(String(key)).index).toBe(index);
  });
});
