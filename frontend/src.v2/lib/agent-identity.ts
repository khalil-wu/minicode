import { AGENT_AVATAR_ART } from "./agent-avatar-assets";

/** An agent keeps its identity when its title, status or position changes. */
export function agentIdentity(identityKey: string): { index: number; light: string; dark: string } {
  // Codex desktop KOc/qOc: use UTF-16 code units and its full 28-art palette.
  let hash = 0;
  for (let index = 0; index < identityKey.length; index += 1) {
    hash = (hash * 31 + identityKey.charCodeAt(index)) % 2147483647;
  }
  const index = hash % AGENT_AVATAR_ART.length;
  return { index, ...AGENT_AVATAR_ART[index] };
}
