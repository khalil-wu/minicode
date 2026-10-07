export const SHORTCUT_DEFINITIONS = [
  { id: "commandPalette", label: "命令面板", description: "查找并运行应用命令。", action: "Command Palette", defaultBinding: "Mod+K" },
  { id: "settings", label: "设置", description: "打开或关闭设置页面。", action: "Settings", defaultBinding: "Mod+Comma" },
  { id: "shortcutHelp", label: "快捷键", description: "查看当前快捷键与消息发送方式。", action: "Keyboard Shortcuts", defaultBinding: "Mod+Slash" },
  { id: "promptHistory", label: "提示词历史", description: "在输入框中查找历史提示词。", action: "Prompt history", defaultBinding: "Mod+R" },
  { id: "newConversation", label: "新建任务", description: "按当前模式新建任务，绑定所选项目。", action: "New conversation", defaultBinding: "Mod+N" },
  { id: "clearComposer", label: "清空输入框", description: "清空当前草稿并聚焦输入框。", action: "Clear composer", defaultBinding: "Mod+L" },
  { id: "processDetail", label: "切换过程详情", description: "依次切换普通、详细与摘要显示。", action: "Cycle process detail", defaultBinding: "Mod+O" },
  { id: "globalSearch", label: "全局搜索", description: "在当前工作区快速查找并打开文件。", action: "Global search", defaultBinding: "Mod+P" },
  { id: "workspaceSearch", label: "搜索项目内容", description: "搜索当前工作区中的文本内容。", action: "Search project text", defaultBinding: "Mod+Shift+F" },
  { id: "toggleDiff", label: "切换差异面板", description: "打开或收起代码变更审阅。", action: "Toggle diff panel", defaultBinding: "Mod+Shift+D" },
  { id: "openPreview", label: "打开预览", description: "切换到代码工作区并显示预览。", action: "Open preview", defaultBinding: "Mod+Shift+P" },
  { id: "permissionMenu", label: "权限菜单", description: "在当前对话中打开权限选项。", action: "Permission menu", defaultBinding: "Mod+Shift+M" },
  { id: "modelMenu", label: "模型菜单", description: "在当前对话中打开模型选项。", action: "Model menu", defaultBinding: "Mod+Shift+I" },
  { id: "openGeneralSettings", label: "打开常规设置", description: "直接进入设置的常规页面。", action: "Open general settings", defaultBinding: "Mod+Shift+E" },
  { id: "terminal", label: "打开终端", description: "打开或收起底部终端面板。", action: "Open terminal stack", defaultBinding: "Mod+J" },
  { id: "closePanel", label: "关闭当前面板", description: "关闭当前聚焦的面板，保留最后一个面板。", action: "Close focused panel", defaultBinding: "Mod+Backslash" },
  { id: "leftSidebar", label: "切换左侧栏", description: "收起侧栏或恢复上次展开的宽度。", action: "Toggle left sidebar", defaultBinding: "Mod+B" },
  { id: "sideChat", label: "切换侧聊", description: "打开或收起右侧聊天。", action: "Toggle side chat", defaultBinding: "Mod+Semicolon" },
  { id: "saveFile", label: "保存文件", description: "保存当前编辑器文件。", action: "Save editor file", defaultBinding: "Mod+S" },
  { id: "saveAllFiles", label: "保存全部文件", description: "保存所有已修改的编辑器文件。", action: "Save all editor files", defaultBinding: "Mod+Shift+S" },
  { id: "closeEditor", label: "关闭编辑器标签", description: "关闭当前编辑器标签。", action: "Close editor tab", defaultBinding: "Mod+W" },
  { id: "nextConversation", label: "切换下一个任务", description: "切换未归档任务；按住 Shift 反向切换。", action: "Next conversation", defaultBinding: "Mod+Tab" },
] as const;

export type ShortcutActionId = typeof SHORTCUT_DEFINITIONS[number]["id"];
export type ShortcutBindings = Record<ShortcutActionId, string>;

export const DEFAULT_SHORTCUT_BINDINGS = Object.fromEntries(
  SHORTCUT_DEFINITIONS.map((definition) => [definition.id, definition.defaultBinding]),
) as ShortcutBindings;

const MODIFIER_CODES = new Set(["ControlLeft", "ControlRight", "MetaLeft", "MetaRight", "AltLeft", "AltRight", "ShiftLeft", "ShiftRight"]);
const KEY_TOKENS: Record<string, string> = {
  "\\": "Backslash",
  ",": "Comma",
  "=": "Equal",
  "+": "Equal",
  "-": "Minus",
  ";": "Semicolon",
  "/": "Slash",
  " ": "Space",
  "0": "Digit0",
};

const keyTokenFromEvent = (event: Pick<KeyboardEvent, "code" | "key">): string => {
  if (event.code.startsWith("Key")) return event.code.slice(3).toUpperCase();
  if (event.code.startsWith("Digit")) return event.code;
  if (event.code && !MODIFIER_CODES.has(event.code)) return event.code.replace(/^(Numpad)/, "$1");
  if (KEY_TOKENS[event.key]) return KEY_TOKENS[event.key];
  if (event.key.length === 1) return event.key.toUpperCase();
  return event.key;
};

export const shortcutFromEvent = (
  event: Pick<KeyboardEvent, "code" | "key" | "ctrlKey" | "metaKey" | "altKey" | "shiftKey">,
): string | null => {
  if (MODIFIER_CODES.has(event.code) || ["Control", "Meta", "Alt", "Shift"].includes(event.key)) return null;
  const key = keyTokenFromEvent(event);
  if (!key) return null;
  const includeShift = event.shiftKey && !(key === "Equal" && event.key === "+");
  const modifiers = [
    event.ctrlKey || event.metaKey ? "Mod" : "",
    event.altKey ? "Alt" : "",
    includeShift ? "Shift" : "",
  ].filter(Boolean);
  return [...modifiers, key].join("+");
};

export const matchesShortcut = (event: KeyboardEvent, binding: string): boolean => {
  if (!binding) return false;
  return shortcutFromEvent(event) === binding;
};

export const matchesShiftedShortcutVariant = (event: KeyboardEvent, binding: string): boolean => {
  if (!binding || !event.shiftKey || binding.split("+").includes("Shift")) return false;
  return shortcutFromEvent({
    code: event.code,
    key: event.key,
    ctrlKey: event.ctrlKey,
    metaKey: event.metaKey,
    altKey: event.altKey,
    shiftKey: false,
  }) === binding;
};

const KEY_LABELS: Record<string, string> = {
  Backslash: "\\",
  Comma: ",",
  Equal: "+",
  Minus: "-",
  Semicolon: ";",
  Slash: "/",
  Space: "Space",
  Tab: "Tab",
};

export const formatShortcut = (binding: string): string => {
  if (!binding) return "未设置";
  return binding
    .split("+")
    .map((token) => token === "Mod" ? "Ctrl/Cmd" : token.startsWith("Digit") ? token.slice(5) : KEY_LABELS[token] ?? token)
    .join(" + ");
};

export const findShortcutConflict = (
  bindings: ShortcutBindings,
  actionId: ShortcutActionId,
  candidate: string,
): typeof SHORTCUT_DEFINITIONS[number] | null => (
  SHORTCUT_DEFINITIONS.find((definition) => definition.id !== actionId && bindings[definition.id] === candidate) ?? null
);
