export const SHORTCUT_DEFINITIONS = [
  { id: "commandPalette", label: "命令面板", action: "Command Palette", defaultBinding: "Mod+K" },
  { id: "settings", label: "设置", action: "Settings", defaultBinding: "Mod+Comma" },
  { id: "shortcutHelp", label: "快捷键", action: "Keyboard Shortcuts", defaultBinding: "Mod+Slash" },
  { id: "promptHistory", label: "提示词历史", action: "Prompt history", defaultBinding: "Mod+R" },
  { id: "newConversation", label: "新建任务", action: "New conversation", defaultBinding: "Mod+N" },
  { id: "clearComposer", label: "清空输入框", action: "Clear composer", defaultBinding: "Mod+L" },
  { id: "processDetail", label: "切换过程详情", action: "Cycle process detail", defaultBinding: "Mod+O" },
  { id: "globalSearch", label: "全局搜索", action: "Global search", defaultBinding: "Mod+P" },
  { id: "toggleDiff", label: "切换差异面板", action: "Toggle diff panel", defaultBinding: "Mod+Shift+D" },
  { id: "openPreview", label: "打开预览", action: "Open preview", defaultBinding: "Mod+Shift+P" },
  { id: "permissionMenu", label: "权限菜单", action: "Permission menu", defaultBinding: "Mod+Shift+M" },
  { id: "modelMenu", label: "模型菜单", action: "Model menu", defaultBinding: "Mod+Shift+I" },
  { id: "openGeneralSettings", label: "打开常规设置", action: "Open general settings", defaultBinding: "Mod+Shift+E" },
  { id: "zoomIn", label: "增大界面字号", action: "Increase text scale", defaultBinding: "Mod+Equal" },
  { id: "zoomOut", label: "减小界面字号", action: "Decrease text scale", defaultBinding: "Mod+Minus" },
  { id: "zoomReset", label: "重置界面字号", action: "Reset text scale", defaultBinding: "Mod+Digit0" },
  { id: "terminal", label: "打开终端", action: "Open terminal stack", defaultBinding: "Mod+J" },
  { id: "closePanel", label: "关闭当前面板", action: "Close focused panel", defaultBinding: "Mod+Backslash" },
  { id: "leftSidebar", label: "切换左侧栏", action: "Toggle left sidebar", defaultBinding: "Mod+B" },
  { id: "sideChat", label: "切换侧聊", action: "Toggle side chat", defaultBinding: "Mod+Semicolon" },
  { id: "saveFile", label: "保存文件", action: "Save editor file", defaultBinding: "Mod+S" },
  { id: "closeEditor", label: "关闭编辑器标签", action: "Close editor tab", defaultBinding: "Mod+W" },
  { id: "nextConversation", label: "切换下一个任务", action: "Next conversation", defaultBinding: "Mod+Tab" },
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
