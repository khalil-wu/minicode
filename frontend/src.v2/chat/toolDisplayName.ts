const BUILTIN_TOOL_LABELS: Record<string, string> = {
  apply_patch: "Edit",
  edit_file: "Edit",
  write_file: "Edit",
  list_files: "List",
  read_file: "Read",
  read_artifact: "Read",
  grep_files: "Search",
  glob_files: "Search",
  search_files: "Search",
  run_command: "Run",
  shell_command: "Run",
  exec_command: "Run",
  bash: "Run",
  web_fetch: "Fetch",
  webfetch: "Fetch",
  web_search: "Search",
  websearch: "Search",
  update_plan: "Update plan",
  tool_exec: "Run code",
  tool_wait: "Run code",
  monitor: "Read command output",
  task_status: "Check agents",
  ask_user: "Ask user",
  task_create: "Create task",
  task_get: "Read task",
  task_list: "List tasks",
  task_update: "Update task",
  task_output: "Save task result",
  preview_server: "Preview",
};

const BUILTIN_TOOL_NAME_RE = new RegExp(
  `^(?:${Object.keys(BUILTIN_TOOL_LABELS).join("|")})(?=$|[\\s,:·(])`,
  "i",
);

const RUNTIME_ACTION_LABELS: Record<string, string> = {
  ...Object.fromEntries(Object.values(BUILTIN_TOOL_LABELS).map((label) => [label, label])),
  Read: "Read",
  Reading: "Read",
  "Read file": "Read",
  "Read artifact": "Read",
  List: "List",
  Listed: "List",
  Listing: "List",
  Search: "Search",
  Searched: "Search",
  Searching: "Search",
  "Search web": "Search",
  Edit: "Edit",
  Edited: "Edit",
  Editing: "Edit",
  Write: "Edit",
  Writing: "Edit",
  Wrote: "Edit",
  "Apply patch": "Edit",
  Run: "Run",
  Ran: "Run",
  Running: "Run",
  "Run command": "Run",
  "Ran command": "Run",
  "Running command": "Run",
  "Run code": "Run code",
  "Running code": "Run code",
  Fetch: "Fetch",
  Fetched: "Fetch",
  Fetching: "Fetch",
  "Update plan": "Update plan",
  "Start subagent": "Start subagent",
  "Script completed": "Run code",
  "Script yielded": "Running code",
  "Script running": "Running code",
  "Script failed": "Run code · Failed",
  "Script cancelled": "Run code · Cancelled",
  "Tool combination": "Run code",
  "code-mode": "Run code",
  code_mode: "Run code",
  正在执行代码: "Run code",
  正在运行代码: "Run code",
  运行代码: "Run code",
  读取文件: "Read",
  读取: "Read",
  已读取: "Read",
  正在读取: "Read",
  列出文件: "List",
  列出: "List",
  已列出: "List",
  正在列出文件: "List",
  搜索文件: "Search",
  搜索网页: "Search",
  搜索: "Search",
  已搜索: "Search",
  正在搜索文件: "Search",
  正在搜索网页: "Search",
  正在搜索: "Search",
  应用补丁: "Edit",
  编辑文件: "Edit",
  写入文件: "Edit",
  已编辑: "Edit",
  正在编辑: "Edit",
  运行命令: "Run",
  已运行命令: "Run",
  正在运行命令: "Run",
  正在运行: "Run",
  获取网页: "Fetch",
  已获取网页: "Fetch",
  正在获取网页: "Fetch",
  更新计划: "Update plan",
  已更新任务清单: "Update plan",
  启动子Agent: "Start subagent",
  "启动子 Agent": "Start subagent",
  组合工具调用: "Run code",
  工具组合: "Run code",
  等待脚本: "Wait",
  脚本已完成: "Run code",
  脚本仍在运行: "Running code",
  脚本执行失败: "Run code · Failed",
  脚本已取消: "Run code · Cancelled",
};

const RUNTIME_ACTION_RE = new RegExp(
  `^(?:${Object.keys(RUNTIME_ACTION_LABELS).sort((a, b) => b.length - a.length).join("|")})(?=$|[\\s,:·(])`,
);

const ACTION_CHROME: Record<string, [string, string]> = {
  Read: ["读取", "正在读取"], List: ["查看目录", "正在查看目录"],
  Search: ["搜索", "正在搜索"], Edit: ["编辑", "正在编辑"],
  Run: ["运行", "正在运行"], Fetch: ["读取网页", "正在读取网页"],
  "Run code": ["操作结果", "操作结果"], "Running code": ["操作结果", "操作结果"],
  Wait: ["等待", "等待中"], "Read command output": ["读取命令输出", "正在读取命令输出"],
  "Update plan": ["更新计划", "正在更新计划"], "Start subagent": ["启动子智能体", "正在启动子智能体"],
  "Check agents": ["查看子智能体", "正在查看子智能体"], "Ask user": ["向你提问", "等待你回复"],
  "Create task": ["创建任务", "正在创建任务"], "Read task": ["读取任务", "正在读取任务"],
  "List tasks": ["查看任务", "正在查看任务"], "Update task": ["更新任务", "正在更新任务"],
  "Save task result": ["保存任务结果", "正在保存任务结果"], Preview: ["预览", "正在预览"],
  "Run code · Failed": ["错误详情", "错误详情"],
  "Run code · Cancelled": ["操作结果", "操作结果"],
};
const actionChrome = (action: string, running: boolean): string => ACTION_CHROME[action]?.[running ? 1 : 0] ?? action;
const FAILURE_CHROME: Record<string, string> = { failed: "失败", blocked: "已阻止", cancelled: "已取消", "timed out": "超时" };

/** Render runtime protocol identifiers as user-facing MiniCode labels.
 * Runtime records keep the original name for execution, policy matching,
 * replay export, and diagnostics. */
export function readableToolLabel(value: string | undefined, isRunning = false): string {
  const text = (value ?? "").trim();
  const completion = text.match(/^Completed:\s*(.+)$/i);
  if (completion) return readableToolLabel(completion[1], isRunning);
  const failure = text.match(/^(Failed|Blocked|Cancelled|Timed out):\s*(.+)$/i);
  if (failure) return `${FAILURE_CHROME[failure[1].toLowerCase()]}：${readableToolLabel(failure[2])}`;

  const mcpName = text.match(/^mcp__([A-Za-z0-9_.-]+?)__([A-Za-z0-9_.-]+)(?=$|[\s,:·(])/);
  if (mcpName) return `${mcpName[1]}.${mcpName[2]}${text.slice(mcpName[0].length)}`;
  const joinedWebNames = text.match(/^(?:(?:webfetch|web_fetch|web_search)[\s,·]*)+$/i)?.[0];
  if (joinedWebNames) {
    const actions = [...new Set(joinedWebNames.match(/webfetch|web_fetch|web_search/gi)!.map(name => BUILTIN_TOOL_LABELS[name.toLowerCase()]))];
    return `${actions.map((action) => actionChrome(action, isRunning)).join(" · ")}${text.slice(joinedWebNames.length)}`;
  }

  // Only translate the leading render label. Paths, commands, URLs and MCP
  // identifiers in the remainder are source evidence, not words to rewrite.
  const protocolName = text.match(BUILTIN_TOOL_NAME_RE)?.[0];
  const runtimeAction = text.match(RUNTIME_ACTION_RE)?.[0];
  const matched = protocolName || runtimeAction;
  if (!matched) return text;
  const action = protocolName
    ? BUILTIN_TOOL_LABELS[protocolName.toLowerCase()]
    : RUNTIME_ACTION_LABELS[runtimeAction!];
  return actionChrome(action, isRunning) + text.slice(matched.length);
}
