const GLYPHS = ["orbit", "petal", "facets", "links", "spark", "bloom"] as const;
const COLORS = ["teal", "blue", "violet", "rose", "amber", "green"] as const;

export type AgentIdentityGlyph = typeof GLYPHS[number];
export type AgentIdentityColor = typeof COLORS[number];

/** An agent keeps its identity when its title, status or position changes. */
export function agentIdentity(identityKey: string): { glyph: AgentIdentityGlyph; color: AgentIdentityColor } {
  let hash = 2166136261;
  for (const character of identityKey) {
    hash = Math.imul(hash ^ character.codePointAt(0)!, 16777619);
  }
  const value = hash >>> 0;
  return {
    glyph: GLYPHS[value % GLYPHS.length],
    color: COLORS[(value >>> 8) % COLORS.length],
  };
}
