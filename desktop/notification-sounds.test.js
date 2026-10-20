"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const sounds = require("./notification-sounds");
const preferences = require("./preferences");

test("packaged built-in notification assets have their actual bytes in the release source and payload identity", () => {
  const script = path.join(__dirname, "scripts", "release-identity.js");
  const packageFile = path.join(__dirname, "package.json");
  const pkg = JSON.parse(fs.readFileSync(packageFile, "utf8"));
  const moduleBox = { exports: {} };
  // This contract checks the real ASAR sources. External prepared runtimes
  // have separate native checks and are not installed by the unit CI job.
  vm.runInNewContext(fs.readFileSync(script, "utf8"), {
    module: moduleBox, __dirname: path.dirname(script), process, Buffer,
    require(name) { return name === "node:fs" ? { ...fs, readFileSync(file, ...args) {
      if (file === packageFile) return JSON.stringify({ ...pkg, build: { ...pkg.build, extraResources: [] } });
      return fs.readFileSync(file, ...args);
    } } : require(name); },
  });
  const identity = moduleBox.exports.packageInputs();
  for (const selection of ["default", "classic"]) {
    const file = sounds.soundPath(selection, "unused-user-data");
    const digest = require("node:crypto").createHash("sha256").update(fs.readFileSync(file)).digest("hex");
    const relative = path.relative(__dirname, file).replaceAll("\\", "/");
    assert.equal(identity.payload["asar/" + relative], digest);
    assert.equal(identity.inputs["desktop/" + relative], digest);
  }
});

function wav(seconds = 1) {
  const bytes = Buffer.alloc(44 + 24000 * seconds * 2);
  bytes.write("RIFF"); bytes.writeUInt32LE(bytes.length - 8, 4); bytes.write("WAVEfmt ", 8);
  bytes.writeUInt32LE(16, 16); bytes.writeUInt16LE(1, 20); bytes.writeUInt16LE(1, 22);
  bytes.writeUInt32LE(24000, 24); bytes.writeUInt32LE(48000, 28); bytes.writeUInt16LE(2, 32); bytes.writeUInt16LE(16, 34);
  bytes.write("data", 36); bytes.writeUInt32LE(bytes.length - 44, 40);
  return [...bytes];
}

test("custom audio is stored as a real bounded local WAV and survives preference restart", t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-sound-test-"));
  t.after(() => fs.rmSync(directory, { recursive: true }));
  const sound = sounds.importSound({ name: "My audio.wav", wav: wav() }, directory);
  assert.equal(sounds.isNotificationSound(sound), true);
  assert.deepEqual([...fs.readFileSync(sounds.soundPath(sound, directory))], wav());
  const file = path.join(directory, "preferences.json");
  preferences.init(file);
  preferences.setPreferences({ notificationSound: sound, autoArchiveAfterDays: 14, localHistoryCompression: true });
  preferences.init(file);
  assert.deepEqual(preferences.getPreferences().notificationSound, sound);
  assert.equal(preferences.getPreferences().autoArchiveAfterDays, 14);
  assert.equal(preferences.getPreferences().localHistoryCompression, true);
  assert.throws(() => preferences.setPreferences({ notificationSound: { fileName: "../outside.wav", name: "x" } }), /Unsupported/);
  assert.throws(() => sounds.importSound({ name: "too-long.wav", wav: wav(30) }, directory), /30/);
  assert.throws(() => sounds.importSound({ name: "broken.wav", wav: [1, 2, 3] }, directory), /WAV/);
});

test("the actual native selected-file producer preserves cancel and enforces the Source file size boundary", async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-sound-pick-"));
  t.after(() => fs.rmSync(directory, { recursive: true }));
  const file = path.join(directory, "selected.wav"); fs.writeFileSync(file, Buffer.from(wav()));
  const owner = {};
  const picked = await sounds.pickSound({ showOpenDialog: async (actualOwner, options) => {
    assert.equal(actualOwner, owner); assert.deepEqual(options.filters[0].extensions, ["mp3", "wav", "flac", "ogg", "oga"]);
    return { canceled: false, filePaths: [file] };
  } }, owner);
  assert.equal(picked.name, "selected.wav"); assert.deepEqual(picked.bytes, wav());
  assert.equal(await sounds.pickSound({ showOpenDialog: async () => ({ canceled: true }) }, owner), null);
  fs.truncateSync(file, 10 * 1024 * 1024 + 1);
  await assert.rejects(sounds.pickSound({ showOpenDialog: async () => ({ canceled: false, filePaths: [file] }) }, owner), /10 MB/);
});

test("native sound playback uses the selected asset with hidden PowerShell and none stays silent", async () => {
  const moduleBox = { exports: {} }, children = [];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "notification-sounds.js"), "utf8"), {
    module: moduleBox, __dirname, Buffer, process: { platform: "win32" }, require(name) {
      if (name !== "node:child_process") return require(name);
      return { spawn(command, args, options) { const child = new EventEmitter(); children.push({ child, command, args, options }); return child; } };
    },
  });
  const api = moduleBox.exports;
  assert.equal(await api.playSound("none", "fixture-data"), true); assert.equal(children.length, 0);
  const playback = api.playSound("classic", "fixture-data");
  const launch = children[0]; assert.equal(launch.command, "powershell.exe"); assert.equal(launch.options.windowsHide, true);
  assert.match(Buffer.from(launch.args.at(-1), "base64").toString("utf16le"), /codex-classic\.wav/);
  launch.child.emit("close", 0, null); assert.equal(await playback, true);
  const failed = api.playSound("default", "fixture-data"); children[1].child.emit("close", 1, null);
  await assert.rejects(failed, /播放失败/);
});
