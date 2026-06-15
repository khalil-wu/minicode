import {
  Code2,
  FileText,
  Globe,
  Search,
  TerminalSquare,
  Wrench,
} from "lucide-react";

export const ToolGlyph = ({ name }: { name: string }) => {
  const props = { size: 15, color: "var(--state-warning)" };
  if (name.includes("web")) return <Globe {...props} />;
  if (name.includes("command") || name.includes("terminal") || name.includes("bash")) return <TerminalSquare {...props} />;
  if (name.includes("write") || name.includes("edit") || name.includes("patch")) return <Code2 {...props} />;
  if (name.includes("read") || name.includes("file")) return <FileText {...props} />;
  if (name.includes("grep") || name.includes("glob") || name.includes("search")) return <Search {...props} />;
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