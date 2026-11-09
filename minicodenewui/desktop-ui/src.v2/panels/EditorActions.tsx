import { useI18n } from "../i18n";
import { useState } from "react";
import { Check, FoldVertical, ListOrdered, MoreHorizontal, SaveAll, Search, UnfoldVertical, WrapText } from "lucide-react";
import { ContextMenu } from "../components/ContextMenu";
import "./EditorActions.css";

export type EditorAction = "find" | "replace" | "gotoLine" | "foldAll" | "unfoldAll" | "format" | "definition" | "references" | "rename";

export function EditorActions({ onAction, supportedActions, wordWrap, onToggleWordWrap, minimap, onToggleMinimap, showMinimap, readOnly, onSaveAll, dirtyCount }: {
  onAction: (action: EditorAction) => void;
  supportedActions: EditorAction[];
  wordWrap: boolean;
  onToggleWordWrap: () => void;
  minimap: boolean;
  onToggleMinimap: () => void;
  showMinimap: boolean;
  readOnly: boolean;
  onSaveAll: () => void;
  dirtyCount: number;
}) {
  const { t: tr } = useI18n();
  const [menuPosition, setMenuPosition] = useState<{ x: number; y: number } | null>(null);
  const supports = (action: EditorAction) => supportedActions.includes(action);
  return <div className="mc-editor-actions" role="toolbar" aria-label={tr("编辑器操作")}>
    {supports("find") && <button type="button" aria-label={tr("查找")} title={tr("查找 · Ctrl+F")} onClick={() => onAction("find")}><Search size={15} /></button>}
    {supports("gotoLine") && <button type="button" aria-label={tr("跳转到行")} title={tr("跳转到行 · Ctrl+G")} onClick={() => onAction("gotoLine")}><ListOrdered size={15} /></button>}
    <button type="button" aria-label={tr("自动换行")} title={tr("自动换行 · Alt+Z")} aria-pressed={wordWrap} onClick={onToggleWordWrap}><WrapText size={15} /></button>
    <button type="button" aria-label={tr("更多编辑器操作")} title={tr("更多编辑器操作")} aria-haspopup="menu" aria-expanded={Boolean(menuPosition)} onClick={(event) => {
      const rect = event.currentTarget.getBoundingClientRect();
      setMenuPosition(menuPosition ? null : { x: rect.right - 210, y: rect.bottom + 4 });
    }}><MoreHorizontal size={15} /></button>
    {menuPosition && <ContextMenu position={menuPosition} onClose={() => setMenuPosition(null)} items={[
      { label: tr("保存全部（{v0}）", { v0: dirtyCount }), icon: <SaveAll size={15} />, shortcut: "Ctrl+Shift+S", disabled: dirtyCount === 0, onClick: onSaveAll },
      ...(supports("definition") ? [{ label: tr("转到定义"), shortcut: "F12", onClick: () => onAction("definition") }] : []),
      ...(supports("references") ? [{ label: tr("查找所有引用"), shortcut: "Shift+F12", onClick: () => onAction("references") }] : []),
      ...(supports("rename") && !readOnly ? [{ label: tr("重命名符号"), shortcut: "F2", onClick: () => onAction("rename") }] : []),
      ...(supports("replace") && !readOnly ? [{ label: tr("查找并替换"), shortcut: "Ctrl+H", onClick: () => onAction("replace") }] : []),
      ...(supports("foldAll") ? [{ label: tr("折叠所有区域"), icon: <FoldVertical size={15} />, onClick: () => onAction("foldAll") }, { label: tr("展开所有区域"), icon: <UnfoldVertical size={15} />, onClick: () => onAction("unfoldAll") }] : []),
      ...(supports("format") && !readOnly ? [{ label: tr("格式化文档"), shortcut: "Shift+Alt+F", onClick: () => onAction("format") }] : []),
      ...(showMinimap ? [{ label: tr("显示代码缩略图"), icon: minimap ? <Check size={15} /> : undefined, onClick: onToggleMinimap }] : []),
    ]} />}
  </div>;
}
