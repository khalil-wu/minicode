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
  const [menuPosition, setMenuPosition] = useState<{ x: number; y: number } | null>(null);
  const supports = (action: EditorAction) => supportedActions.includes(action);
  return <div className="mc-editor-actions" role="toolbar" aria-label="编辑器操作">
    {supports("find") && <button type="button" aria-label="查找" title="查找 · Ctrl+F" onClick={() => onAction("find")}><Search size={15} /></button>}
    {supports("gotoLine") && <button type="button" aria-label="跳转到行" title="跳转到行 · Ctrl+G" onClick={() => onAction("gotoLine")}><ListOrdered size={15} /></button>}
    <button type="button" aria-label="自动换行" title="自动换行 · Alt+Z" aria-pressed={wordWrap} onClick={onToggleWordWrap}><WrapText size={15} /></button>
    <button type="button" aria-label="更多编辑器操作" title="更多编辑器操作" aria-haspopup="menu" aria-expanded={Boolean(menuPosition)} onClick={(event) => {
      const rect = event.currentTarget.getBoundingClientRect();
      setMenuPosition(menuPosition ? null : { x: rect.right - 210, y: rect.bottom + 4 });
    }}><MoreHorizontal size={15} /></button>
    {menuPosition && <ContextMenu position={menuPosition} onClose={() => setMenuPosition(null)} items={[
      { label: `保存全部（${dirtyCount}）`, icon: <SaveAll size={15} />, shortcut: "Ctrl+Shift+S", disabled: dirtyCount === 0, onClick: onSaveAll },
      ...(supports("definition") ? [{ label: "转到定义", shortcut: "F12", onClick: () => onAction("definition") }] : []),
      ...(supports("references") ? [{ label: "查找所有引用", shortcut: "Shift+F12", onClick: () => onAction("references") }] : []),
      ...(supports("rename") && !readOnly ? [{ label: "重命名符号", shortcut: "F2", onClick: () => onAction("rename") }] : []),
      ...(supports("replace") && !readOnly ? [{ label: "查找并替换", shortcut: "Ctrl+H", onClick: () => onAction("replace") }] : []),
      ...(supports("foldAll") ? [{ label: "折叠所有区域", icon: <FoldVertical size={15} />, onClick: () => onAction("foldAll") }, { label: "展开所有区域", icon: <UnfoldVertical size={15} />, onClick: () => onAction("unfoldAll") }] : []),
      ...(supports("format") && !readOnly ? [{ label: "格式化文档", shortcut: "Shift+Alt+F", onClick: () => onAction("format") }] : []),
      ...(showMinimap ? [{ label: "显示代码缩略图", icon: minimap ? <Check size={15} /> : undefined, onClick: onToggleMinimap }] : []),
    ]} />}
  </div>;
}
