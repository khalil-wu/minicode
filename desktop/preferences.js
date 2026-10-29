"use strict";

const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { atomicWriteTextSync } = require("./utils");
const { FILE_OPEN_TARGET_IDS, fileOpenTarget, fileOpenTargets } = require("./open-targets");
const { isNotificationSound } = require("./notification-sounds");

const DEFAULT_PREFERENCES = Object.freeze({
  fileOpenTarget: "minicode",
  fileOpenTargetPerPath: {},
  defaultTaskDirectory: path.join(os.homedir(), "Documents", "MiniCode"),
  defaultPermissionMode: "confirm",
  showFullAccess: true,
  showContextWindowUsage: false,
  enabledReasoningEfforts: ["low", "medium", "high", "xhigh", "ultra", "persistent"],
  localeOverride: null,
  terminalShell: process.platform === "win32" ? "powershell" : "system",
  confirmBeforeClose: "keyboardOnly",
  turnCompletionNotifications: "onlyWhenUnfocused",
  permissionNotifications: true,
  questionNotifications: true,
  notificationSound: "default",
  autoArchiveAfterDays: 0,
  localHistoryCompression: false,
  showBottomPanel: true,
  defaultWorkspaceFullView: false,
  popoutWindowShortcut: null,
  popoutWindowProjectlessDefault: false,
  confettiEnabled: false,
  defaultTerminalLocation: "bottom",
  composerPlainTextMode: false,
  urlOpenTarget: "external-browser",
  localUrlOpenTarget: "in-app-browser",
  showFullUrl: false,
  annotationScreenshotsMode: "always",
  primaryNumberShortcutTarget: "tabs",
  externalBrowserLinkShortcut: "primary",
  speechEnabled: false,
  speechProvider: "",
  speechModel: "",
  speechLanguage: "zh",
  microphoneId: "",
  dictationDictionary: [],
  dictationMigratedPartitions: [],
  dictationConfiguredFields: [],
});
const OPTIONS = {
  fileOpenTarget: FILE_OPEN_TARGET_IDS,
  defaultPermissionMode: ["confirm", "plan", "auto", "bypass"],
  showFullAccess: [true, false],
  showContextWindowUsage: [true, false],
  localeOverride: [null, "en-US", "zh-CN"],
  terminalShell: ["powershell", "pwsh", "cmd", "gitBash", "wsl", "system"],
  confirmBeforeClose: ["never", "keyboardOnly", "always"],
  turnCompletionNotifications: ["never", "onlyWhenUnfocused", "always"],
  permissionNotifications: [true, false],
  questionNotifications: [true, false],
  notificationSound: ["default", "classic", "none"],
  autoArchiveAfterDays: [0, 1, 7, 14, 30, 90],
  localHistoryCompression: [true, false],
  showBottomPanel: [true, false],
  defaultWorkspaceFullView: [true, false],
  popoutWindowProjectlessDefault: [true, false],
  confettiEnabled: [true, false],
  defaultTerminalLocation: ["bottom", "right"],
  composerPlainTextMode: [true, false],
  urlOpenTarget: ["in-app-browser", "external-browser"],
  localUrlOpenTarget: ["in-app-browser", "external-browser"],
  showFullUrl: [true, false],
  annotationScreenshotsMode: ["always", "necessary"],
  primaryNumberShortcutTarget: ["tabs", "sidebar"],
  externalBrowserLinkShortcut: ["unassigned", "primary", "alt", "primaryShift"],
  speechEnabled: [true, false],
  speechProvider: ["", "openai", "anthropic", "custom"],
};
const DICTATION_SERVICE_KEYS = ["speechEnabled", "speechProvider", "speechModel", "speechLanguage", "microphoneId"];
const DICTATION_KEYS = [...DICTATION_SERVICE_KEYS, "dictationDictionary"];
const isDictationValue = (key, value) => ["speechModel", "speechLanguage", "microphoneId"].includes(key) ? typeof value === "string"
  : key === "dictationDictionary" ? Array.isArray(value) && value.every(entry => typeof entry === "string")
  : OPTIONS[key]?.includes(value);
let preferencesFile = "";
let defaultTaskDirectory = DEFAULT_PREFERENCES.defaultTaskDirectory;
let preferences = { ...DEFAULT_PREFERENCES, defaultTaskDirectory: null };

function init(file, defaults = {}) {
  preferencesFile = file;
  defaultTaskDirectory = defaults.defaultTaskDirectory ?? DEFAULT_PREFERENCES.defaultTaskDirectory;
  preferences = { ...DEFAULT_PREFERENCES, ...defaults, defaultTaskDirectory: null,
    ...(fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {}) };
}

function getPreferences() {
  return { ...preferences, defaultTaskDirectory: preferences.defaultTaskDirectory ?? defaultTaskDirectory,
    taskDirectoryOverride: preferences.defaultTaskDirectory };
}

function setPreferences(patch) {
  for (const [key, value] of Object.entries(patch)) {
    if (key === "popoutWindowShortcut" && (value === null || typeof value === "string" && value.trim())) continue;
    if (key === "defaultTaskDirectory" && (value === null || typeof value === "string" && path.isAbsolute(value))) continue;
    if (key === "notificationSound" && isNotificationSound(value)) continue;
    if (key === "enabledReasoningEfforts" && Array.isArray(value)
      && value.every(level => ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "persistent"].includes(level))) continue;
    if (DICTATION_KEYS.includes(key) && isDictationValue(key, value)) continue;
    if (!OPTIONS[key]?.includes(value)) throw new Error(`Unsupported desktop preference: ${key}`);
  }
  const next = { ...preferences, ...patch,
    dictationConfiguredFields: [...new Set([...preferences.dictationConfiguredFields, ...DICTATION_SERVICE_KEYS.filter(key => Object.hasOwn(patch, key))])] };
  atomicWriteTextSync(preferencesFile, `${JSON.stringify(next, null, 2)}\n`);
  preferences = next;
  return getPreferences();
}

function migrateDictationPreferences(partition, legacy) {
  if (preferences.dictationMigratedPartitions.includes(partition)) return getPreferences();
  if (Object.keys(legacy).length !== DICTATION_KEYS.length || DICTATION_KEYS.some(key => !isDictationValue(key, legacy[key]))) {
    throw new Error("Invalid legacy dictation preferences");
  }
  const next = { ...preferences,
    dictationDictionary: [...new Set([...preferences.dictationDictionary, ...legacy.dictationDictionary])],
    dictationMigratedPartitions: [...preferences.dictationMigratedPartitions, partition],
  };
  if (partition === "local") {
    for (const key of DICTATION_SERVICE_KEYS) {
      if (!preferences.dictationConfiguredFields.includes(key)) next[key] = legacy[key];
    }
  }
  atomicWriteTextSync(preferencesFile, `${JSON.stringify(next, null, 2)}\n`);
  preferences = next;
  return getPreferences();
}

function preferredFileOpenTarget(workspaceRoot) {
  return (workspaceRoot ? preferences.fileOpenTargetPerPath[path.resolve(workspaceRoot)] : undefined) ?? preferences.fileOpenTarget;
}

function setPreferredFileOpenTarget(workspaceRoot, targetId) {
  const next = { ...preferences, fileOpenTarget: targetId,
    fileOpenTargetPerPath: { ...preferences.fileOpenTargetPerPath, [path.resolve(workspaceRoot)]: targetId } };
  atomicWriteTextSync(preferencesFile, `${JSON.stringify(next, null, 2)}\n`);
  preferences = next;
}

function terminalExecutableOnPath(name) {
  if (path.isAbsolute(name)) return fs.existsSync(name) ? name : null;
  const directories = (process.env.Path ?? process.env.PATH ?? "").split(path.delimiter).filter(Boolean);
  for (const directory of directories) {
    const executable = path.join(directory.replace(/^"|"$/g, ""), name);
    if (fs.existsSync(executable)) return executable;
  }
  return null;
}

function sourcePowerShell() {
  return terminalExecutableOnPath("pwsh.exe") ?? terminalExecutableOnPath("powershell.exe");
}

function commandPrompt() {
  return terminalExecutableOnPath(process.env.COMSPEC?.trim() || "cmd.exe");
}

function gitBash() {
  const launcher = terminalExecutableOnPath("git-bash.exe");
  if (launcher) return launcher;
  const git = terminalExecutableOnPath("git.exe");
  if (git) {
    const directory = path.dirname(git);
    for (const candidate of [path.join(directory, "bash.exe"), path.join(path.dirname(directory), "bin", "bash.exe")]) {
      if (fs.existsSync(candidate)) return candidate;
    }
  }
  const localAppData = process.env.LOCALAPPDATA ?? path.join(os.homedir(), "AppData", "Local");
  for (const root of [path.join(localAppData, "Programs"), process.env.ProgramFiles, process.env["ProgramFiles(x86)"]].filter(Boolean)) {
    const executable = path.join(root, "Git", "bin", "bash.exe");
    if (fs.existsSync(executable)) return executable;
  }
  return null;
}

function explicitPwsh() {
  // Preserve the previous explicit pwsh selection's installed 7.x command;
  // a PATH pwsh is still the same shell intent, a powershell.exe is not.
  if (process.env.ProgramFiles) {
    const installed = path.join(process.env.ProgramFiles, "PowerShell", "7", "pwsh.exe");
    if (fs.existsSync(installed)) return installed;
  }
  return terminalExecutableOnPath("pwsh.exe");
}

function terminalShellLabel(executable) {
  const name = path.basename(executable).toLowerCase();
  if (["powershell.exe", "pwsh.exe"].includes(name)) return "PowerShell";
  if (name === "cmd.exe") return "Command Prompt";
  if (name === "wsl.exe") return "WSL";
  if (name === "git-bash.exe" || name === "bash.exe" && /[\\/]Git[\\/]/i.test(executable)) return "Git Bash";
  return name.replace(/\.exe$/i, "");
}

function terminalShellTargets() {
  if (process.platform !== "win32") {
    const executable = process.env.SHELL || "bash";
    return [{ id: "system", label: executable, executable, args: [] }];
  }
  const targets = [
    { id: "powershell", label: "PowerShell", executable: sourcePowerShell(), args: [] },
    { id: "cmd", label: "Command Prompt", executable: commandPrompt(), args: [] },
    { id: "gitBash", label: "Git Bash", executable: gitBash(), args: ["--login", "-i"] },
    { id: "wsl", label: "WSL", executable: terminalExecutableOnPath("wsl.exe"), args: [] },
  ].filter(target => target.executable !== null);
  // Source has four ordinary Windows choices. These existing explicit values
  // remain displayable only while selected and backed by their actual shell.
  if (preferences.terminalShell === "pwsh") {
    const executable = explicitPwsh();
    if (executable) targets.push({ id: "pwsh", label: "PowerShell (pwsh)", executable, args: [] });
  }
  if (preferences.terminalShell === "system") {
    const executable = process.env.SHELL ? terminalExecutableOnPath(process.env.SHELL) : commandPrompt();
    if (executable) targets.push({ id: "system", label: terminalShellLabel(executable), executable, args: [] });
  }
  return targets;
}

function resolveTerminalShell() {
  const targets = terminalShellTargets();
  const selected = targets.find(target => target.id === preferences.terminalShell);
  if (selected) return selected;
  if (preferences.terminalShell === "pwsh" || preferences.terminalShell === "system") {
    throw new Error("The selected terminal shell is no longer installed.");
  }
  // Source Kqe explicitly resolves a vanished ordinary choice to the real
  // PowerShell command, then Command Prompt; it does not rewrite the setting.
  const defaultTarget = targets.find(target => target.id === "powershell") ?? targets.find(target => target.id === "cmd");
  if (!defaultTarget) throw new Error("No installed terminal shell is available.");
  return defaultTarget;
}

function shouldNotify(kind, focused) {
  if (kind === "permission") return preferences.permissionNotifications;
  if (kind === "question") return preferences.questionNotifications;
  if (kind !== "completion") throw new Error("Desktop notification kind is required.");
  return preferences.turnCompletionNotifications === "always"
    || preferences.turnCompletionNotifications === "onlyWhenUnfocused" && !focused;
}

function needsCloseConfirmation(source) {
  return preferences.confirmBeforeClose === "always"
    || preferences.confirmBeforeClose === "keyboardOnly" && source === "keyboard";
}

module.exports = { init, getPreferences, setPreferences, migrateDictationPreferences, preferredFileOpenTarget, setPreferredFileOpenTarget, fileOpenTarget, fileOpenTargets, terminalShellTargets, resolveTerminalShell, shouldNotify, needsCloseConfirmation, DEFAULT_PREFERENCES };
