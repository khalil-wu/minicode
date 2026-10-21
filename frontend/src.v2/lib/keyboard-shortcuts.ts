export const SHORTCUT_DEFINITIONS = [
  { id: "undoAppAction", label: "撤销", description: "撤销最近的应用操作", action: "Undo", defaultBinding: "Mod+Z" },
  { id: "redoAppAction", label: "重做", description: "重做最近的应用操作", action: "Redo", defaultBinding: "Mod+Y" },
  { id: "commandPalette", label: "命令面板", description: "查找并运行应用命令。", action: "Command Palette", defaultBinding: "Mod+K" },
  { id: "settings", label: "设置", description: "打开或关闭设置页面。", action: "Settings", defaultBinding: "Mod+Comma" },
  { id: "shortcutHelp", label: "快捷键", description: "查看当前快捷键与消息发送方式。", action: "Keyboard Shortcuts", defaultBinding: "Mod+Slash" },
  { id: "promptHistory", label: "提示词历史", description: "在输入框中查找历史提示词。", action: "Prompt history", defaultBinding: "", codeOnly: true },
  { id: "newConversation", label: "新建任务", description: "按当前模式新建任务，绑定所选项目。", action: "New conversation", defaultBinding: "Mod+N" },
  { id: "clearComposer", label: "清空输入框", description: "清空当前草稿并聚焦输入框。", action: "Clear composer", defaultBinding: "", codeOnly: true },
  { id: "processDetail", label: "切换过程详情", description: "依次切换普通、详细与摘要显示。", action: "Cycle process detail", defaultBinding: "", codeOnly: true },
  { id: "globalSearch", label: "全局搜索", description: "在当前工作区快速查找并打开文件。", action: "Global search", defaultBinding: "Mod+P" },
  { id: "workspaceSearch", label: "搜索项目内容", description: "搜索当前工作区中的文本内容。", action: "Search project text", defaultBinding: "Mod+Shift+F", codeOnly: true },
  { id: "toggleDiff", label: "切换差异面板", description: "打开或收起代码变更审阅。", action: "Toggle diff panel", defaultBinding: "Mod+Shift+D", codeOnly: true },
  { id: "openPreview", label: "打开预览", description: "切换到代码工作区并显示预览。", action: "Open preview", defaultBinding: "", codeOnly: true },
  { id: "permissionMenu", label: "权限菜单", description: "在当前对话中打开权限选项。", action: "Permission menu", defaultBinding: "" },
  { id: "modelMenu", label: "模型菜单", description: "在当前对话中打开模型选项。", action: "Model menu", defaultBinding: "" },
  { id: "openGeneralSettings", label: "打开常规设置", description: "直接进入设置的常规页面。", action: "Open general settings", defaultBinding: "", codeOnly: true },
  { id: "terminal", label: "打开终端", description: "打开终端", action: "Open terminal", defaultBinding: "Mod+Backquote" },
  { id: "closePanel", label: "关闭当前面板", description: "关闭当前聚焦的面板，保留最后一个面板。", action: "Close focused panel", defaultBinding: "", codeOnly: true },
  { id: "leftSidebar", label: "切换左侧栏", description: "收起侧栏或恢复上次展开的宽度。", action: "Toggle left sidebar", defaultBinding: "Mod+Shift+S" },
  { id: "sideChat", label: "切换侧聊", description: "打开或收起右侧聊天。", action: "Toggle side chat", defaultBinding: "", codeOnly: true },
  { id: "saveFile", label: "保存文件", description: "保存当前编辑器文件。", action: "Save editor file", defaultBinding: "Mod+S", codeOnly: true },
  { id: "saveAllFiles", label: "保存全部文件", description: "保存所有已修改的编辑器文件。", action: "Save all editor files", defaultBinding: "Mod+Shift+S", codeOnly: true },
  { id: "closeEditor", label: "关闭编辑器标签", description: "关闭当前编辑器标签。", action: "Close editor tab", defaultBinding: "", codeOnly: true },
  { id: "nextConversation", label: "切换下一个任务", description: "切换下一个任务", action: "Next conversation", defaultBinding: "Mod+Alt+BracketRight" },
  { id: "newWindow", label: "新建窗口", description: "新建窗口", action: "New Window", defaultBinding: "" },
  { id: "newProject", label: "创建项目", description: "创建项目", action: "New Project", defaultBinding: "" },
  { id: "openFolder", label: "打开文件夹", description: "打开文件夹", action: "Open Folder", defaultBinding: "Mod+O" },
  { id: "closeTab", label: "关闭标签页", description: "关闭标签页", action: "Close Tab", defaultBinding: "Mod+W" },
  { id: "quit", label: "退出", description: "退出", action: "Quit", defaultBinding: "Mod+Q" },
  { id: "bottomPanel", label: "切换底部面板", description: "切换底部面板", action: "Toggle Bottom Panel", defaultBinding: "Mod+J", codeOnly: true },
  { id: "pinnedSummary", label: "切换摘要", description: "切换摘要", action: "Toggle Pinned Summary", defaultBinding: "" },
  { id: "fileTree", label: "切换文件树", description: "切换文件树", action: "Toggle File Tree", defaultBinding: "Mod+Shift+E" },
  { id: "sidePanel", label: "切换右侧栏", description: "切换右侧栏", action: "Toggle Changes Panel", defaultBinding: "Mod+Alt+B" },
  { id: "browserTab", label: "打开浏览器标签页", description: "打开浏览器标签页", action: "Open Browser Tab", defaultBinding: "Mod+T" },
  { id: "browserAddress", label: "聚焦浏览器地址栏", description: "聚焦浏览器地址栏", action: "Focus Browser Address Bar", defaultBinding: "Mod+L" },
  { id: "browserReload", label: "重新加载浏览器页面", description: "重新加载浏览器页面", action: "Reload Browser Page", defaultBinding: "Mod+R" },
  { id: "browserHardReload", label: "强制重新加载浏览器页面", description: "强制重新加载浏览器页面", action: "Force Reload Browser Page", defaultBinding: "Mod+Shift+R" },
  { id: "browserBack", label: "浏览器后退", description: "浏览器后退", action: "Browser Back", defaultBinding: "Alt+ArrowLeft" },
  { id: "browserForward", label: "浏览器前进", description: "浏览器前进", action: "Browser Forward", defaultBinding: "Alt+ArrowRight" },
  { id: "find", label: "查找", description: "查找", action: "Find", defaultBinding: "Mod+F" },
  { id: "findNext", label: "下一个匹配项", description: "下一个匹配项", action: "Find Next", defaultBinding: "Mod+G" },
  { id: "findPrevious", label: "上一个匹配项", description: "上一个匹配项", action: "Find Previous", defaultBinding: "Shift+F3" },
  { id: "navigateBack", label: "返回", description: "返回", action: "Back", defaultBinding: "Mod+BracketLeft" },
  { id: "navigateForward", label: "前进", description: "前进", action: "Forward", defaultBinding: "" },
  { id: "previousConversation", label: "切换上一个任务", description: "切换上一个任务", action: "Previous conversation", defaultBinding: "Mod+Alt+BracketLeft" },
  { id: "previousTab", label: "上一个标签页", description: "上一个标签页", action: "Previous Tab", defaultBinding: "Mod+Shift+BracketLeft" },
  { id: "nextTab", label: "下一个标签页", description: "下一个标签页", action: "Next Tab", defaultBinding: "Mod+Shift+BracketRight" },
  { id: "zoomIn", label: "放大", description: "放大", action: "Zoom In", defaultBinding: "Mod+Shift+Equal" },
  { id: "zoomOut", label: "缩小", description: "缩小", action: "Zoom Out", defaultBinding: "Mod+Minus" },
  { id: "zoomReset", label: "实际大小", description: "实际大小", action: "Actual Size", defaultBinding: "Mod+Digit0" },
  { id: "fullScreen", label: "切换全屏", description: "切换全屏", action: "Toggle Full Screen", defaultBinding: "F11" },
] as const;

export type ShortcutActionId = typeof SHORTCUT_DEFINITIONS[number]["id"];
export type ShortcutBindings = Record<ShortcutActionId, string[]>;

const SECONDARY_SHORTCUTS: Partial<Record<ShortcutActionId, string[]>> = {
  newConversation: ["Mod+Shift+O"], leftSidebar: ["Mod+B"], closeTab: ["Mod+F4"],
  previousConversation: ["Mod+Alt+ArrowLeft"], nextConversation: ["Mod+Alt+ArrowRight"],
  previousTab: ["Mod+PageUp"], nextTab: ["Mod+PageDown"],
  zoomIn: ["Mod+Equal"],
};

export const DEFAULT_SHORTCUT_BINDINGS = Object.fromEntries(
  SHORTCUT_DEFINITIONS.map((definition) => [definition.id,
    definition.id === "redoAppAction"
      ? typeof navigator !== "undefined" && navigator.platform.startsWith("Mac") ? ["Mod+Shift+Z"] : ["Mod+Y", "Mod+Shift+Z"]
      : [...(definition.defaultBinding ? [definition.defaultBinding] : []), ...(SECONDARY_SHORTCUTS[definition.id] || [])]]),
) as ShortcutBindings;

export const isCodeShortcut = (action: ShortcutActionId): boolean => {
  const definition = SHORTCUT_DEFINITIONS.find(definition => definition.id === action)!;
  return "codeOnly" in definition && definition.codeOnly;
};

const ELECTRON_KEYS: Record<string, string> = {
  Backquote: "`", Backslash: "\\", BracketLeft: "[", BracketRight: "]", Comma: ",", Equal: "=", Minus: "-",
  Semicolon: ";", Slash: "/", ArrowLeft: "Left", ArrowRight: "Right", ArrowUp: "Up", ArrowDown: "Down",
  NumpadAdd: "numadd", NumpadSubtract: "numsub", NumpadMultiply: "nummult", NumpadDivide: "numdiv", NumpadDecimal: "numdec", NumpadEnter: "Enter",
};

export const toElectronAccelerator = (binding: string): string | null => binding ? binding.split("+").map(token =>
  token === "Mod" ? "CommandOrControl" : ELECTRON_KEYS[token]
    ?? (/^Digit\d$/.test(token) ? token.slice(5) : /^Numpad\d$/.test(token) ? `num${token.slice(6)}` : token),
).join("+") : null;

export const nativeMenuAccelerators = (bindings: ShortcutBindings): Record<ShortcutActionId, string | null> =>
  Object.fromEntries(SHORTCUT_DEFINITIONS.map(definition => [definition.id, toElectronAccelerator(bindings[definition.id][0] || "")])) as Record<ShortcutActionId, string | null>;

export const nativeMenuBindings = (bindings: ShortcutBindings): Record<ShortcutActionId, string[]> =>
  Object.fromEntries(SHORTCUT_DEFINITIONS.map(definition => [definition.id, bindings[definition.id].map(binding => toElectronAccelerator(binding)!)])) as Record<ShortcutActionId, string[]>;

export const nativeShortcutOverrides = (bindings: ShortcutBindings): ShortcutActionId[] =>
  SHORTCUT_DEFINITIONS.filter(definition => JSON.stringify(bindings[definition.id]) !== JSON.stringify(DEFAULT_SHORTCUT_BINDINGS[definition.id])).map(definition => definition.id);

export const NATIVE_MENU_SHORTCUT_ACTIONS: readonly ShortcutActionId[] = [
  "undoAppAction", "redoAppAction",
  "commandPalette", "settings", "shortcutHelp", "newConversation", "globalSearch", "terminal", "leftSidebar",
  "newWindow", "newProject", "openFolder", "closeTab", "quit", "bottomPanel", "pinnedSummary", "fileTree", "sidePanel",
  "browserTab", "browserAddress", "browserReload", "browserHardReload", "browserBack", "browserForward", "find", "findNext", "findPrevious",
  "navigateBack", "navigateForward", "previousConversation", "nextConversation", "previousTab", "nextTab", "zoomIn", "zoomOut", "zoomReset", "fullScreen",
];

export const nativeShortcutAction = (event: KeyboardEvent, bindings: ShortcutBindings, codeMode: boolean): ShortcutActionId | undefined =>
  NATIVE_MENU_SHORTCUT_ACTIONS.find(action => (codeMode || !isCodeShortcut(action)) && matchesShortcut(event, bindings[action]));

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

export const matchesShortcut = (event: KeyboardEvent, binding: string | readonly string[]): boolean => {
  if (typeof binding !== "string") return binding.some(chord => matchesShortcut(event, chord));
  if (!binding) return false;
  return shortcutFromEvent(event) === binding;
};

export const matchesShiftedShortcutVariant = (event: KeyboardEvent, binding: string | readonly string[]): boolean => {
  if (typeof binding !== "string") return binding.some(chord => matchesShiftedShortcutVariant(event, chord));
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
  Backquote: "`",
  BracketLeft: "[",
  BracketRight: "]",
  ArrowLeft: "←",
  ArrowRight: "→",
  ArrowUp: "↑",
  ArrowDown: "↓",
};

export const formatShortcut = (binding: string | readonly string[]): string => {
  if (typeof binding !== "string") return binding.map(formatShortcut).join(" / ");
  if (!binding) return "";
  return binding
    .split("+")
    .map((token) => token === "Mod" ? (navigator.platform.startsWith("Mac") ? "⌘" : "Ctrl") : token.startsWith("Digit") ? token.slice(5) : KEY_LABELS[token] ?? token)
    .join(" + ");
};

export const findShortcutConflict = (
  bindings: ShortcutBindings,
  actionId: ShortcutActionId,
  candidate: string,
): typeof SHORTCUT_DEFINITIONS[number] | null => (
  SHORTCUT_DEFINITIONS.find((definition) => definition.id !== actionId && bindings[definition.id].includes(candidate)
    && !([actionId, definition.id].includes("leftSidebar") && [actionId, definition.id].includes("saveAllFiles"))) ?? null
);
