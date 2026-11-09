const TWO_TOKEN_BINS = new Set([
  "npm", "pnpm", "yarn", "git", "cargo", "go", "python", "python3",
  "pip", "pip3", "npx", "docker", "kubectl", "make", "rustup", "uv",
]);

const UNSAFE_WRAPPERS = new Set([
  "bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "cmd", "cmd.exe",
  "powershell", "powershell.exe", "pwsh", "pwsh.exe", "env", "sudo",
  "doas", "pkexec", "xargs", "timeout", "nice", "nohup", "time", "stdbuf",
  "setsid", "command", "builtin",
]);

const hasUnquotedShellControl = (command: string): boolean => {
  let quote = "";
  let escaped = false;
  for (let index = 0; index < command.length; index += 1) {
    const char = command[index];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (char === "\\" && quote !== "'") {
      escaped = true;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = quote === char ? "" : quote || char;
      continue;
    }
    if (quote) continue;
    if (";|&`\n<>".includes(char)) return true;
    if (char === "$" && command[index + 1] === "(") return true;
  }
  return false;
};

/**
 * Derive a sensible "always allow" prefix from a shell command, so approving
 * `npm run build` suggests `npm run:*`. Two-token prefix for tools with
 * meaningful subcommands (npm/git/cargo/...), otherwise the binary alone.
 */
export const deriveCommandPrefix = (command: string): string => {
  const trimmed = String(command || "").trim().replace(/^\$\s*/, "");
  if (hasUnquotedShellControl(trimmed)) return "";
  const tokens = trimmed.split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return "";
  const bin = tokens[0];
  const baseBin = bin.replace(/^['"]|['"]$/g, "").split(/[\\/]/).pop()?.toLowerCase() ?? "";
  if (UNSAFE_WRAPPERS.has(baseBin)) return "";
  if (tokens.length >= 2 && TWO_TOKEN_BINS.has(bin)) return `${bin} ${tokens[1]}`;
  return bin;
};
