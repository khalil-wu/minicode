"use strict";

const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawn } = require("node:child_process");
const { shell } = require("electron");

// Windows targets and launch arguments follow Codex 26.1007.2314's
// open-in-targets catalog. Applications without a win32 entry stay out of it.
const localAppData = process.env.LOCALAPPDATA ?? path.join(os.homedir(), "AppData", "Local");
const programRoots = [path.join(localAppData, "Programs"), process.env.ProgramFiles, process.env["ProgramFiles(x86)"]].filter(Boolean);
const windowsApps = path.join(localAppData, "Microsoft", "WindowsApps");
const shortcutRoots = [
  process.env.APPDATA && path.join(process.env.APPDATA, "Microsoft", "Windows", "Start Menu", "Programs"),
  process.env.ProgramData && path.join(process.env.ProgramData, "Microsoft", "Windows", "Start Menu", "Programs"),
].filter(Boolean);

const exists = file => new Promise(resolve => fs.exists(file, resolve));
async function firstExisting(paths) {
  for (const file of paths) if (await exists(file)) return file;
  return null;
}

function runProbe(command, args, encoding = "utf8") {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    const chunks = [];
    child.stdout.on("data", chunk => chunks.push(chunk));
    child.stderr.resume();
    child.once("error", reject);
    child.once("close", status => resolve({ status, stdout: Buffer.concat(chunks).toString(encoding) }));
  });
}

async function commandPath(...names) {
  const directories = (process.env.Path ?? process.env.PATH ?? "").split(path.delimiter).filter(Boolean);
  const extensions = (process.env.PATHEXT ?? ".COM;.EXE;.BAT;.CMD").split(";");
  for (const name of names) for (const directory of directories) {
    for (const suffix of path.extname(name) ? [""] : ["", ...extensions]) {
      const candidate = path.join(directory.replace(/^"|"$/g, ""), `${name}${suffix}`);
      if (await exists(candidate)) return candidate;
    }
  }
  return null;
}

function installedPath(paths) {
  return firstExisting(programRoots.flatMap(root => paths.map(parts => path.join(root, ...parts))));
}

function executableBeside(command, name) {
  const directory = path.dirname(command);
  const candidates = path.basename(directory).toLowerCase() === "bin"
    ? [path.join(path.dirname(directory), name), path.join(directory, name)]
    : [path.join(directory, name)];
  return firstExisting(candidates);
}

async function codeExecutable(command, name, directory) {
  const found = await commandPath(command);
  if (found) return await executableBeside(found, name) ?? (path.basename(found).toLowerCase() === name.toLowerCase() ? found : null);
  return installedPath([[directory, name]]);
}

async function findShortcut(names) {
  const wanted = new Set(names.map(name => name.toLowerCase()));
  const find = async directory => {
    if (!await exists(directory)) return null;
    for (const entry of await fs.promises.readdir(directory, { withFileTypes: true })) {
      const candidate = path.join(directory, entry.name);
      if (entry.isFile() && wanted.has(entry.name.toLowerCase())) return candidate;
      if (entry.isDirectory()) {
        const result = await find(candidate);
        if (result) return result;
      }
    }
    return null;
  };
  for (const root of shortcutRoots) {
    const result = await find(root);
    if (result) return result;
  }
  return null;
}

async function terminalExecutable() {
  const direct = await commandPath("wt.exe") ?? path.join(windowsApps, "wt.exe");
  if (await exists(direct)) return direct;
  const shortcut = await findShortcut(["Terminal.lnk", "Windows Terminal.lnk"]);
  return shortcut ? shell.readShortcutLink(shortcut).target : null;
}

async function cursorExecutable() {
  const command = await commandPath("cursor");
  if (command) {
    if (path.basename(command).toLowerCase() === "cursor.exe") return command;
    const directory = path.dirname(command);
    if (path.basename(directory).toLowerCase() === "bin"
      && path.basename(path.dirname(directory)).toLowerCase() === "app"
      && path.basename(path.dirname(path.dirname(directory))).toLowerCase() === "resources") {
      const executable = path.join(directory, "..", "..", "..", "Cursor.exe");
      if (await exists(executable)) return executable;
    }
  }
  return installedPath([["Cursor", "Cursor.exe"]]);
}

async function visualStudioExecutable() {
  const command = await commandPath("devenv.exe", "devenv");
  if (command) return command;
  const installed = await installedPath(["2022", "2019", "2017"].flatMap(version =>
    ["Community", "Professional", "Enterprise", "Preview", "BuildTools"].map(edition =>
      ["Microsoft Visual Studio", version, edition, "Common7", "IDE", "devenv.exe"])));
  if (installed) return installed;
  const finder = await installedPath([["Microsoft Visual Studio", "Installer", "vswhere.exe"]]);
  if (!finder) return null;
  const result = await runProbe(finder, ["-products", "*", "-all", "-find", "**\\Common7\\IDE\\devenv.exe", "-utf8", "-nologo", "-prerelease"]);
  return result.status === 0 ? firstExisting(result.stdout.split(/\r?\n/).map(file => file.trim()).filter(Boolean)) : null;
}

async function jetBrainsExecutable(commands, prefixes, executables, extraPaths = []) {
  const command = await commandPath(...commands);
  if (command) return command;
  for (const root of [process.env.ProgramFiles, process.env["ProgramFiles(x86)"]].filter(Boolean)) {
    const directory = path.join(root, "JetBrains");
    if (!await exists(directory)) continue;
    for (const entry of (await fs.promises.readdir(directory)).sort((a, b) => b.localeCompare(a))) {
      if (!prefixes.some(prefix => entry.toLowerCase().startsWith(prefix))) continue;
      const executable = await firstExisting(executables.map(name => path.join(directory, entry, "bin", name)));
      if (executable) return executable;
    }
  }
  return installedPath(extraPaths);
}

async function wslInstalled() {
  const command = await commandPath("wsl.exe");
  if (!command) return false;
  const status = await runProbe(command, ["--status"]);
  if (status.status !== 0) return false;
  const list = await runProbe(command, ["--list", "--verbose"], "utf16le");
  if (list.status !== 0) return false;
  if (list.stdout.split(/\r?\n/).some(line => line.trim().startsWith("*"))) return true;
  const quiet = await runProbe(command, ["--list", "--quiet"], "utf16le");
  return quiet.status === 0 && quiet.stdout.split(/\r?\n/).some(line => line.trim().toLowerCase().startsWith("ubuntu"));
}

function containingDirectory(file) { return fs.statSync(file).isDirectory() ? file : path.dirname(file); }
const codeArgs = (file, line, column = 1) => line == null ? [file] : ["--goto", `${file}:${line}:${column}`];
const locationArgs = (file, line, column = 1) => [line == null ? file : `${file}:${line}:${column}`];
const jetBrainsArgs = (file, line, column = 1) => line == null ? [file] : ["--line", String(line), "--column", String(column), file];

const windowsTargets = [
  { id: "vscode", label: "VS Code", icon: "vscode.png", kind: "editor", detect: () => codeExecutable("code", "Code.exe", "Microsoft VS Code"), args: codeArgs },
  { id: "vscodeInsiders", label: "VS Code Insiders", icon: "vscode-insiders.png", kind: "editor", detect: () => codeExecutable("code-insiders", "Code - Insiders.exe", "Microsoft VS Code Insiders"), args: codeArgs },
  { id: "visualStudio", label: "Visual Studio", icon: "vscode.png", kind: "editor", detect: visualStudioExecutable, args: file => [file] },
  { id: "cursor", label: "Cursor", icon: "cursor.png", kind: "editor", detect: cursorExecutable, args: codeArgs },
  { id: "sublimeText", label: "Sublime Text", icon: "sublime-text.png", kind: "editor", detect: async () => {
    const command = await commandPath("subl.exe", "subl");
    return command ? await executableBeside(command, "sublime_text.exe") ?? command : installedPath([["Sublime Text", "sublime_text.exe"], ["Sublime Text", "subl.exe"]]);
  }, args: locationArgs },
  { id: "zed", label: "Zed", icon: "zed.png", kind: "editor", detect: async () => await commandPath("zed.exe", "zed") ?? installedPath([["Zed", "Zed.exe"]]), args: locationArgs },
  { id: "antigravity", label: "Antigravity", icon: "antigravity.png", kind: "editor", detect: async () => await commandPath("antigravity.exe", "antigravity.cmd", "antigravity")
    ?? installedPath(["Antigravity", "antigravity"].flatMap(directory => [[directory, "Antigravity.exe"], [directory, "bin", "antigravity.cmd"], [directory, "resources", "app", "bin", "antigravity.cmd"]])), iconPath: () => null, args: codeArgs },
  { id: "githubDesktop", label: "GitHub Desktop", icon: "vscode.png", kind: "editor", detect: async () => {
    const command = await commandPath("github.exe", "github");
    if (command) return await executableBeside(command, "GitHubDesktop.exe") ?? command;
    return await firstExisting([path.join(localAppData, "GitHubDesktop", "GitHubDesktop.exe"), path.join(localAppData, "GitHub Desktop", "GitHubDesktop.exe")])
      ?? installedPath([["GitHub Desktop", "GitHubDesktop.exe"], ["GitHubDesktop", "GitHubDesktop.exe"]]);
  }, args: file => [containingDirectory(file)] },
  { id: "fileManager", label: "File Explorer", icon: "file-explorer.png", kind: "fileManager", detect: () => path.join(process.env.SystemRoot ?? process.env.windir, "explorer.exe") },
  { id: "terminal", label: "Terminal", icon: "microsoft-terminal.png", kind: "terminal", detect: terminalExecutable, iconPath: () => null, detached: true, args: file => ["-d", containingDirectory(file)] },
  { id: "gitBash", label: "Git Bash", icon: "vscode.png", kind: "terminal", detect: async () => {
    const command = await commandPath("git-bash.exe", "git-bash");
    return command ? await executableBeside(command, "git-bash.exe") ?? command : installedPath([["Git", "git-bash.exe"], ["Git", "bin", "bash.exe"]]);
  }, iconPath: async executable => await findShortcut(["Git Bash.lnk"]) ?? await installedPath([["Git", "git-bash.exe"]]) ?? executable, args: file => [`--cd=${containingDirectory(file)}`] },
  { id: "cmder", label: "Cmder", icon: "cmder.png", kind: "terminal", detect: async () => {
    const root = process.env.CMDER_ROOT?.trim();
    if (root && await exists(path.join(root, "Cmder.exe"))) return path.join(root, "Cmder.exe");
    const command = await commandPath("cmder.exe", "cmder");
    if (command) return command;
    const shortcut = await findShortcut(["Cmder.lnk"]);
    return shortcut ? shell.readShortcutLink(shortcut).target : null;
  }, args: file => ["/START", containingDirectory(file)] },
  { id: "wsl", label: "WSL", icon: "terminal.png", kind: "terminal", detect: async () => await wslInstalled() ? terminalExecutable() : null,
    iconPath: async () => await findShortcut(["WSL.lnk"]) ?? firstExisting([path.join(windowsApps, "wsl.exe")]), detached: true, args: file => {
      const directory = containingDirectory(file);
      const distro = directory.match(/^(?:\\\\|\/\/)(?:\?[\\/]UNC[\\/])?(?:wsl\$|wsl\.localhost)[\\/]([^\\/]+)/i)?.[1];
      return ["-d", directory, "wsl.exe", ...(distro ? ["-d", distro] : [])];
    } },
  ...[
    ["androidStudio", "Android Studio", "android-studio.png", "studio", ["android studio"], [["Android", "Android Studio", "bin", "studio64.exe"], ["Android", "Android Studio", "bin", "studio.exe"]]],
    ["intellij", "IntelliJ IDEA", "intellij.png", "idea", ["intellij idea", "idea"], []],
    ["rider", "Rider", "rider.png", "rider", ["rider"], []],
    ["pycharm", "PyCharm", "pycharm.png", "pycharm", ["pycharm"], []],
    ["webstorm", "WebStorm", "webstorm.svg", "webstorm", ["webstorm"], []],
    ["phpstorm", "PhpStorm", "phpstorm.png", "phpstorm", ["phpstorm"], []],
  ].map(([id, label, icon, command, prefixes, extraPaths]) => ({ id, label, icon, kind: "editor", args: jetBrainsArgs,
    detect: () => jetBrainsExecutable([`${command}64.exe`, `${command}.exe`, command], prefixes, [`${command}64.exe`, `${command}.exe`], extraPaths) })),
];

const FILE_OPEN_TARGET_IDS = ["minicode", "system", ...windowsTargets.map(target => target.id)];

async function fileOpenTarget(id) {
  if (id === "minicode") return { id, label: "MiniCode", kind: "editor", hidden: true };
  if (id === "system") return { id, label: "Default app", kind: "systemDefault", hidden: true, icon: "apps/file-explorer.png" };
  if (process.platform !== "win32") return null;
  const target = windowsTargets.find(target => target.id === id);
  if (!target) return null;
  const executable = await target.detect();
  return executable ? { ...target, executable, icon: `apps/${target.icon}`, iconPath: target.iconPath ? await target.iconPath(executable) : executable } : null;
}

async function fileOpenTargets() {
  const ids = process.platform === "win32"
    ? ["minicode", ...windowsTargets.flatMap(target => target.id === "fileManager" ? ["system", target.id] : [target.id])]
    : ["minicode", "system"];
  return (await Promise.all(ids.map(fileOpenTarget))).filter(Boolean);
}

async function openInTarget(target, file, line, column) {
  if (target.id === "fileManager") {
    if (fs.statSync(file).isFile()) { shell.showItemInFolder(file); return; }
    const error = await shell.openPath(file);
    if (error) throw new Error(error);
    return;
  }
  if (target.id === "system") {
    const error = await shell.openPath(file);
    if (error) throw new Error(error);
    return;
  }
  const args = target.args(file, line, column);
  if (target.detached) {
    await new Promise((resolve, reject) => {
      const child = spawn(target.executable, args.map(argument => argument.replaceAll(";", "\\;")), { detached: true, stdio: "ignore" });
      child.once("error", reject);
      child.once("spawn", () => { child.unref(); resolve(); });
    });
    return;
  }
  const batch = /\.(cmd|bat)$/i.test(target.executable);
  await new Promise((resolve, reject) => {
    const child = spawn(batch ? process.env.ComSpec ?? "cmd.exe" : target.executable, batch ? ["/d", "/s", "/c", target.executable, ...args] : args, { stdio: "ignore", windowsHide: true });
    child.once("error", reject);
    child.once("close", code => code === 0 ? resolve() : reject(new Error(`${target.label} exited with code ${code}`)));
  });
}

module.exports = { FILE_OPEN_TARGET_IDS, fileOpenTarget, fileOpenTargets, openInTarget };
