"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");

function chromeUserDataRoot(platform = process.platform, environment = process.env, home = os.homedir()) {
  if (platform === "win32") return path.join(environment.LOCALAPPDATA, "Google", "Chrome", "User Data");
  if (platform === "darwin") return path.join(home, "Library", "Application Support", "Google", "Chrome");
  return path.join(home, ".config", "google-chrome");
}

function createBrowserProfileImporter({ history, rootPath = chromeUserDataRoot(), temporaryRoot = os.tmpdir() }) {
  function listImportableBrowserProfiles() {
    const localStatePath = path.join(rootPath, "Local State");
    if (!fs.existsSync(localStatePath)) return [];
    const localState = JSON.parse(fs.readFileSync(localStatePath, "utf8"));
    if (localState.profile?.info_cache == null) return [];
    return Object.entries(localState.profile.info_cache).map(([directory, info]) => {
      const profilePath = path.resolve(rootPath, directory);
      if (path.dirname(profilePath) !== path.resolve(rootPath)) throw new Error("Invalid browser profile directory");
      return { source: "chrome", appName: "Google Chrome", profileName: info.name,
        profileDirectoryName: directory, profilePath, rootPath,
        hasHistory: fs.existsSync(path.join(profilePath, "History")) };
    }).filter(profile => profile.hasHistory);
  }

  async function importBrowserProfile(request) {
    if (request.importHistory !== true || request.importCookies || request.importPasswords || request.importExtensions) {
      throw new Error("This browser import supports browsing history only");
    }
    const profile = listImportableBrowserProfiles().find(item => item.source === request.source && item.profilePath === request.profilePath);
    if (!profile) throw new Error("Browser profile is no longer importable");
    const directory = await fs.promises.mkdtemp(path.join(temporaryRoot, "minicode-browser-import-"));
    try {
      const snapshotPath = path.join(directory, "History");
      await fs.promises.copyFile(path.join(profile.profilePath, "History"), snapshotPath);
      const walPath = path.join(profile.profilePath, "History-wal");
      if (fs.existsSync(walPath)) await fs.promises.copyFile(walPath, `${snapshotPath}-wal`);
      const database = new DatabaseSync(snapshotPath, { readOnly: true });
      let rows;
      try {
        const statement = database.prepare("SELECT urls.url, urls.title, visits.visit_time AS visitTime FROM visits JOIN urls ON urls.id = visits.url ORDER BY visits.visit_time DESC, visits.id DESC");
        statement.setReadBigInts(true);
        rows = statement.all();
      } finally { database.close(); }
      const entries = [];
      let skippedInvalid = 0;
      for (const row of rows) {
        let url;
        try { url = new URL(row.url); }
        catch { skippedInvalid++; continue; }
        const visitTime = Number(row.visitTime / 1000n - 11644473600000n);
        if (!["http:", "https:"].includes(url.protocol) || row.visitTime <= 0n || !Number.isSafeInteger(visitTime)) {
          skippedInvalid++; continue;
        }
        entries.push({ url: url.href, title: row.title, visitTime });
      }
      const result = history.importVisits(entries);
      return { source: profile.source, profilePath: profile.profilePath,
        history: { status: "completed", discovered: rows.length, ...result, skippedInvalid, failed: 0 } };
    } finally { await fs.promises.rm(directory, { recursive: true }); }
  }

  return { listImportableBrowserProfiles, importBrowserProfile };
}

module.exports = { createBrowserProfileImporter, chromeUserDataRoot };
