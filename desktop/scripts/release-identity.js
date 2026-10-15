"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { minimatch } = require("minimatch");
const { execFileSync } = require("node:child_process");
const desktop = path.resolve(__dirname, "..");
const project = path.resolve(desktop, "..");
const receiptPath = path.join(desktop, ".tmp", "frontend-build-identity.json");
const manifestPath = path.join(desktop, ".tmp", "release-identity.json");
const sha = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");
function files(directory, allow = () => true) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const file = path.join(directory, entry.name);
    return entry.isDirectory() ? files(file, allow) : allow(file) ? [file] : [];
  }).sort();
}
const hashes = (paths) => Object.fromEntries(paths.map(file => [path.relative(project, file).replaceAll("\\", "/"), sha(fs.readFileSync(file))]));
const frontendInputs = () => hashes([
  ...files(path.join(project, "frontend", "src.v2")),
  ...files(path.join(project, "frontend", "public")),
  ...["package.json", "package-lock.json", "index.html", "vite.config.ts", "postcss.config.js", "tailwind.config.js", "tsconfig.json"].map(name => path.join(project, "frontend", name)),
  ...[".env", ".env.local", ".env.desktop-release", ".env.desktop-release.local"].map(name => path.join(project, "frontend", name)).filter(file => fs.existsSync(file)),
]);
const buildEnvironment = () => Object.fromEntries(Object.keys(process.env).filter(key => key.startsWith("VITE_") || ["NODE_ENV", "VITEST", "MINICODE_API_BASE_URL", "MINICODE_WS_BASE_URL"].includes(key)).sort().map(key => [key, sha(Buffer.from(process.env[key]))]));
function selectedFiles(directory, patterns) {
  const include = patterns.filter(pattern => !pattern.startsWith("!"));
  const exclude = patterns.filter(pattern => pattern.startsWith("!")).map(pattern => pattern.slice(1));
  return files(directory, file => {
    const relative = path.relative(directory, file).replaceAll("\\", "/");
    return include.some(pattern => minimatch(relative, pattern, { dot: true })) && !exclude.some(pattern => minimatch(relative, pattern, { dot: true }));
  });
}
const frontendOutput = () => hashes(files(path.join(project, "frontend", ".tmp", "desktop-release")));
function equal(actual, expected, label) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error(`${label} differs from the recorded build inputs; rebuild before packaging.`);
}
function write(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(value, null, 2) + "\n");
}
function recordFrontend(stage) {
  if (stage === "frontend-before") write(receiptPath, { source: frontendInputs(), environment: buildEnvironment(), mode: "desktop-release" });
  else {
    const receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
    equal(frontendInputs(), receipt.source, "Frontend source");
    equal(buildEnvironment(), receipt.environment, "Frontend build environment");
    write(receiptPath, { ...receipt, output: frontendOutput() });
  }
}
function packageInputs() {
  const pkg = JSON.parse(fs.readFileSync(path.join(desktop, "package.json"), "utf8"));
  const desktopFiles = pkg.build.files.filter(name => !name.includes("*") && !name.startsWith("!") && name !== "package.json");
  const inputPaths = [...desktopFiles.map(name => path.join(desktop, name)), path.join(desktop, "package.json"), path.join(desktop, "package-lock.json"),
    ...files(path.join(desktop, "scripts"), file => /\.(js|ps1|py)$/.test(file))];
  const payload = {}, resourceDirectories = [];
  for (const name of desktopFiles) payload["asar/" + name] = sha(fs.readFileSync(path.join(desktop, name)));
  for (const resource of pkg.build.extraResources) {
    if (resource.to === "release-identity.json") continue; // The manifest verifies its copied bytes after packaging.
    const source = path.resolve(desktop, resource.from);
    const isDirectory = fs.statSync(source).isDirectory();
    const selected = isDirectory ? selectedFiles(source, resource.filter || ["**/*"]) : [source];
    if (isDirectory) resourceDirectories.push(resource.to.replaceAll("\\", "/"));
    for (const file of selected) {
      inputPaths.push(file);
      const target = "resources/" + resource.to.replaceAll("\\", "/") + (isDirectory ? "/" + path.relative(source, file).replaceAll("\\", "/") : "");
      const digest = sha(fs.readFileSync(file));
      if (payload[target] !== undefined && payload[target] !== digest) throw new Error(`Conflicting release resource target: ${target}`);
      payload[target] = digest;
    }
  }
  return { package_version: pkg.version, inputs: hashes([...new Set(inputPaths)].sort()), payload, resourceDirectories: [...new Set(resourceDirectories)].sort() };
}
async function beforePack(context) {
  await require("./verify-windows-sandbox-runtime")(context);
  const receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
  equal(frontendInputs(), receipt.source, "Frontend source");
  equal(buildEnvironment(), receipt.environment, "Frontend build environment");
  equal(frontendOutput(), receipt.output, "Frontend output");
  const captured = packageInputs();
  const git = (...args) => execFileSync("git", args, { cwd: project });
  write(manifestPath, { schema: "minicode.release.identity", version: 1, ...captured,
    commit: git("rev-parse", "HEAD").toString().trim(), worktree_status_sha256: sha(git("status", "--porcelain=v1", "-z")),
    source_sha256: sha(JSON.stringify({ inputs: captured.inputs, frontend: receipt.source, environment: receipt.environment })), frontend: receipt,
    platform: context.electronPlatformName, arch: context.arch });
}
async function afterPack(context) {
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  equal(frontendInputs(), manifest.frontend.source, "Frontend source during packaging");
  equal(frontendOutput(), manifest.frontend.output, "Frontend output during packaging");
  const current = packageInputs();
  equal(current.inputs, manifest.inputs, "Complete source inventory during packaging");
  equal(current.payload, manifest.payload, "Complete resource inputs during packaging");
  const resources = context.electronPlatformName === "darwin"
    ? path.join(context.appOutDir, context.packager.appInfo.productFilename + ".app", "Contents", "Resources")
    : path.join(context.appOutDir, "resources");
  const archive = path.join(resources, "app.asar");
  const asar = require("@electron/asar");
  for (const [file, digest] of Object.entries(manifest.payload)) {
    const bytes = file.startsWith("asar/") ? asar.extractFile(archive, file.slice(5)) : fs.readFileSync(path.join(resources, file.slice("resources/".length)));
    if (sha(bytes) !== digest) throw new Error(`Packaged bytes differ from source: ${file}`);
  }
  for (const directory of manifest.resourceDirectories) {
    const prefix = "resources/" + directory + "/";
    const expected = Object.keys(manifest.payload).filter(file => file.startsWith(prefix)).sort();
    const actual = files(path.join(resources, directory)).map(file => "resources/" + path.relative(resources, file).replaceAll("\\", "/")).sort();
    equal(actual, expected, `Packaged ${directory} inventory`);
  }
  const packaged = JSON.parse(fs.readFileSync(path.join(resources, "release-identity.json"), "utf8"));
  equal(packaged, manifest, "Packaged identity");
  console.log(`Release source and payload identity verified: ${manifest.source_sha256}`);
}
module.exports = { beforePack, afterPack, equal, hashes, files, packageInputs };
if (require.main === module) recordFrontend(process.argv[2]);
