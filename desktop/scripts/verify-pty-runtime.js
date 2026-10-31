"use strict";

const process = require("node:process");
const path = require("node:path");
const pty = process.platform === "win32"
  ? require("../windows-pty").createWindowsPty(path.join(__dirname, "..", "windows-sandbox-runtime", "codex.exe"))
  : require("node-pty");

const marker = "MINICODE_NODE_PTY_RUNTIME_OK";
const isWindows = process.platform === "win32";
const shell = isWindows ? process.env.ComSpec || "cmd.exe" : process.env.SHELL || "/bin/sh";
const args = isWindows
  ? ["/d", "/s", "/c", `echo ${marker}`]
  : ["-c", `printf '%s\\n' '${marker}'`];

let output = "";
let settled = false;

const terminal = pty.spawn(shell, args, {
  cols: 80,
  rows: 24,
  cwd: process.cwd(),
  env: process.env,
});

const timeout = setTimeout(() => {
  if (settled) return;
  settled = true;
  try {
    terminal.kill();
  } catch {
    // The process may already have exited while the timeout callback was queued.
  }
  console.error("Native PTY runtime verification timed out");
  process.exit(1);
}, 10_000);

terminal.onData((data) => {
  if (output.length < 64 * 1024) output += data;
});

terminal.onExit(({ exitCode }) => {
  if (settled) return;
  settled = true;
  clearTimeout(timeout);

  if (exitCode !== 0 || !output.includes(marker)) {
    console.error(
      `Native PTY runtime verification failed (exit=${exitCode}, marker=${output.includes(marker)})`,
    );
    process.exit(1);
  }

  console.log(
    `Native PTY runtime verified with Electron ${process.versions.electron} / Node ${process.versions.node}`,
  );
  // Success must release the actual owner so this process exits naturally.
});
