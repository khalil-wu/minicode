import { File, Folder, Sparkles } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { isDesktop, fsSearchFiles } from "../desktop/runtime";
import { useAppStore } from "../stores";
import { listWorkspaceTree, searchWorkspaceFiles } from "../protocol/workspace";
import { fuzzyFilter } from "../lib/fuzzy-match";

interface Props {
  open: boolean;
  kind: "slash" | "mention";
  filter?: string;
  onSelect: (value: string) => void;
}

interface FileItem {
  name: string;
  description: string;
  type: "file" | "folder";
  path: string;
}

interface MenuItem {
  name: string;
  description: string;
  type?: "file" | "folder" | "skill";
  path?: string;
}

const FALLBACK_SLASH_COMMANDS: { name: string; description: string }[] = [
  { name: "/review", description: "Review code changes" },
  { name: "/debug", description: "Debug the current issue" },
  { name: "/refactor", description: "Refactor safely" },
  { name: "/test", description: "Add or update tests" },
  { name: "/docs", description: "Write developer docs" },
  { name: "/explain", description: "Explain code paths" },
  { name: "/commit", description: "Prepare a commit summary" },
  { name: "/skills", description: "Browse skills" },
  { name: "/permissions", description: "Inspect or change permissions" },
  { name: "/effort", description: "Set reasoning effort" },
  { name: "/new", description: "Start a new conversation" },
  { name: "/clear", description: "Clear conversation" },
  { name: "/compact", description: "Compact context" },
  { name: "/memory", description: "Set memory mode" },
  { name: "/archive", description: "Archive conversation" },
  { name: "/unarchive", description: "Unarchive conversation" },
  { name: "/tasks", description: "Show running tasks" },
  { name: "/status", description: "Show runtime status" },
  { name: "/usage", description: "Show token usage" },
  { name: "/help", description: "Show slash command help" },
];

const SLASH_ORDER = FALLBACK_SLASH_COMMANDS.map((item) => item.name);
const FALLBACK_DESCRIPTION = new Map(FALLBACK_SLASH_COMMANDS.map((item) => [item.name, item.description]));

export const MenuOverlay = ({ open, kind, filter, onSelect }: Props) => {
  const [activeIndex, setActiveIndex] = useState(0);
  const [fileResults, setFileResults] = useState<FileItem[]>([]);
  const [searching, setSearching] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Array<HTMLDivElement | null>>([]);
  const storeCommands = useAppStore((s) => s.slashCommands);
  const availableSkills = useAppStore((s) => s.availableSkills);
  const slashFilter = filter ?? "";

  // Slash commands
  const rawSlashItems = storeCommands.length > 0
    ? storeCommands.map((c) => {
        const name = c.label?.startsWith("/") ? c.label : `/${c.command}`;
        return { name, description: c.description || FALLBACK_DESCRIPTION.get(name) || "" };
      })
    : FALLBACK_SLASH_COMMANDS;
  const slashBaseItems = rawSlashItems
    .filter((item, index, list) => list.findIndex((other) => other.name === item.name) === index)
    .sort((a, b) => {
      const ai = SLASH_ORDER.indexOf(a.name);
      const bi = SLASH_ORDER.indexOf(b.name);
      if (ai !== -1 || bi !== -1) return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
      return a.name.localeCompare(b.name);
    });

  const slashCommandItems = slashFilter && slashFilter !== "/"
    ? fuzzyFilter(slashBaseItems, slashFilter.replace(/^\//, ""), (c) => c.name + " " + c.description)
    : slashBaseItems;
  const slashItems = slashCommandItems.slice(0, 18);

  // Mention mode: extract query from @<query>
  const mentionQuery = kind === "mention" ? (filter ?? "").replace(/^@/, "").trim() : "";
  const skillItems: MenuItem[] = kind === "mention"
    ? fuzzyFilter(
        availableSkills.map((skill) => ({
          name: `@${skill.name}`,
          description: skill.description || "Skill",
          type: "skill" as const,
          path: `skill:${skill.name}`,
        })),
        mentionQuery,
        (skill) => `${skill.name} ${skill.description}`,
      ).slice(0, 8)
    : [];

  useEffect(() => {
    setActiveIndex(0);
  }, [open, kind, filter]);

  // File search effect for @ mentions
  useEffect(() => {
    if (!open || kind !== "mention") return;

    setSearching(true);

    if (!mentionQuery) {
      listWorkspaceTree(".")
        .then((tree) => {
          const children = tree?.children ?? [];
          setFileResults(children.slice(0, 8).map((node) => ({
            name: node.name || node.path,
            description: node.path,
            type: node.is_dir ? "folder" as const : "file" as const,
            path: `${node.is_dir ? "folder" : "file"}:${node.path}`,
          })));
        })
        .catch(() => setFileResults([]))
        .finally(() => setSearching(false));
      return;
    }

    if (isDesktop()) {
      const root = useAppStore.getState().workingDirectory || "";
      fsSearchFiles(root, mentionQuery, 10, "all")
        .then((files) => {
          setFileResults(files.map((f) => {
            const type = f.kind === "folder" || f.path.endsWith("/") || f.path.endsWith("\\") ? "folder" as const : "file" as const;
            return { name: f.name || f.path, description: f.path.replace(/[\\/][^\\/]*$/, ""), type, path: `${type}:${f.path}` };
          }));
        })
        .catch(() => undefined)
        .finally(() => setSearching(false));
    } else {
      searchWorkspaceFiles(mentionQuery, 10, "all")
        .then((files) => {
          setFileResults(files.map((f) => ({
            name: f.name || f.path,
            description: f.path.replace(/[\\/][^\\/]*$/, ""),
            type: f.kind === "folder" ? "folder" as const : "file" as const,
            path: `${f.kind === "folder" ? "folder" : "file"}:${f.path}`,
          })));
        })
        .catch(() => undefined)
        .finally(() => setSearching(false));
    }
  }, [open, kind, mentionQuery]);

  const items: MenuItem[] =
    kind === "slash"
      ? slashItems.slice(0, 18)
      : [...skillItems, ...fileResults].slice(0, 18);

  useEffect(() => {
    if (!open) return;
    itemRefs.current = itemRefs.current.slice(0, items.length);
    const activeItem = itemRefs.current[activeIndex];
    activeItem?.scrollIntoView({ block: "nearest" });
  }, [open, activeIndex, items.length]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (!open) return;
      if (["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(e.key)) {
        e.stopPropagation();
      }
      if (items.length === 0) {
        if (e.key === "Escape") {
          e.preventDefault();
          onSelect("");
        }
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIndex((i) => (i + 1) % items.length);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIndex((i) => (i - 1 + items.length) % items.length);
      } else if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        if (items[activeIndex]) onSelect(items[activeIndex].path ?? items[activeIndex].name);
      } else if (e.key === "Escape") {
        e.preventDefault();
        onSelect("");
      }
    },
    [open, items, activeIndex, onSelect],
  );

  useEffect(() => {
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, [handleKeyDown]);

  if (!open) return null;

  const activeItem = items[activeIndex];

  return (
    <div
      style={{
        position: "absolute",
        bottom: "calc(100% + 10px)",
        left: kind === "slash" ? 0 : 12,
        zIndex: 50,
        display: "flex",
        alignItems: "flex-start",
        gap: 0,
      }}
    >
      <div
        ref={listRef}
        role="listbox"
        style={menuListStyle}
      >
        {items.length === 0 ? (
          <div style={emptyMenuStyle}>
            {searching ? "Searching..." : kind === "mention" ? "No files found" : "No matches"}
          </div>
        ) : (
          items.map((it, i) => (
            <div
              key={it.path ?? it.name}
              ref={(el) => { itemRefs.current[i] = el; }}
              role="option"
              aria-selected={i === activeIndex}
              onMouseEnter={() => setActiveIndex(i)}
              onClick={() => onSelect(it.path ?? it.name)}
              style={{
                ...menuItemStyle,
                background: i === activeIndex ? "var(--surface-page)" : "transparent",
              }}
            >
              {"type" in it && it.type && (
                <span style={menuIconStyle}>
                  {it.type === "folder" ? <Folder size={14} /> : it.type === "skill" ? <Sparkles size={14} /> : <File size={14} />}
                </span>
              )}
              <span style={menuNameStyle}>{it.name}</span>
              {kind === "slash" && it.description && <span style={menuDescriptionStyle}>{it.description}</span>}
              {i === activeIndex && <span style={menuShortcutStyle}>Enter</span>}
            </div>
          ))
        )}
      </div>
      {activeItem?.description && kind === "mention" && (
        <div style={tooltipStyle}>
          {activeItem.description}
        </div>
      )}
    </div>
  );
};

const menuListStyle: React.CSSProperties = {
  width: 280,
  background: "var(--surface-raised)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
  boxShadow: "var(--shadow-md)",
  padding: 4,
  maxHeight: "min(320px, calc(100vh - 230px))",
  overflowY: "auto",
  overscrollBehavior: "contain",
};

const emptyMenuStyle: React.CSSProperties = {
  padding: "8px 10px",
  color: "var(--text-muted)",
  fontSize: "var(--text-sm)",
};

const menuItemStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minHeight: 34,
  padding: "0 9px",
  fontSize: "var(--text-sm)",
  cursor: "pointer",
  borderRadius: "var(--radius-sm, 5px)",
  color: "var(--text-primary)",
};

const menuIconStyle: React.CSSProperties = {
  width: 16,
  display: "inline-flex",
  justifyContent: "center",
  color: "var(--text-muted)",
  flexShrink: 0,
};

const menuNameStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  fontFamily: "var(--font-ui)",
  fontWeight: 520,
};

const menuDescriptionStyle: React.CSSProperties = {
  flex: "0 1 170px",
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
};

const menuShortcutStyle: React.CSSProperties = {
  flexShrink: 0,
  color: "var(--text-muted)",
  fontSize: 11,
  fontFamily: "var(--font-mono)",
};

const tooltipStyle: React.CSSProperties = {
  maxWidth: 480,
  padding: "8px 10px",
  background: "color-mix(in oklch, var(--text-primary) 88%, black)",
  color: "var(--surface-page)",
  borderRadius: "var(--radius-sm, 6px)",
  boxShadow: "var(--shadow-md)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.35,
};
