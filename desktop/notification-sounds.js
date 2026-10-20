"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { spawn } = require("node:child_process");

const BUILTIN_SOUNDS = { default: "codex-notification.wav", classic: "codex-classic.wav" };
const CUSTOM_SOUND_NAME = /^minicode-custom-[a-f0-9]{64}\.wav$/;
let playing;

function isNotificationSound(value) {
  return ["default", "classic", "none"].includes(value)
    || value !== null && typeof value === "object" && CUSTOM_SOUND_NAME.test(value.fileName)
      && typeof value.name === "string" && value.name.length > 0;
}

async function pickSound(dialog, owner) {
  const result = await dialog.showOpenDialog(owner, {
    title: "选择通知声音",
    properties: ["openFile"],
    filters: [{ name: "MP3, WAV, FLAC, Ogg", extensions: ["mp3", "wav", "flac", "ogg", "oga"] }],
  });
  if (result.canceled) return null;
  const file = result.filePaths[0];
  if (fs.statSync(file).size > 10 * 1024 * 1024) throw new Error("通知声音文件不能超过 10 MB。");
  return { name: path.basename(file), bytes: [...fs.readFileSync(file)] };
}

function importSound({ name, wav }, userData) {
  const bytes = Buffer.from(wav);
  if (typeof name !== "string" || name.length === 0
    || bytes.length < 44 || bytes.length > 10 * 1024 * 1024
    || bytes.toString("ascii", 0, 4) !== "RIFF" || bytes.toString("ascii", 8, 16) !== "WAVEfmt "
    || bytes.readUInt32LE(16) !== 16 || bytes.readUInt16LE(20) !== 1
    || ![1, 2].includes(bytes.readUInt16LE(22)) || bytes.readUInt32LE(24) !== 24000
    || bytes.readUInt16LE(34) !== 16 || bytes.toString("ascii", 36, 40) !== "data"
    || bytes.readUInt32LE(4) !== bytes.length - 8 || bytes.readUInt32LE(40) !== bytes.length - 44) {
    throw new Error("通知声音必须是可读取的 PCM WAV 音频。");
  }
  const frameBytes = bytes.readUInt16LE(22) * 2;
  const sampleBytes = bytes.length - 44;
  if (sampleBytes === 0 || sampleBytes % frameBytes !== 0 || sampleBytes >= 24000 * frameBytes * 30) {
    throw new Error("通知声音必须短于 30 秒。");
  }
  const fileName = `minicode-custom-${createHash("sha256").update(bytes).digest("hex")}.wav`;
  const directory = path.join(userData, "notification-sounds");
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(directory, fileName), bytes);
  return { fileName, name };
}

function soundPath(sound, userData) {
  return typeof sound === "object" ? path.join(userData, "notification-sounds", sound.fileName)
    : path.join(__dirname, "sounds", BUILTIN_SOUNDS[sound]);
}

function playSound(sound, userData, { restart = false } = {}) {
  if (sound === "none") { if (restart) playing?.kill(); return Promise.resolve(true); }
  if (playing && !restart) return Promise.resolve(false);
  if (restart) playing?.kill();
  const file = soundPath(sound, userData);
  const command = process.platform === "win32" ? "powershell.exe" : process.platform === "darwin" ? "/usr/bin/afplay" : "aplay";
  const script = `[System.Media.SoundPlayer]::new('${file.replaceAll("'", "''")}').PlaySync()`;
  const args = process.platform === "win32"
    ? ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")]
    : [file];
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: "ignore", windowsHide: true });
    playing = child;
    child.once("error", error => { if (playing === child) playing = undefined; reject(error); });
    child.once("close", (code, signal) => {
      if (playing === child) playing = undefined;
      if (code === 0 || signal === "SIGTERM") resolve(true);
      else reject(new Error(`通知声音播放失败 (${code})。`));
    });
  });
}

module.exports = { isNotificationSound, pickSound, importSound, playSound, soundPath };
