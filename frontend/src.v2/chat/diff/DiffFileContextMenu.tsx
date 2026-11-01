import { createContext, useContext, useLayoutEffect, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import { ContextMenu, type ContextMenuItem } from "../../components/ContextMenu";
import { FileOpenTargetIcon } from "../../components/FileOpenTargetIcon";
import { isDesktop, runtime, type DesktopPreferences, type FileOpenTarget } from "../../desktop/runtime";
import { useI18n } from "../../i18n";
import { copyText } from "../../lib/clipboard";
import type { DiffLineLocation } from "../../lib/diff-comment-lines";
import { openPreferredFile, openPreferredWebLink, preferredFileTargetForWorkspace } from "../../lib/open-preferred-target";
import { isWindowsLikeWorkspacePath, normalizeWorkspacePath } from "../../lib/workspace-path";
import { openWorkspaceSourceFile } from "../../workspace/openWorkspaceFolder";
import { useDiffViewPreference } from "./diff-view-preferences";
import { useDesktopPreferenceTargets } from "../../hooks/useDesktopPreferenceTargets";

type RequestChanges = (location?: DiffLineLocation) => void;
const RequestChangesContext = createContext<MutableRefObject<RequestChanges | undefined> | null>(null);

/** Source requestChangesRef registers the current annotation action and releases only itself. */
export function useDiffRequestChanges(handler: RequestChanges): void {
  const action = useContext(RequestChangesContext);
  useLayoutEffect(() => {
    if (action === null) return;
    action.current = handler;
    return () => { if (action.current === handler) action.current = undefined; };
  }, [action, handler]);
}

function pathsForMenu(path: string, workspaceRoot: string) {
  const file = normalizeWorkspacePath(path), root = normalizeWorkspacePath(workspaceRoot);
  const absolute = file.startsWith("/") || /^[A-Za-z]:\//.test(file);
  const full = absolute ? file : root ? normalizeWorkspacePath(`${root}/${file}`) : undefined;
  if (!absolute) return { full, relative: file };
  if (!root) return { full, relative: undefined };
  const left = root.split("/").filter(Boolean), right = file.split("/").filter(Boolean);
  const windows = isWindowsLikeWorkspacePath(root);
  const key = (part: string) => windows ? part.toLowerCase() : part;
  if (windows && key(left[0]) !== key(right[0])) return { full, relative: file };
  let common = 0;
  while (common < left.length && common < right.length && key(left[common]) === key(right[common])) common++;
  return { full, relative: [...left.slice(common).map(() => ".."), ...right.slice(common)].join("/") || "." };
}

export interface DiffFileContextMenuProps {
  path: string;
  workspaceRoot: string;
  deleted?: boolean;
  firstChangedLine?: number;
  preferences: DesktopPreferences | null;
  githubUrl?: string;
  canOpenFile?: boolean;
  onOpenFile?: (line: number) => void;
  canRequestChanges?: boolean;
  children: ReactNode;
}

type MenuState = { position: { x: number; y: number }; location?: DiffLineLocation; selection: string };

/** Current file actions, real selection and the registered annotation utility. */
export function DiffFileContextMenu({ path, workspaceRoot, deleted = false, firstChangedLine = 1, preferences, githubUrl,
  canOpenFile = true, onOpenFile, canRequestChanges = true, children }: DiffFileContextMenuProps) {
  const { t, locale } = useI18n();
  const requestChanges = useRef<RequestChanges>();
  const [menu, setMenu] = useState<MenuState>();
  const { targets: openTargets } = useDesktopPreferenceTargets(Boolean(menu));
  const [wrap, setWrap] = useDiffViewPreference("wrap");
  const zh = locale === "zh-CN";
  const validPath = path.length > 0 && path !== "/dev/null";
  const canOpen = canOpenFile && validPath && workspaceRoot !== "";
  const paths = pathsForMenu(path, workspaceRoot);
  const targets = isDesktop() && !runtime()?.remoteHost ? openTargets?.fileOpenTargets ?? [] : [];
  const preferred = preferences ? preferredFileTargetForWorkspace(preferences, workspaceRoot) : undefined;
  const primary = canOpen ? targets.find(target => target.id === preferred) : undefined;
  const newTab = canOpen && !deleted;
  const nativeNewTab = targets.find(target => target.id === "minicode");
  const line = menu?.location?.line ?? firstChangedLine;
  const icon = (target: FileOpenTarget) => target.icon ? <FileOpenTargetIcon target={target} /> : undefined;
  const openTarget = (target: FileOpenTarget, persist: boolean) => {
    void openPreferredFile(path, workspaceRoot, line, undefined, { targetId: target.id,
      ...persist && target.kind === "editor" ? { persistPreferredTargetPath: workspaceRoot } : {} });
  };
  const items: ContextMenuItem[] = [
    ...canRequestChanges ? [{ label: zh ? "请求更改" : "Request changes", onClick: () => requestChanges.current?.(menu?.location) }] : [],
    ...primary ? [{ label: zh ? `在“${primary.label}”中打开` : `Open in ${primary.label}`, icon: icon(primary), onClick: () => openTarget(primary, false) }] : [],
    ...primary || newTab ? [{ label: zh ? "打开方式" : "Open with", submenu: [
      ...newTab ? [{ label: t("新标签页"), icon: nativeNewTab ? icon(nativeNewTab) : undefined,
        onClick: () => onOpenFile ? onOpenFile(line) : openWorkspaceSourceFile(path, workspaceRoot, line) }] : [],
      ...targets.map(target => ({ label: target.label, icon: icon(target), onClick: () => openTarget(target, true) })),
    ] }] : [],
    ...githubUrl ? [{ label: zh ? "在 GitHub 中打开" : "Open in GitHub", onClick: () => { void openPreferredWebLink(githubUrl); } }] : [],
    ...primary || newTab || githubUrl ? [{ label: "", separator: true }] : [],
    { label: zh ? "复制所选内容" : "Copy selection", disabled: !menu?.selection,
      onClick: () => { void copyText(menu!.selection, t("文本")); } },
    { label: t("复制路径"), disabled: !canOpen, onClick: () => { void copyText(paths.full!, t("路径")); } },
    ...validPath && paths.relative !== undefined ? [{ label: zh ? "复制相对路径" : "Copy relative path",
      onClick: () => { void copyText(paths.relative!, t("路径")); } }] : [],
    { label: zh ? "切换自动换行" : "Toggle line wrap", onClick: () => setWrap(!wrap) },
  ];

  return <RequestChangesContext.Provider value={requestChanges}>
    <div style={{ display: "contents" }} onContextMenu={event => {
      event.preventDefault(); event.stopPropagation();
      const element = (event.target as Element).closest<HTMLElement>("[data-diff-line-number][data-diff-line-side]");
      const location = element && event.currentTarget.contains(element) ? {
        side: element.dataset.diffLineSide as DiffLineLocation["side"], line: Number(element.dataset.diffLineNumber),
      } : undefined;
      setMenu({ position: { x: event.clientX, y: event.clientY }, location, selection: window.getSelection()?.toString()?.trim() ?? "" });
    }}>
      {children}
      {menu && <ContextMenu position={menu.position} items={items} onClose={() => setMenu(undefined)} />}
    </div>
  </RequestChangesContext.Provider>;
}
