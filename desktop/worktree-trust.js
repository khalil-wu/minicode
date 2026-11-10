"use strict";

const fs = require("node:fs");
const path = require("node:path");
const decoder = new TextDecoder("utf-8", { fatal: true });
const trim = value => value.replace(/^[\x09-\x0d\x20]+|[\x09-\x0d\x20]+$/g, "");

function metadataText(file) {
  const info = fs.lstatSync(file);
  if (!info.isFile() || info.isSymbolicLink() || info.size > 64 * 1024) throw new Error("Git metadata is not a bounded regular file");
  const data = fs.readFileSync(file);
  if (data.length > 64 * 1024) throw new Error("Git metadata exceeds the Source size boundary");
  return trim(decoder.decode(data));
}

function gitDirectory(file) {
  const value = metadataText(file);
  if (!value.startsWith("gitdir:") || !trim(value.slice(7))) throw new Error("Missing Git directory pointer");
  return path.resolve(path.dirname(file), trim(value.slice(7)));
}

function worktreeTrustRoot(directory) {
  try {
    let checkout = fs.realpathSync.native(directory);
    while (true) {
      try {
        fs.lstatSync(path.join(checkout, ".git"));
        break;
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
        const parent = path.dirname(checkout);
        if (parent === checkout) return null;
        checkout = parent;
      }
    }
    const dotGit = path.join(checkout, ".git");
    if (!fs.lstatSync(dotGit).isFile()) return null;
    const link = gitDirectory(dotGit), info = fs.lstatSync(link);
    if (!info.isDirectory() || info.isSymbolicLink()) return null;
    const linked = fs.realpathSync.native(link);
    if (path.basename(path.dirname(linked)) !== "worktrees") return null;
    const common = path.dirname(path.dirname(linked));
    const backlink = path.resolve(linked, metadataText(path.join(linked, "gitdir")));
    const linkedCommon = fs.realpathSync.native(path.resolve(linked, metadataText(path.join(linked, "commondir"))));
    if (path.basename(backlink) !== ".git" || fs.realpathSync.native(path.dirname(backlink)) !== checkout || linkedCommon !== common) return null;
    const primary = path.dirname(common), primaryGit = path.join(primary, ".git");
    const actual = fs.realpathSync.native(fs.statSync(primaryGit).isDirectory() ? primaryGit : gitDirectory(primaryGit));
    return actual === common ? primary : null;
  } catch {
    return null;
  }
}

module.exports = { worktreeTrustRoot };
