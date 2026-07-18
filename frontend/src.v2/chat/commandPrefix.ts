const TWO_TOKEN_BINS = new Set([
  "npm", "pnpm", "yarn", "git", "cargo", "go", "python", "python3",
  "pip", "pip3", "npx", "docker", "kubectl", "make", "rustup", "uv",
]);

/**
 * Derive a sensible "always allow" prefix from a shell command, so approving
 * `npm run build` suggests `npm run:*`. Two-token prefix for tools with
 * meaningful subcommands (npm/git/cargo/...), otherwise the binary alone.
 */
export const deriveCommandPrefix = (command: string): string => {
  const trimmed = String(command || "").trim().replace(/^\$\s*/, "");
  const tokens = trimmed.split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return "";
  const bin = tokens[0];
  if (tokens.length >= 2 && TWO_TOKEN_BINS.has(bin)) return `${bin} ${tokens[1]}`;
  return bin;
};
