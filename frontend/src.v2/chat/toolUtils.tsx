import {
  Code2,
  FileSearch,
  FileText,
  Globe,
  ListChecks,
  PencilLine,
  Search,
  TerminalSquare,
  Wrench,
} from "lucide-react";

export const ToolGlyph = ({
  kind,
  size = 15,
  className,
}: {
  kind?: string;
  size?: number;
  className?: string;
}) => {
  const normalized = String(kind || "").trim().toLowerCase();
  const props = { size, className: ["mc-tool-glyph", className].filter(Boolean).join(" ") };
  if (normalized === "websearch" || normalized === "search") return <Search {...props} />;
  if (normalized === "web" || normalized === "browser" || normalized === "preview") return <Globe {...props} />;
  if (normalized === "commandexecution" || normalized === "command") return <TerminalSquare {...props} />;
  if (normalized === "filechange" || normalized === "edit") return <PencilLine {...props} />;
  if (normalized === "fileread" || normalized === "file") return <FileText {...props} />;
  if (normalized === "workspacelist") return <ListChecks {...props} />;
  if (normalized === "workspacesearch") return <FileSearch {...props} />;
  if (normalized === "skill" || normalized === "plan") return <ListChecks {...props} />;
  if (normalized === "code") return <Code2 {...props} />;
  return <Wrench {...props} />;
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
