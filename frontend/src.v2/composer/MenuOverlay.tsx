import { File, Folder, Sparkles } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { isDesktop, fsListTree, fsSearchFiles } from "../desktop/runtime";
import { useAppStore } from "../stores";
import { listWorkspaceTree, searchWorkspaceFiles } from "../protocol/workspace";
import { fuzzyFilter } from "../lib/fuzzy-match";
import { buildRuntimeSlashArgMenuItems, buildRuntimeSlashMenuItems } from "../lib/runtime-commands";

interface Props {
  open: boolean;
  kind: "slash" | "mention";
  filter?: string;
  onSelect: (value: string) => void;
  placement?: "above" | "below";
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

const MENTION_SEARCH_DEBOUNCE_MS = 150;
const MENTION_CACHE_LIMIT = 80;
const mentionTreeCache = new Map<string, FileItem[]>();
const mentionSearchCache = new Map<string, FileItem[]>();

export const __clearMentionFileCacheForTests = () => {
  mentionTreeCache.clear();
  mentionSearchCache.clear();
};

const rememberMentionResults = (cache: Map<string, FileItem[]>, key: string, results: FileItem[]) => {
  if (!cache.has(key) && cache.size >= MENTION_CACHE_LIMIT) {
    const oldestKey = cache.keys().next().value;
    if (oldestKey) cache.delete(oldestKey);
  }
  cache.set(key, results);
};

export const MenuOverlay = ({ open, kind, filter, onSelect, placement = "above" }: Props) => {
  const [activeIndex, setActiveIndex] = useState(0);
  const [fileResults, setFileResults] = useState<FileItem[]>([]);
  const [searching, setSearching] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Array<HTMLDivElement | null>>([]);
  const searchSequenceRef = useRef(0);
  const storeCommands = useAppStore((s) => s.slashCommands);
  const availableSkills = useAppStore((s) => s.availableSkills);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const slashFilter = filter ?? "";

  // Slash commands
  const slashBaseItems = buildRuntimeSlashMenuItems(storeCommands);
  const slashSkillItems: MenuItem[] = availableSkills.map((skill) => ({
    name: `/${skill.name}`,
    description: skill.description || "Skill",
    type: "skill" as const,
    path: `/${skill.name}`,
  }));
  const skillsPickerActive = /^\/skills(?:\s|$)/i.test(slashFilter);

  // Argument stage: "/effort" or "/effort lo" with a local command that
  // declares args shows its argument completions instead of command matches.
  const slashArgItems = kind === "slash" && !skillsPickerActive
    ? buildRuntimeSlashArgMenuItems(slashFilter, storeCommands)
    : null;

  const slashCommandItems = slashArgItems
    ? slashArgItems
    : skillsPickerActive
    ? fuzzyFilter(
        slashSkillItems,
        slashFilter.replace(/^\/skills\s*/i, "").replace(/^\//, ""),
        (c) => c.name + " " + c.description,
      )
    : slashFilter && slashFilter !== "/"
      ? fuzzyFilter([...slashBaseItems, ...slashSkillItems], slashFilter.replace(/^\//, ""), (c) => c.name + " " + c.description)
      : [...slashBaseItems, ...slashSkillItems];
  const slashItems = slashCommandItems.slice(0, 18);

  // Mention mode: extract query from @<query>
  const mentionQuery = kind === "mention" ? (filter ?? "").replace(/^@/, "").trim() : "";
  const mentionSearchQuery = stripLineAnchor(mentionQuery);
  const skillItems: MenuItem[] = kind === "mention"
    ? fuzzyFilter(
        availableSkills.map((skill) => ({
          name: `@${skill.name}`,
          description: skill.description || "Skill",
          type: "skill" as const,
          path: `skill:${skill.name}`,
        })),
        mentionSearchQuery,
        (skill) => `${skill.name} ${skill.description}`,
      ).slice(0, 8)
    : [];

  useEffect(() => {
    setActiveIndex(0);
  }, [open, kind, filter]);

  // File search effect for @ mentions
  useEffect(() => {
    if (!open || kind !== "mention") {
      searchSequenceRef.current += 1;
      setSearching(false);
      return;
    }

    const searchId = ++searchSequenceRef.current;
    const desktopMode = isDesktop();
    const root = workingDirectory || "";
    const cacheScope = desktopMode ? `desktop:${root}` : "web";

    if (!mentionSearchQuery) {
      const cacheKey = `${cacheScope}:tree`;
      const cached = mentionTreeCache.get(cacheKey);
      if (cached) {
        setFileResults(cached);
        setSearching(false);
        return;
      }
      setSearching(true);
      if (desktopMode && workingDirectory) {
        fsListTree(workingDirectory)
          .then((entries) => {
            if (searchId !== searchSequenceRef.current) return; // Stale result
            const results = entries.slice(0, 8).map((entry) => {
              const type = entry.isDirectory ? "folder" as const : "file" as const;
              return {
                name: entry.name || entry.path,
                description: entry.path.replace(/[\\/][^\\/]*$/, ""),
                type,
                path: `${type}:${entry.path}`,
              };
            });
            rememberMentionResults(mentionTreeCache, cacheKey, results);
            setFileResults(results);
          })
          .catch(() => {
            if (searchId !== searchSequenceRef.current) return;
            setFileResults([]);
          })
          .finally(() => {
            if (searchId !== searchSequenceRef.current) return;
            setSearching(false);
          });
        return;
      }
      listWorkspaceTree(".")
        .then((tree) => {
          if (searchId !== searchSequenceRef.current) return; // Stale result
          const children = tree?.children ?? [];
          const results = children.slice(0, 8).map((node) => ({
            name: node.name || node.path,
            description: node.path,
            type: node.is_dir ? "folder" as const : "file" as const,
            path: `${node.is_dir ? "folder" : "file"}:${node.path}`,
          }));
          rememberMentionResults(mentionTreeCache, cacheKey, results);
          setFileResults(results);
        })
        .catch(() => {
          if (searchId !== searchSequenceRef.current) return;
          setFileResults([]);
        })
        .finally(() => {
          if (searchId !== searchSequenceRef.current) return;
          setSearching(false);
        });
      return;
    }

    const cacheKey = `${cacheScope}:search:${mentionSearchQuery}`;
    const cached = mentionSearchCache.get(cacheKey);
    if (cached) {
      setFileResults(cached);
      setSearching(false);
      return;
    }

    setFileResults([]);
    setSearching(true);
    const timer = window.setTimeout(() => {
      if (searchId !== searchSequenceRef.current) return;

      if (desktopMode) {
        fsSearchFiles(workingDirectory || "", mentionSearchQuery, 10, "all")
          .then((files) => {
            if (searchId !== searchSequenceRef.current) return; // Stale result
            const results = files.map((f) => {
              const type = f.kind === "folder" || f.path.endsWith("/") || f.path.endsWith("\\") ? "folder" as const : "file" as const;
              return { name: f.name || f.path, description: f.path.replace(/[\\/][^\\/]*$/, ""), type, path: `${type}:${f.path}` };
            });
            rememberMentionResults(mentionSearchCache, cacheKey, results);
            setFileResults(results);
          })
          .catch(() => {
            if (searchId !== searchSequenceRef.current) return;
            setFileResults([]);
          })
          .finally(() => {
            if (searchId !== searchSequenceRef.current) return;
            setSearching(false);
          });
      } else {
        searchWorkspaceFiles(mentionSearchQuery, 10, "all")
          .then((files) => {
            if (searchId !== searchSequenceRef.current) return; // Stale result
            const results = files.map((f) => ({
              name: f.name || f.path,
              description: f.path.replace(/[\\/][^\\/]*$/, ""),
              type: f.kind === "folder" ? "folder" as const : "file" as const,
              path: `${f.kind === "folder" ? "folder" : "file"}:${f.path}`,
            }));
            rememberMentionResults(mentionSearchCache, cacheKey, results);
            setFileResults(results);
          })
          .catch(() => {
            if (searchId !== searchSequenceRef.current) return;
            setFileResults([]);
          })
          .finally(() => {
            if (searchId !== searchSequenceRef.current) return;
            setSearching(false);
          });
      }
    }, MENTION_SEARCH_DEBOUNCE_MS);

    return () => window.clearTimeout(timer);
  }, [open, kind, mentionSearchQuery, workingDirectory]);

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

  return (
    <div
      style={{
        position: "absolute",
        bottom: placement === "above" ? "calc(100% + 10px)" : undefined,
        top: placement === "below" ? "calc(100% + 10px)" : undefined,
        left: kind === "slash" ? 0 : 8,
        zIndex: 50,
        maxWidth: "calc(100vw - 32px)",
      }}
    >
      <div
        ref={listRef}
        role="listbox"
        style={menuListStyle(kind)}
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
    </div>
  );
};

const menuListStyle = (kind: Props["kind"]): React.CSSProperties => ({
  width: kind === "mention" ? "min(320px, calc(100vw - 56px))" : "min(420px, calc(100vw - 56px))",
  background: "var(--surface-raised)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md, 8px)",
  boxShadow: "var(--shadow-soft)",
  padding: 6,
  maxHeight: "min(270px, calc(100vh - 230px))",
  overflowY: "auto",
  overflowX: "hidden",
  overscrollBehavior: "contain",
});

const emptyMenuStyle: React.CSSProperties = {
  padding: "8px 10px",
  color: "var(--text-muted)",
  fontSize: "var(--text-sm)",
};

const menuItemStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  minHeight: 36,
  padding: "0 8px",
  fontSize: "var(--text-sm)",
  cursor: "pointer",
  borderRadius: "var(--radius-sm, 5px)",
  color: "var(--text-primary)",
  minWidth: 0,
};

const menuIconStyle: React.CSSProperties = {
  width: 16,
  display: "inline-flex",
  justifyContent: "center",
  color: "var(--text-muted)",
  flexShrink: 0,
};

const menuNameStyle: React.CSSProperties = {
  flex: "1 1 auto",
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

function stripLineAnchor(value: string): string {
  return value.replace(/#L?\d+(?:-L?\d+)?$/i, "");
}
