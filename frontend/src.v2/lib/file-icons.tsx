import type { ReactNode } from "react";
import { Icon } from "@iconify/react";
import type { IconifyIcon } from "@iconify/types";
import folderSvg from "material-icon-theme/icons/folder.svg?url";
import folderOpenSvg from "material-icon-theme/icons/folder-open.svg?url";
import folderApi from "material-icon-theme/icons/folder-api.svg?url";
import folderApiOpen from "material-icon-theme/icons/folder-api-open.svg?url";
import folderClient from "material-icon-theme/icons/folder-client.svg?url";
import folderClientOpen from "material-icon-theme/icons/folder-client-open.svg?url";
import folderComponents from "material-icon-theme/icons/folder-components.svg?url";
import folderComponentsOpen from "material-icon-theme/icons/folder-components-open.svg?url";
import folderConfig from "material-icon-theme/icons/folder-config.svg?url";
import folderConfigOpen from "material-icon-theme/icons/folder-config-open.svg?url";
import folderCss from "material-icon-theme/icons/folder-css.svg?url";
import folderCssOpen from "material-icon-theme/icons/folder-css-open.svg?url";
import folderDist from "material-icon-theme/icons/folder-dist.svg?url";
import folderDistOpen from "material-icon-theme/icons/folder-dist-open.svg?url";
import folderDocs from "material-icon-theme/icons/folder-docs.svg?url";
import folderDocsOpen from "material-icon-theme/icons/folder-docs-open.svg?url";
import folderGit from "material-icon-theme/icons/folder-git.svg?url";
import folderGitOpen from "material-icon-theme/icons/folder-git-open.svg?url";
import folderGithub from "material-icon-theme/icons/folder-github.svg?url";
import folderGithubOpen from "material-icon-theme/icons/folder-github-open.svg?url";
import folderImages from "material-icon-theme/icons/folder-images.svg?url";
import folderImagesOpen from "material-icon-theme/icons/folder-images-open.svg?url";
import folderJavascript from "material-icon-theme/icons/folder-javascript.svg?url";
import folderJavascriptOpen from "material-icon-theme/icons/folder-javascript-open.svg?url";
import folderNode from "material-icon-theme/icons/folder-node.svg?url";
import folderNodeOpen from "material-icon-theme/icons/folder-node-open.svg?url";
import folderPublic from "material-icon-theme/icons/folder-public.svg?url";
import folderPublicOpen from "material-icon-theme/icons/folder-public-open.svg?url";
import folderScripts from "material-icon-theme/icons/folder-scripts.svg?url";
import folderScriptsOpen from "material-icon-theme/icons/folder-scripts-open.svg?url";
import folderServer from "material-icon-theme/icons/folder-server.svg?url";
import folderServerOpen from "material-icon-theme/icons/folder-server-open.svg?url";
import folderSrc from "material-icon-theme/icons/folder-src.svg?url";
import folderSrcOpen from "material-icon-theme/icons/folder-src-open.svg?url";
import folderTest from "material-icon-theme/icons/folder-test.svg?url";
import folderTestOpen from "material-icon-theme/icons/folder-test-open.svg?url";
import folderTypescript from "material-icon-theme/icons/folder-typescript.svg?url";
import folderTypescriptOpen from "material-icon-theme/icons/folder-typescript-open.svg?url";
import defaultFileIcon from "@iconify-icons/material-icon-theme/document";
import cIcon from "@iconify-icons/material-icon-theme/c";
import cppIcon from "@iconify-icons/material-icon-theme/cpp";
import csharpIcon from "@iconify-icons/material-icon-theme/csharp";
import cssIcon from "@iconify-icons/material-icon-theme/css";
import consoleIcon from "@iconify-icons/material-icon-theme/console";
import dbIcon from "@iconify-icons/material-icon-theme/database";
import dockerIcon from "@iconify-icons/material-icon-theme/docker";
import fontIcon from "@iconify-icons/material-icon-theme/font";
import gitIcon from "@iconify-icons/material-icon-theme/git";
import goIcon from "@iconify-icons/material-icon-theme/go";
import htmlIcon from "@iconify-icons/material-icon-theme/html";
import imageIcon from "@iconify-icons/material-icon-theme/image";
import javaIcon from "@iconify-icons/material-icon-theme/java";
import jsIcon from "@iconify-icons/material-icon-theme/javascript";
import jsonIcon from "@iconify-icons/material-icon-theme/json";
import kotlinIcon from "@iconify-icons/material-icon-theme/kotlin";
import lessIcon from "@iconify-icons/material-icon-theme/less";
import licenseIcon from "@iconify-icons/material-icon-theme/license";
import luaIcon from "@iconify-icons/material-icon-theme/lua";
import makefileIcon from "@iconify-icons/material-icon-theme/makefile";
import markdownIcon from "@iconify-icons/material-icon-theme/markdown";
import npmIcon from "@iconify-icons/material-icon-theme/npm";
import pdfIcon from "@iconify-icons/material-icon-theme/pdf";
import phpIcon from "@iconify-icons/material-icon-theme/php";
import pnpmIcon from "@iconify-icons/material-icon-theme/pnpm";
import powerpointIcon from "@iconify-icons/material-icon-theme/powerpoint";
import powershellIcon from "@iconify-icons/material-icon-theme/powershell";
import pythonIcon from "@iconify-icons/material-icon-theme/python";
import reactIcon from "@iconify-icons/material-icon-theme/react";
import rubyIcon from "@iconify-icons/material-icon-theme/ruby";
import rustIcon from "@iconify-icons/material-icon-theme/rust";
import sassIcon from "@iconify-icons/material-icon-theme/sass";
import scalaIcon from "@iconify-icons/material-icon-theme/scala";
import settingsIcon from "@iconify-icons/material-icon-theme/settings";
import svelteIcon from "@iconify-icons/material-icon-theme/svelte";
import svgIcon from "@iconify-icons/material-icon-theme/svg";
import swiftIcon from "@iconify-icons/material-icon-theme/swift";
import tableIcon from "@iconify-icons/material-icon-theme/table";
import tomlIcon from "@iconify-icons/material-icon-theme/toml";
import tsIcon from "@iconify-icons/material-icon-theme/typescript";
import tsconfigIcon from "@iconify-icons/material-icon-theme/tsconfig";
import viteIcon from "@iconify-icons/material-icon-theme/vite";
import vueIcon from "@iconify-icons/material-icon-theme/vue";
import wordIcon from "@iconify-icons/material-icon-theme/word";
import xmlIcon from "@iconify-icons/material-icon-theme/xml";
import yamlIcon from "@iconify-icons/material-icon-theme/yaml";
import yarnIcon from "@iconify-icons/material-icon-theme/yarn";
import zipIcon from "@iconify-icons/material-icon-theme/zip";
import { isImagePath } from "./media-types";

export type FileGlyphKind =
  | "archive" | "code" | "config" | "data" | "database" | "document"
  | "font" | "generic" | "git" | "image" | "lock" | "package"
  | "pdf" | "style" | "terminal";

const CODE_EXTENSIONS = new Set([
  "c", "cc", "cjs", "cpp", "cs", "dart", "ex", "exs", "go", "h", "hpp", "html", "java", "js", "jsx", "kt", "lua", "mjs", "php", "py", "r", "rb", "rs", "scala", "svelte", "swift", "ts", "tsx", "vue",
]);
const DATA_EXTENSIONS = new Set(["csv", "ini", "json", "toml", "xml", "yaml", "yml"]);
const DATABASE_EXTENSIONS = new Set(["db", "sqlite", "sqlite3", "sql"]);
const DOCUMENT_EXTENSIONS = new Set(["doc", "docx", "log", "md", "mdx", "rst", "txt"]);
const ARCHIVE_EXTENSIONS = new Set(["7z", "gz", "rar", "tar", "tgz", "zip"]);
const FONT_EXTENSIONS = new Set(["otf", "ttf", "woff", "woff2"]);
const STYLE_EXTENSIONS = new Set(["css", "less", "sass", "scss"]);
const TERMINAL_EXTENSIONS = new Set(["bat", "cmd", "ps1", "sh", "zsh"]);

const baseName = (name: string): string => name.toLowerCase().split(/[/\\]/).pop() ?? name.toLowerCase();
const extensionOf = (base: string): string => base.includes(".") ? base.split(".").pop() ?? "" : "";

export const fileGlyphKind = (name: string): FileGlyphKind => {
  const base = baseName(name);
  const ext = extensionOf(base);
  if (base.endsWith(".lock") || base.includes("-lock.") || base === "lockfile") return "lock";
  if (base.startsWith(".git") || base === ".gitattributes" || base === ".gitignore") return "git";
  if (["package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "cargo.toml"].includes(base) || base.startsWith("requirements")) return "package";
  if (base.startsWith(".env") || base.includes("config") || ["dockerfile", "makefile", "pyproject.toml", "compose.yml", "compose.yaml"].includes(base)) return "config";
  if (/^(readme|license|changelog|contributing)(\.|$)/.test(base)) return "document";
  if (ext === "pdf") return "pdf";
  if (CODE_EXTENSIONS.has(ext)) return "code";
  if (STYLE_EXTENSIONS.has(ext)) return "style";
  if (DATA_EXTENSIONS.has(ext)) return "data";
  if (DATABASE_EXTENSIONS.has(ext)) return "database";
  if (DOCUMENT_EXTENSIONS.has(ext)) return "document";
  if (isImagePath(base)) return "image";
  if (ARCHIVE_EXTENSIONS.has(ext)) return "archive";
  if (FONT_EXTENSIONS.has(ext)) return "font";
  if (TERMINAL_EXTENSIONS.has(ext)) return "terminal";
  return "generic";
};

const ICON_BY_EXTENSION: Record<string, IconifyIcon> = {
  c: cIcon, h: cIcon, cc: cppIcon, cpp: cppIcon, hpp: cppIcon, cs: csharpIcon,
  css: cssIcon, less: lessIcon, sass: sassIcon, scss: sassIcon,
  db: dbIcon, sqlite: dbIcon, sqlite3: dbIcon, sql: dbIcon,
  doc: wordIcon, docx: wordIcon, xls: tableIcon, xlsx: tableIcon, csv: tableIcon,
  ppt: powerpointIcon, pptx: powerpointIcon, pdf: pdfIcon,
  go: goIcon, html: htmlIcon, js: jsIcon, cjs: jsIcon, mjs: jsIcon,
  jsx: reactIcon, tsx: reactIcon, ts: tsIcon,
  json: jsonIcon, kt: kotlinIcon, java: javaIcon, lua: luaIcon,
  md: markdownIcon, mdx: markdownIcon, php: phpIcon, py: pythonIcon,
  rb: rubyIcon, rs: rustIcon, scala: scalaIcon, svelte: svelteIcon,
  svg: svgIcon, swift: swiftIcon, vue: vueIcon,
  png: imageIcon, jpg: imageIcon, jpeg: imageIcon, gif: imageIcon,
  webp: imageIcon, ico: imageIcon, avif: imageIcon, bmp: imageIcon,
  ps1: powershellIcon, sh: consoleIcon, bash: consoleIcon, zsh: consoleIcon,
  yaml: yamlIcon, yml: yamlIcon, toml: tomlIcon, xml: xmlIcon, ini: settingsIcon,
  zip: zipIcon, "7z": zipIcon, rar: zipIcon, tar: zipIcon, tgz: zipIcon, gz: zipIcon,
  otf: fontIcon, ttf: fontIcon, woff: fontIcon, woff2: fontIcon,
  txt: defaultFileIcon, log: defaultFileIcon, rst: defaultFileIcon,
};

const officialFileIcon = (name: string): IconifyIcon => {
  const base = baseName(name);
  if (base === "package.json" || base === "package-lock.json") return npmIcon;
  if (base === "pnpm-lock.yaml") return pnpmIcon;
  if (base === "yarn.lock") return yarnIcon;
  if (base === "dockerfile" || base.startsWith("docker-compose")) return dockerIcon;
  if (base === "makefile") return makefileIcon;
  if (base === "tsconfig.json" || base === "jsconfig.json") return tsconfigIcon;
  if (base.startsWith("vite.config.")) return viteIcon;
  if (base.startsWith(".env")) return settingsIcon;
  if (base.startsWith(".git") || base === "gitignore") return gitIcon;
  if (base === "license" || base.startsWith("license.")) return licenseIcon;
  return ICON_BY_EXTENSION[extensionOf(base)] ?? defaultFileIcon;
};

const FILE_GLYPH_COLORS: Record<FileGlyphKind, string> = {
  archive: "var(--mc-icon-archive, var(--text-muted))",
  code: "var(--mc-icon-code, var(--state-info))",
  config: "var(--mc-icon-config, var(--state-warning))",
  data: "var(--mc-icon-data, var(--accent-primary))",
  database: "var(--mc-icon-database, var(--accent-primary))",
  document: "var(--mc-icon-document, var(--text-muted))",
  font: "var(--mc-icon-font, var(--text-secondary))",
  generic: "var(--mc-icon-generic, var(--text-muted))",
  git: "var(--mc-icon-git, var(--text-muted))",
  image: "var(--mc-icon-media, var(--state-success))",
  lock: "var(--mc-icon-config, var(--state-warning))",
  package: "var(--mc-icon-package, var(--state-warning))",
  pdf: "var(--mc-icon-pdf, var(--state-danger))",
  style: "var(--mc-icon-style, var(--accent-primary))",
  terminal: "var(--mc-icon-terminal, var(--text-secondary))",
};

export const fileGlyphColor = (name: string): string => FILE_GLYPH_COLORS[fileGlyphKind(name)];

export const fileIcon = (name: string, options: { size?: number; className?: string } = {}): ReactNode => {
  const size = options.size ?? 18;
  return <Icon
    icon={officialFileIcon(name)}
    width={size}
    height={size}
    className={`mc-file-icon ${options.className ?? "file-tree-file-icon"}`}
    data-file-kind={fileGlyphKind(name)}
    aria-hidden="true"
  />;
};

const FOLDER_ICONS: Record<string, [string, string]> = {
  api: [folderApi, folderApiOpen],
  backend: [folderServer, folderServerOpen],
  build: [folderDist, folderDistOpen],
  client: [folderClient, folderClientOpen],
  components: [folderComponents, folderComponentsOpen],
  config: [folderConfig, folderConfigOpen],
  css: [folderCss, folderCssOpen],
  dist: [folderDist, folderDistOpen],
  docs: [folderDocs, folderDocsOpen],
  frontend: [folderClient, folderClientOpen],
  ".git": [folderGit, folderGitOpen],
  ".github": [folderGithub, folderGithubOpen],
  images: [folderImages, folderImagesOpen],
  img: [folderImages, folderImagesOpen],
  js: [folderJavascript, folderJavascriptOpen],
  javascript: [folderJavascript, folderJavascriptOpen],
  node_modules: [folderNode, folderNodeOpen],
  public: [folderPublic, folderPublicOpen],
  scripts: [folderScripts, folderScriptsOpen],
  server: [folderServer, folderServerOpen],
  src: [folderSrc, folderSrcOpen],
  styles: [folderCss, folderCssOpen],
  test: [folderTest, folderTestOpen],
  tests: [folderTest, folderTestOpen],
  ts: [folderTypescript, folderTypescriptOpen],
  typescript: [folderTypescript, folderTypescriptOpen],
};

export const folderIcon = (expanded: boolean, size = 18, name = ""): ReactNode => {
  const [closed, open] = FOLDER_ICONS[baseName(name)] ?? [folderSvg, folderOpenSvg];
  return <img
    src={expanded ? open : closed}
    width={size}
    height={size}
    className="mc-file-icon file-tree-folder-icon"
    alt=""
    aria-hidden="true"
    draggable={false}
  />;
};
