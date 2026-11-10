"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const test = require("node:test");
const security = require("./security");
const { worktreeTrustRoot } = require("./worktree-trust");

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "minicode-worktree-trust-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const repo = path.join(directory, "repo"), checkout = path.join(directory, "pool", "linked");
  fs.mkdirSync(repo);
  const git = (...args) => execFileSync("git", args, { cwd: repo, stdio: "pipe" });
  git("init", "-b", "main");
  git("-c", "user.name=Trust fixture", "-c", "user.email=trust@example.invalid", "commit", "--allow-empty", "-m", "base");
  git("worktree", "add", "-b", "linked", checkout);
  const ledger = path.join(directory, "trusted.json");
  fs.writeFileSync(ledger, JSON.stringify([repo]));
  const gitdir = fs.readFileSync(path.join(checkout, ".git"), "utf8").trim().slice(7).trim();
  security.init({ initialRoots: [], trustedRootsFile: ledger });
  return { directory, repo, checkout, ledger, gitdir };
}

test("restored worktree uses the primary ledger and revokes access when trust or its backlink changes", t => {
  const { directory, repo, checkout, ledger, gitdir } = fixture(t);
  fs.writeFileSync(path.join(checkout, "source.ts"), "export {};");
  assert.equal(worktreeTrustRoot(checkout), fs.realpathSync.native(repo));
  assert.equal(security.restoreTrustedWorkspaceRoot(checkout), fs.realpathSync.native(checkout));
  assert.equal(security.assertReadablePath(path.join(checkout, "source.ts")), fs.realpathSync.native(path.join(checkout, "source.ts")));
  assert.equal(security.isTrustedWorkspaceRootPath(checkout), true);
  assert.deepEqual(JSON.parse(fs.readFileSync(ledger, "utf8")), [repo]);
  fs.writeFileSync(ledger, "[]");
  assert.equal(security.isWithinTrustedWorkspace(path.join(checkout, "source.ts")), false);
  fs.writeFileSync(ledger, JSON.stringify([repo]));
  assert.equal(security.isWithinTrustedWorkspace(path.join(checkout, "source.ts")), true);
  fs.writeFileSync(path.join(gitdir, "gitdir"), path.join(directory, "other", ".git"));
  assert.equal(security.isWithinTrustedWorkspace(path.join(checkout, "source.ts")), false);
});

for (const corruption of ["forged-checkout", "wrong-common", "oversize", "linked-directory-alias"]) {
  test(`worktree trust refuses ${corruption}`, t => {
    const { directory, checkout, gitdir } = fixture(t);
    if (corruption === "forged-checkout") {
      const forged = path.join(directory, "forged");
      fs.mkdirSync(forged);
      fs.copyFileSync(path.join(checkout, ".git"), path.join(forged, ".git"));
      assert.equal(security.restoreTrustedWorkspaceRoot(forged), "");
      assert.equal(security.restoreTrustedWorkspaceRoot(checkout), fs.realpathSync.native(checkout));
      return;
    }
    if (corruption === "wrong-common") fs.writeFileSync(path.join(gitdir, "commondir"), directory);
    if (corruption === "oversize") {
      fs.unlinkSync(path.join(checkout, ".git"));
      fs.writeFileSync(path.join(checkout, ".git"), `gitdir: ${gitdir}${" ".repeat(64 * 1024)}`);
    }
    if (corruption === "linked-directory-alias") {
      const alias = path.join(directory, "alias");
      fs.symlinkSync(gitdir, alias, process.platform === "win32" ? "junction" : "dir");
      fs.unlinkSync(path.join(checkout, ".git"));
      fs.writeFileSync(path.join(checkout, ".git"), `gitdir: ${alias}`);
    }
    assert.equal(worktreeTrustRoot(checkout), null);
    assert.equal(security.restoreTrustedWorkspaceRoot(checkout), "");
  });
}
