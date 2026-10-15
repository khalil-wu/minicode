import { useI18n, type Translate } from "../i18n";
import { AtSign, Blocks, Command, Folder, RefreshCw } from "lucide-react";
import { BrandIcon } from "../components/BrandIcon";
import { fileIcon } from "../lib/file-icons";
import { useCallback, useEffect, useRef, useState } from "react";
import { isDesktop, fsListTree, fsSearchFiles } from "../desktop/runtime";
import { useAppStore } from "../stores";
import { listWorkspaceTree, searchWorkspaceFiles } from "../protocol/workspace";
import { fuzzyFilter } from "../lib/fuzzy-match";
import { buildRuntimeSlashArgMenuItems, buildRuntimeSlashMenuItems } from "../lib/runtime-commands";
import { mentionSearchCache, mentionTreeCache, type MentionFileItem } from "./mentionCache";
import { workspaceFilePathComparisonKey } from "../lib/workspace-path";
import { useComposerPlugins } from "./useComposerPlugins";
import { recentGuardianDenials, useGuardianApprovalState } from "../chat/guardianApprovalState";

interface Props {
  open: boolean;
  kind: "slash" | "mention" | "skill";
  filter?: string;
  onSelect: (value: string) => void;
  placement?: "above" | "below";
  workspaceRoot?: string;
}

type FileItem = MentionFileItem;

interface MenuItem {
  name: string;
  description: string;
  type?: "file" | "folder" | "plugin" | "skill" | "command" | "argument";
  path?: string;
  section?: string;
  keywords?: string[];
  displayName?: string;
  icon?: string;
  sourceLevel?: string;
  active?: boolean;
  allowImplicitInvocation?: boolean;
  mcpDependencies?: string[];
  defaultPrompt?: string;
  skillPath?: string;
  marketplace?: string;
}

const MENTION_SEARCH_DEBOUNCE_MS = 150;
const MENTION_CACHE_LIMIT = 80;
const SLASH_MENU_LIMIT = 18;
const ROOT_SLASH_SKILL_PREVIEW_LIMIT = 6;

const rememberMentionResults = (cache: Map<string, FileItem[]>, key: string, results: FileItem[]) => {
  if (!cache.has(key) && cache.size >= MENTION_CACHE_LIMIT) {
    const oldestKey = cache.keys().next().value;
    if (oldestKey) cache.delete(oldestKey);
  }
  cache.set(key, results);
};

export const MenuOverlay = ({ open, kind, filter, onSelect, placement = "above", workspaceRoot }: Props) => {
  const { t: tr } = useI18n();
  const [activeIndex, setActiveIndex] = useState(0);
  const [fileResults, setFileResults] = useState<FileItem[]>([]);
  const [searching, setSearching] = useState(false);
  const [fileSearchError, setFileSearchError] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<Array<HTMLDivElement | null>>([]);
  const searchSequenceRef = useRef(0);
  const storeCommands = useAppStore((s) => s.slashCommands);
  const guardianOwner = useAppStore(s => s.conversationId ?? "");
  const guardianMessages = useAppStore(s => s.messages);
  const guardianState = useGuardianApprovalState(guardianOwner);
  const hasGuardianDenials = guardianMessages.length > 0 && recentGuardianDenials(guardianOwner, guardianState.approved).length > 0;
  const availableSkills = useAppStore((s) => s.availableSkills);
  const selectedSkills = useAppStore((s) => s.selectedSkills);
  const workingDirectory = useAppStore((s) => workspaceRoot ?? s.workingDirectory);
  const { plugins: pluginResults, loading: pluginLoading, error: pluginLoadError, refresh: refreshPlugins } = useComposerPlugins(open && kind === "mention", workingDirectory);
  const fileTreeVersion = useAppStore((s) => s.fileTreeVersion);
  const slashFilter = filter ?? "";
  const emptyReviewComposer = useAppStore(s => !s.draft.trim() || /^\s*\/[^/\r\n]*\s*$/.test(s.draft));
  const skillKey = (path: string | undefined, name: string): string => path
    ? `path:${workspaceFilePathComparisonKey(path, workingDirectory)}`
    : `name:${name}`;
  const selectedSkillKeys = new Set(selectedSkills.map((skill) => skillKey(skill.path, skill.name)));

  // Slash commands and explicit skill picker
  const slashBaseItems: MenuItem[] = buildRuntimeSlashMenuItems(storeCommands)
    .filter(item => item.name !== "/approve" || hasGuardianDenials || storeCommands.find(command => command.name === "approve")?.source !== "builtin")
    .filter(item => item.name !== "/review" || emptyReviewComposer || !storeCommands.some(command => command.name === "review" && command.source === "builtin"))
    .map((item) => ({
    ...item,
    description: item.name === "/approve" && storeCommands.find(command => command.name === "approve")?.source === "builtin"
      ? tr("批准最近一次自动审查驳回") : item.description,
    type: "command" as const,
    section: item.section ?? tr("命令"),
    path: item.name,
  }));
  const slashSkillItems: MenuItem[] = availableSkills
    .filter((skill) => skill.user_invocable !== false)
    .map((skill) => ({
      name: `/${skill.name}`,
      description: skill.short_description || skill.description || tr("技能"),
      type: "skill" as const,
      path: skill.path ? `skill-path:${encodeURIComponent(skill.path)}` : `skill-name:${encodeURIComponent(skill.name)}`,
      section: tr("技能"),
      displayName: skill.display_name,
      icon: skill.icon,
      sourceLevel: skill.source_level,
      active: Boolean(skill.active || selectedSkillKeys.has(skillKey(skill.path, skill.name))),
      allowImplicitInvocation: skill.allow_implicit_invocation,
      mcpDependencies: skill.mcp_dependencies,
      defaultPrompt: skill.default_prompt,
      skillPath: skill.path,
    }));
  const skillsPickerActive = /^\/skills?\s+/i.test(slashFilter);
  const explicitSkillPickerActive = kind === "skill";

  // Argument stage: "/effort" or "/effort lo" with a local command that
  // declares args shows its argument completions instead of command matches.
  const slashArgItems = kind === "slash" && !skillsPickerActive
    ? buildRuntimeSlashArgMenuItems(slashFilter, storeCommands)?.map((item) => ({
        ...item,
        type: "argument" as const,
        section: tr("选项"),
        path: item.name,
      }))
    : null;

  const slashCommandItems = slashArgItems
    ? slashArgItems
    : skillsPickerActive
    ? fuzzyFilter(
        slashSkillItems,
        slashFilter.replace(/^\/skills?\s*/i, "").replace(/^\//, ""),
        (c) => [c.name, c.displayName, c.description, c.defaultPrompt, ...(c.mcpDependencies ?? [])].filter(Boolean).join(" "),
      )
    : slashFilter && slashFilter !== "/"
      ? fuzzyFilter(
          [...slashBaseItems, ...slashSkillItems],
          slashFilter.replace(/^\//, ""),
          (c) => [c.name, c.description, c.section, ...(c.keywords ?? [])].filter(Boolean).join(" "),
        )
      : balancedRootSlashItems(slashBaseItems, slashSkillItems);
  const slashItems = prioritizeExactSlashCommand(slashCommandItems, slashFilter)
    .slice(0, SLASH_MENU_LIMIT);

  // Mention mode: extract query from @<query>
  const mentionQuery = kind === "mention" ? (filter ?? "").replace(/^@/, "").trim() : "";
  const mentionSearchQuery = stripLineAnchor(mentionQuery);
  const explicitSkillItems: MenuItem[] = kind === "skill"
    ? fuzzyFilter(
        availableSkills.filter((skill) => skill.user_invocable !== false).map((skill) => ({
          name: `$${skill.name}`,
          description: skill.short_description || skill.description || tr("技能"),
          type: "skill" as const,
          path: skill.path ? `skill-path:${encodeURIComponent(skill.path)}` : `skill-name:${encodeURIComponent(skill.name)}`,
          section: tr("技能"),
          displayName: skill.display_name,
          icon: skill.icon,
          sourceLevel: skill.source_level,
          active: Boolean(skill.active || selectedSkillKeys.has(skillKey(skill.path, skill.name))),
          allowImplicitInvocation: skill.allow_implicit_invocation,
          mcpDependencies: skill.mcp_dependencies,
          defaultPrompt: skill.default_prompt,
          skillPath: skill.path,
        })),
        (filter ?? "").replace(/^\$/, ""),
        (skill) => [skill.name, skill.displayName, skill.description, skill.defaultPrompt, ...(skill.mcpDependencies ?? [])].filter(Boolean).join(" "),
      ).slice(0, 18)
    : [];

  const mentionPluginItems: MenuItem[] = kind === "mention"
    ? fuzzyFilter(
        pluginResults
          .filter((plugin) => plugin.enabled)
          .map((plugin) => ({
            name: `@${plugin.displayName || plugin.name}`,
            displayName: plugin.displayName || plugin.name,
            description: plugin.shortDescription || plugin.description || [
              plugin.skill_count ? tr("{count} 个技能", { count: plugin.skill_count }) : "",
              plugin.mcp_server_count ? tr("{count} 个 MCP 服务", { count: plugin.mcp_server_count }) : "",
            ].filter(Boolean).join(" · ") || tr("插件"),
            type: "plugin" as const,
            path: `plugin:${encodeURIComponent(plugin.id)}`,
            marketplace: plugin.marketplace,
            section: tr("插件"),
          })),
        mentionSearchQuery,
        (plugin) => [plugin.name, plugin.displayName, plugin.description].filter(Boolean).join(" "),
      ).slice(0, 8)
    : [];

  useEffect(() => {
    setActiveIndex(0);
  }, [open, kind, filter, workingDirectory]);

  // File search effect for @ mentions
  useEffect(() => {
    if (!open || kind !== "mention") {
      searchSequenceRef.current += 1;
      setSearching(false);
      setFileSearchError("");
      return;
    }

    const searchId = ++searchSequenceRef.current;
    const invalidate = () => { searchSequenceRef.current += 1; };
    const desktopMode = isDesktop();
    const root = workingDirectory || "";
    const cacheScope = `${desktopMode ? "desktop" : "web"}:${root}:revision:${fileTreeVersion}`;
    setFileSearchError("");
    setFileResults([]);

    if (!mentionSearchQuery) {
      const cacheKey = `${cacheScope}:tree`;
      const cached = mentionTreeCache.get(cacheKey);
      if (cached) {
        setFileResults(cached);
        setSearching(false);
        return invalidate;
      }
      setSearching(true);
      if (desktopMode && workingDirectory) {
        Promise.resolve(fsListTree(workingDirectory))
          .then((entries) => {
            if (searchId !== searchSequenceRef.current) return; // Stale result
            const results = (entries ?? []).slice(0, 8).map((entry) => {
              const type = entry.isDirectory ? "folder" as const : "file" as const;
              return {
                name: entry.name || entry.path,
                description: entry.path.replace(/[\\/][^\\/]*$/, ""),
                type,
                section: tr("文件"),
                path: `${type}:${entry.path}`,
              };
            });
            rememberMentionResults(mentionTreeCache, cacheKey, results);
            setFileResults(results);
          })
          .catch((error: unknown) => {
            if (searchId !== searchSequenceRef.current) return;
            setFileResults([]);
            setFileSearchError(error instanceof Error ? error.message : tr("无法读取工作区文件，请重试。"));
          })
          .finally(() => {
            if (searchId !== searchSequenceRef.current) return;
            setSearching(false);
          });
        return invalidate;
      }
      listWorkspaceTree(workingDirectory, ".")
        .then((tree) => {
          if (searchId !== searchSequenceRef.current) return; // Stale result
          const children = tree?.children ?? [];
          const results = children.slice(0, 8).map((node) => ({
            name: node.name || node.path,
            description: node.path,
            type: node.is_dir ? "folder" as const : "file" as const,
            section: tr("文件"),
            path: `${node.is_dir ? "folder" : "file"}:${node.path}`,
          }));
          rememberMentionResults(mentionTreeCache, cacheKey, results);
          setFileResults(results);
        })
        .catch((error: unknown) => {
          if (searchId !== searchSequenceRef.current) return;
          setFileResults([]);
          setFileSearchError(error instanceof Error ? error.message : tr("无法读取工作区文件，请重试。"));
        })
        .finally(() => {
          if (searchId !== searchSequenceRef.current) return;
          setSearching(false);
        });
      return invalidate;
    }

    const cacheKey = `${cacheScope}:search:${mentionSearchQuery}`;
    const cached = mentionSearchCache.get(cacheKey);
    if (cached) {
      setFileResults(cached);
      setSearching(false);
      return invalidate;
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
              return { name: f.name || f.path, description: f.path.replace(/[\\/][^\\/]*$/, ""), type, section: tr("文件"), path: `${type}:${f.path}` };
            });
            rememberMentionResults(mentionSearchCache, cacheKey, results);
            setFileResults(results);
          })
          .catch((error: unknown) => {
            if (searchId !== searchSequenceRef.current) return;
            setFileResults([]);
            setFileSearchError(error instanceof Error ? error.message : tr("文件搜索失败，请重试。"));
          })
          .finally(() => {
            if (searchId !== searchSequenceRef.current) return;
            setSearching(false);
          });
      } else {
        searchWorkspaceFiles(workingDirectory, mentionSearchQuery, 10, "all")
          .then((files) => {
            if (searchId !== searchSequenceRef.current) return; // Stale result
            const results = files.map((f) => ({
              name: f.name || f.path,
              description: f.path.replace(/[\\/][^\\/]*$/, ""),
              type: f.kind === "folder" ? "folder" as const : "file" as const,
              section: tr("文件"),
              path: `${f.kind === "folder" ? "folder" : "file"}:${f.path}`,
            }));
            rememberMentionResults(mentionSearchCache, cacheKey, results);
            setFileResults(results);
          })
          .catch((error: unknown) => {
            if (searchId !== searchSequenceRef.current) return;
            setFileResults([]);
            setFileSearchError(error instanceof Error ? error.message : tr("文件搜索失败，请重试。"));
          })
          .finally(() => {
            if (searchId !== searchSequenceRef.current) return;
            setSearching(false);
          });
      }
    }, MENTION_SEARCH_DEBOUNCE_MS);

    return () => { window.clearTimeout(timer); invalidate(); };
  }, [open, kind, mentionSearchQuery, workingDirectory, fileTreeVersion]);

  const items: MenuItem[] =
    kind === "slash"
      ? slashItems.slice(0, SLASH_MENU_LIMIT)
      : kind === "skill"
        ? explicitSkillItems
      : [...mentionPluginItems, ...fileResults].slice(0, 18);

  useEffect(() => {
    if (!open) return;
    itemRefs.current = itemRefs.current.slice(0, items.length);
    const activeItem = itemRefs.current[activeIndex];
    activeItem?.scrollIntoView({ block: "nearest" });
  }, [open, activeIndex, items.length]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (!open || e.isComposing || e.keyCode === 229) return;
      const owner = listRef.current?.closest(".composer-container");
      if (owner && !owner.contains(e.target as Node)) return;
      if (e.key !== "Escape" && listRef.current?.contains(e.target as Node)) return;
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
        zIndex: "var(--z-dropdown)",
        maxWidth: "calc(100vw - 32px)",
      }}
    >
      <div
        ref={listRef}
        role="listbox"
        className="composer-menu-list mc-dropdown-menu"
        data-kind={kind}
        data-skills-picker={skillsPickerActive || explicitSkillPickerActive ? "true" : "false"}
        style={menuListStyle(kind, skillsPickerActive || explicitSkillPickerActive)}
      >
        {fileSearchError && <div role="alert" style={emptyMenuStyle}>{fileSearchError}</div>}
        {kind === "mention" && pluginLoadError && (
          <div role="alert" style={{ ...emptyMenuStyle, display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ flex: 1, overflowWrap: "anywhere" }}>{tr("插件加载失败：")}{pluginLoadError}</span>
            <button type="button" onClick={refreshPlugins} title={tr("重试加载插件")} aria-label={tr("重试加载插件")} className="mc-icon-btn">
              <RefreshCw size={14} />
            </button>
          </div>
        )}
        {items.length === 0 ? (
          fileSearchError || (kind === "mention" && pluginLoadError) ? null : <div style={emptyMenuStyle}>
            {searching || (kind === "mention" && pluginLoading)
              ? tr("正在搜索…")
              : kind === "mention"
                  ? tr("未找到文件或插件")
                  : (skillsPickerActive || explicitSkillPickerActive) ? tr("未找到技能") : tr("无匹配项")}
          </div>
        ) : (
          items.map((it, i) => {
            const showSection = i === 0 || items[i - 1]?.section !== it.section;
            const displayName = displayMenuName(it, skillsPickerActive || explicitSkillPickerActive);
            const sourceLabel = it.type === "skill" ? formatSourceLevel(it.sourceLevel, tr) : it.marketplace ?? "";
            const skillPolicyLabel = it.type === "skill" ? formatSkillPolicy(it, tr) : "";
            return (
              <div key={it.path ?? it.name}>
                {showSection && it.section && (
                  <div className="composer-menu-section-label" style={sectionLabelStyle}>
                    {it.section}
                  </div>
                )}
                <div
                  ref={(el) => { itemRefs.current[i] = el; }}
                  role="option"
                  aria-selected={i === activeIndex}
                  onMouseEnter={() => setActiveIndex(i)}
                  onClick={() => onSelect(it.path ?? it.name)}
                  className="composer-menu-item"
                  data-active={i === activeIndex ? "true" : "false"}
                  data-selected={it.active ? "true" : "false"}
                  style={{
                    ...menuItemStyle,
                    background: i === activeIndex ? "var(--surface-hover)" : "transparent",
                  }}
                >
                  <span style={menuIconStyle}>
                    {renderMenuIcon(it)}
                  </span>
                  <span style={menuBodyStyle}>
                    <span style={menuTitleRowStyle}>
                      <span style={menuNameStyle}>{displayName}</span>
                      {it.active && <span style={activeBadgeStyle}>{tr("已启用")}</span>}
                    </span>
                    {it.description && <span style={menuDescriptionStyle}>{it.description}</span>}
                    {skillPolicyLabel && <span style={menuTriggerStyle}>{skillPolicyLabel}</span>}
                  </span>
                  {sourceLabel && <span style={sourceBadgeStyle}>{sourceLabel}</span>}
                  {i === activeIndex && <span style={menuShortcutStyle}>Enter</span>}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};

const menuListStyle = (kind: Props["kind"], skillsPickerActive: boolean): React.CSSProperties => ({
  width: kind === "mention" ? "min(360px, calc(100vw - 56px))" : skillsPickerActive ? "min(520px, calc(100vw - 56px))" : "min(460px, calc(100vw - 56px))",
  background: "var(--surface-raised)",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-md)",
  boxShadow: "var(--shadow-medium)",
  padding: 5,
  maxHeight: skillsPickerActive ? "min(340px, calc(100vh - 230px))" : "min(292px, calc(100vh - 230px))",
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
  display: "grid",
  gridTemplateColumns: "18px minmax(0, 1fr) auto auto",
  alignItems: "center",
  gap: 8,
  minHeight: 42,
  padding: "5px 7px",
  fontSize: "var(--text-sm)",
  cursor: "pointer",
  borderRadius: "var(--radius-sm, 5px)",
  color: "var(--text-primary)",
  minWidth: 0,
};

const menuIconStyle: React.CSSProperties = {
  width: 18,
  display: "inline-flex",
  justifyContent: "center",
  color: "var(--text-muted)",
  flexShrink: 0,
};

const menuBodyStyle: React.CSSProperties = {
  display: "grid",
  gap: 1,
  minWidth: 0,
};

const menuTitleRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  minWidth: 0,
};

const menuNameStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  fontFamily: "var(--font-ui)",
  fontWeight: "var(--fw-semibold)",
};

const menuDescriptionStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  lineHeight: 1.35,
};

const menuTriggerStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-muted)",
  fontSize: "var(--text-2xs)",
  lineHeight: 1.3,
  fontFamily: "var(--font-ui)",
};

const menuShortcutStyle: React.CSSProperties = {
  flexShrink: 0,
  color: "var(--text-muted)",
  fontSize: "var(--text-2xs)",
  fontFamily: "var(--font-ui)",
};

const sectionLabelStyle: React.CSSProperties = {
  padding: "6px 7px 4px",
  color: "var(--text-muted)",
  fontSize: "var(--text-2xs)",
  fontWeight: "var(--fw-bold)",
  letterSpacing: 0,
  textTransform: "uppercase",
};

const sourceBadgeStyle: React.CSSProperties = {
  justifySelf: "end",
  minWidth: 0,
  maxWidth: 86,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  padding: "2px 5px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 5px)",
  color: "var(--text-muted)",
  background: "var(--surface-page)",
  fontSize: "var(--text-2xs)",
  lineHeight: 1.25,
  fontFamily: "var(--font-ui)",
};

const activeBadgeStyle: React.CSSProperties = {
  flexShrink: 0,
  color: "var(--accent-primary)",
  fontSize: "var(--text-2xs)",
  fontFamily: "var(--font-ui)",
};

function renderMenuIcon(item: MenuItem) {
  if (item.type === "folder") return <Folder size={14} />;
  if (item.type === "file") return fileIcon(item.name || item.path || "file", { size: 16, className: "composer-context-icon-svg" });
  if (item.type === "skill") return <BrandIcon value={item.displayName || item.name} iconUrl={item.icon} inferBrand={false} fallback="skill" size={14} />;
  if (item.type === "plugin") return <Blocks size={14} />;
  if (item.type === "argument") return <AtSign size={14} />;
  return <Command size={14} />;
}

function displayMenuName(item: MenuItem, skillsPickerActive: boolean): string {
  if (skillsPickerActive && item.type === "skill") return item.displayName || item.name.replace(/^[/$]/, "");
  return item.name;
}

function formatSkillPolicy(item: MenuItem, tr: Translate): string {
  const parts: string[] = [];
  if (item.allowImplicitInvocation === false) parts.push(tr("仅显式调用"));
  const mcp = (item.mcpDependencies ?? []).filter(Boolean);
  if (mcp.length > 0) parts.push(`MCP: ${mcp.slice(0, 2).join(", ")}`);
  return parts.join(" · ");
}

function formatSourceLevel(level: string | undefined, tr: Translate): string {
  const normalized = (level || "").replace(/-legacy$/i, "").toLowerCase();
  if (!normalized) return "";
  if (normalized === "global") return tr("个人");
  if (normalized === "builtin") return tr("内置");
  if (normalized === "project") return tr("项目");
  if (normalized === "user" || normalized === "personal") return tr("个人");
  return normalized;
}

function stripLineAnchor(value: string): string {
  return value.replace(/#L?\d+(?:-L?\d+)?$/i, "");
}

function balancedRootSlashItems(commands: MenuItem[], skills: MenuItem[]): MenuItem[] {
  if (skills.length === 0) return commands;
  const skillCount = Math.min(ROOT_SLASH_SKILL_PREVIEW_LIMIT, skills.length);
  const commandCount = Math.max(0, SLASH_MENU_LIMIT - skillCount);
  return [
    ...commands.slice(0, commandCount),
    ...skills.slice(0, skillCount),
  ];
}

function prioritizeExactSlashCommand(items: MenuItem[], filter: string): MenuItem[] {
  const exactName = filter.trim().toLowerCase();
  if (!/^\/[a-z0-9][a-z0-9._:-]*$/i.test(exactName)) return items;
  const exactIndex = items.findIndex((item) => (
    item.type === "command" && item.name.trim().toLowerCase() === exactName
  ));
  if (exactIndex <= 0) return items;
  return [items[exactIndex], ...items.slice(0, exactIndex), ...items.slice(exactIndex + 1)];
}
