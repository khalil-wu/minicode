import {
  Code2,
  FilePlus2,
  FileSearch,
  FileText,
  GitBranch,
  Globe,
  ListChecks,
  PencilLine,
  Search,
  TerminalSquare,
  Wrench,
} from "lucide-react";

export const ToolGlyph = ({
  name,
  size = 15,
  className,
}: {
  name: string;
  size?: number;
  className?: string;
}) => {
  // Inherit color from the parent so status/theme wrappers stay in control.
  const normalized = name.toLowerCase().replace(/[\s-]+/g, "_");
  const props = { size, className: ["mc-tool-glyph", className].filter(Boolean).join(" ") };
  if (normalized.includes("web_search") || normalized.includes("search_web") || normalized.includes("websearch")) return <Search {...props} />;
  if (normalized.includes("web_fetch") || normalized.includes("webfetch") || (normalized.includes("web") && normalized.includes("fetch"))) return <Globe {...props} />;
  if (normalized.includes("web")) return <Globe {...props} />;
  if (
    normalized.includes("command")
    || normalized.includes("terminal")
    || normalized.includes("bash")
    || normalized.includes("powershell")
    || normalized.includes("shell")
  ) return <TerminalSquare {...props} />;
  if (normalized.includes("apply_patch") || normalized.includes("patch")) return <PencilLine {...props} />;
  if (normalized.includes("write") || normalized.includes("create_file") || normalized.includes("create")) return <FilePlus2 {...props} />;
  if (normalized.includes("edit") || normalized.includes("delete") || normalized.includes("update_file")) return <PencilLine {...props} />;
  if (normalized.includes("read") || normalized.includes("open_file") || normalized.includes("file")) return <FileText {...props} />;
  if (normalized.includes("todo") || normalized.includes("list_checks") || normalized.includes("list")) return <ListChecks {...props} />;
  if (normalized.includes("grep") || normalized.includes("glob") || normalized.includes("search")) return <FileSearch {...props} />;
  if (normalized.includes("git")) return <GitBranch {...props} />;
  if (normalized.includes("code") || normalized.includes("symbol")) return <Code2 {...props} />;
  return <Wrench {...props} />;
};

export const toolDisplayName = (name: string): string => {
  if (name === "web_search" || name === "search_web") return "Search web";
  if (name === "web_fetch") return "Fetch page";
  if (name === "run_command") return "Run command";
  if (name === "read_file") return "Read file";
  if (name === "write_file") return "Write file";
  if (name === "edit_file") return "Edit file";
  if (name === "apply_patch") return "Apply patch";
  if (name === "grep_files" || name === "grep") return "Search files";
  if (name === "glob_files" || name === "glob") return "Scan files";
  if (name === "git_status") return "Check git";
  return name.replace(/_/g, " ");
};

export const summarizeArgs = (args: Record<string, unknown>): { label: string; value: string }[] => {
  const preferred = ["command", "cmd", "path", "file_path", "target", "filename", "query", "pattern", "url", "cwd"];
  const rows: { label: string; value: string }[] = [];
  for (const key of preferred) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) rows.push({ label: humanizeKey(key), value });
    else if (typeof value === "number" || typeof value === "boolean") rows.push({ label: humanizeKey(key), value: String(value) });
  }
  if (rows.length > 0) return rows.slice(0, 4);
  const fallback = Object.entries(args)
    .filter(([, value]) => typeof value === "string" || typeof value === "number" || typeof value === "boolean")
    .slice(0, 4)
    .map(([key, value]) => ({ label: humanizeKey(key), value: String(value) }));
  return fallback.length > 0 ? fallback : [{ label: "request", value: "No concise parameters available" }];
};

export const humanizeKey = (key: string) => key.replace(/_/g, " ");

export function extractToolFilePath(args: Record<string, unknown>): string | null {
  const path = args.file_path ?? args.path ?? args.target ?? args.filename;
  return typeof path === "string" ? path : null;
}

export function shortToolPath(fullPath: string): string {
  const parts = fullPath.replace(/\\/g, "/").split("/").filter(Boolean);
  if (parts.length <= 3) return parts.join("/");
  return `.../${parts.slice(-2).join("/")}`;
}

function readFileLineRangeSuffix(args: Record<string, unknown>): string {
  if (args.start_line == null && args.end_line == null && args.startLine == null && args.endLine == null) return "";
  const start = Number(args.start_line ?? args.startLine);
  const end = Number(args.end_line ?? args.endLine);
  if (Number.isFinite(start) && start > 0 && Number.isFinite(end) && end > 0) return ` L${start}-L${end}`;
  if (Number.isFinite(start) && start > 0) return ` L${start}+`;
  if (Number.isFinite(end) && end > 0) return ` L1-L${end}`;
  return "";
}

export function summarizeToolInput(name: string, args: Record<string, unknown>): string | null {
  const normalized = name.toLowerCase();
  const command = args.command ?? args.cmd;
  if ((normalized.includes("command") || normalized.includes("terminal")) && typeof command === "string") return command;
  const query = args.query ?? args.pattern;
  if ((normalized.includes("grep") || normalized.includes("glob") || normalized.includes("search") || normalized.includes("web")) && typeof query === "string") return query;
  const url = args.url;
  if (typeof url === "string") return url;
  const filePath = extractToolFilePath(args);
  if (filePath) {
    const base = shortToolPath(filePath);
    if (normalized === "read_file") return `${base}${readFileLineRangeSuffix(args)}`;
    return base;
  }
  const firstScalar = Object.values(args).find((value) => typeof value === "string" || typeof value === "number" || typeof value === "boolean");
  return firstScalar == null ? null : String(firstScalar);
}
