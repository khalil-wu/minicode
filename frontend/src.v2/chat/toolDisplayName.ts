const BUILTIN_TOOL_LABELS: Record<string, string> = {
  apply_patch: "应用补丁",
  edit_file: "编辑文件",
  list_files: "列出文件",
  read_file: "读取文件",
  run_command: "运行命令",
  search_files: "搜索文件",
  shell_command: "运行命令",
  web_fetch: "获取网页",
  webfetch: "获取网页",
  web_search: "搜索网页",
  write_file: "写入文件",
  update_plan: "更新计划",
};

const BUILTIN_TOOL_NAME_RE = new RegExp(
  `\\b(?:${Object.keys(BUILTIN_TOOL_LABELS).join("|")})\\b`,
  "gi",
);

/** Render runtime protocol identifiers as user-facing MiniCode labels.
 * Runtime records keep the original name for execution, policy matching,
 * replay export, and diagnostics. */
export function readableToolLabel(value: string | undefined): string {
  const separatedProtocolNames = String(value || "").trim()
    .replace(/(webfetch|web_fetch|web_search)(?=webfetch|web_fetch|web_search)/gi, "$1 ");
  const mcpLabel = separatedProtocolNames.replace(
    /\bmcp__([A-Za-z0-9_.-]+)__([A-Za-z0-9_.-]+)\b/g,
    "$1.$2",
  );
  return mcpLabel.replace(
    BUILTIN_TOOL_NAME_RE,
    (name) => BUILTIN_TOOL_LABELS[name.toLowerCase()] || name,
  )
    .replace(/(?:获取网页[\s,·]*){2,}/g, "获取网页 ")
    .replace(/(?:搜索网页[\s,·]*){2,}/g, "搜索网页 ")
    .trim();
}
