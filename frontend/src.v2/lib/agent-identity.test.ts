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
    expect(new Set(identities.map(({ glyph, color }) => `${glyph}:${color}`)).size).toBeGreaterThan(4);
    expect(new Set(identities.map(({ glyph }) => glyph)).size).toBeGreaterThan(1);
    expect(new Set(identities.map(({ color }) => color)).size).toBeGreaterThan(1);
  });
});
