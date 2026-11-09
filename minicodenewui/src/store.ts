import { create } from "zustand";
import { initialChats, projects as initialProjects, plugins as pluginCatalog, automations as initialAutomations, mcpServers } from "./mock";
import type {
  Accent,
  ApprovalDecision,
  AssistantTurn,
  Attachment,
  Chat,
  Effort,
  Message,
  PanelTab,
  PermissionMode,
  Project,
  Route,
  SettingsSection,
  ThemePref,
  Toast,
  UserMessage,
} from "./types";
import { uid } from "./lib/util";
import { cancelTurn, resolvePending, startTurn } from "./sim";

export interface Settings {
  theme: ThemePref;
  accent: Accent;
  density: "comfortable" | "compact";
  chatFont: "sans" | "serif";
  fontSize: number;
  reduceMotion: boolean;
  sendKey: "enter" | "mod-enter";
  notifyOnDone: boolean;
  autoExpandSteps: boolean;
  defaultPermission: PermissionMode;
  customInstructions: string;
  memory: boolean;
  commitPrefix: string;
  autoBranch: boolean;
  language: "zh-CN" | "en";
  apiKeys: Record<string, string>;
}

export interface Draft {
  text: string;
  attachments: Attachment[];
}

export interface ConfirmRequest {
  title: string;
  body: string;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
}

export interface Automation {
  id: string;
  name: string;
  schedule: string;
  project: string;
  enabled: boolean;
  last: string;
}

export interface McpServer {
  id: string;
  name: string;
  transport: string;
  tools: number;
  status: "connected" | "error" | "disabled";
  enabled: boolean;
}

const SETTINGS_KEY = "minicode-newui:settings";

const defaultSettings: Settings = {
  theme: "light",
  accent: "clay",
  density: "comfortable",
  chatFont: "sans",
  fontSize: 15,
  reduceMotion: false,
  sendKey: "enter",
  notifyOnDone: true,
  autoExpandSteps: false,
  defaultPermission: "ask",
  customInstructions: "",
  memory: true,
  commitPrefix: "",
  autoBranch: false,
  language: "zh-CN",
  apiKeys: { anthropic: "sk-ant-api03-••••••••••••••••7Qx", deepseek: "" },
};

function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (raw) return { ...defaultSettings, ...JSON.parse(raw) };
  } catch {
    /* 演示环境，读不到就用默认值 */
  }
  return defaultSettings;
}

const params = new URLSearchParams(typeof location !== "undefined" ? location.search : "");

export interface AppState {
  projects: Project[];
  chats: Chat[];
  route: Route;
  homeProjectId: string | null;
  sidebarOpen: boolean;
  panelOpen: boolean;
  panelTab: PanelTab;
  panelWidth: number;
  /** 变更页里选中的文件 */
  diffPath: string | null;
  /** 文件页里打开的文件 */
  viewerPath: string | null;
  model: string;
  effort: Effort;
  permission: PermissionMode;
  planMode: boolean;
  drafts: Record<string, Draft>;
  settings: Settings;
  installedPlugins: Record<string, boolean>;
  automations: Automation[];
  mcp: McpServer[];
  paletteOpen: boolean;
  shortcutsOpen: boolean;
  confirm: ConfirmRequest | null;
  toasts: Toast[];
  renaming: { id: string; where: "sidebar" | "title" } | null;
  /** 演示速度倍率，冒烟测试用 ?speed=20 加速 */
  speed: number;
  alwaysAllow: Record<string, string[]>;

  navigate: (route: Route) => void;
  openChat: (id: string) => void;
  newChat: (projectId?: string | null) => void;
  setHomeProject: (id: string | null) => void;
  toggleSidebar: () => void;
  togglePanel: (open?: boolean) => void;
  openPanel: (tab: PanelTab, path?: string | null) => void;
  setPanelWidth: (w: number) => void;
  setDiffPath: (p: string | null) => void;
  setViewerPath: (p: string | null) => void;
  setModel: (id: string) => void;
  setEffort: (e: Effort) => void;
  setPermission: (p: PermissionMode) => void;
  togglePlanMode: (on?: boolean) => void;
  setDraft: (key: string, patch: Partial<Draft>) => void;
  send: (key: string, text: string) => void;
  stop: (chatId: string) => void;
  removeQueued: (chatId: string, index: number) => void;
  sendQueuedNow: (chatId: string, index: number) => void;
  decideApproval: (chatId: string, turnId: string, decision: ApprovalDecision) => void;
  answerQuestion: (chatId: string, turnId: string, answer: string) => void;
  acceptPlan: (chatId: string, turnId: string) => void;
  retry: (chatId: string, turnId: string) => void;
  editAndResend: (chatId: string, messageId: string, text: string) => void;
  revertFile: (chatId: string, turnId: string, path: string) => void;
  revertTurn: (chatId: string, turnId: string) => void;
  renameChat: (id: string, title: string) => void;
  setRenaming: (id: string | null, where?: "sidebar" | "title") => void;
  togglePin: (id: string) => void;
  toggleUnread: (id: string) => void;
  archiveChat: (id: string) => void;
  restoreChat: (id: string) => void;
  deleteChat: (id: string) => void;
  toggleProject: (id: string) => void;
  addProject: (name: string) => void;
  updateSettings: (patch: Partial<Settings>) => void;
  resetSettings: () => void;
  togglePlugin: (id: string) => void;
  toggleAutomation: (id: string) => void;
  addAutomation: (a: Omit<Automation, "id" | "last">) => void;
  toggleMcp: (id: string) => void;
  addMcp: (name: string, transport: string) => void;
  setPalette: (open: boolean) => void;
  setShortcuts: (open: boolean) => void;
  askConfirm: (req: ConfirmRequest | null) => void;
  toast: (t: Omit<Toast, "id">) => void;
  dismissToast: (id: string) => void;

  /* 模拟器内部使用 */
  patchTurn: (chatId: string, turnId: string, patch: (t: AssistantTurn) => AssistantTurn) => void;
  finishTurn: (chatId: string, turnId: string) => void;
}

function mapChat(chats: Chat[], id: string, fn: (c: Chat) => Chat): Chat[] {
  return chats.map((c) => (c.id === id ? fn(c) : c));
}

function titleFrom(text: string): string {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > 22 ? line.slice(0, 22) + "…" : line || "新聊天";
}

const settings = loadSettings();

export const useApp = create<AppState>()((set, get) => {
  /** 往会话里追加一条用户消息并开始新一轮回复 */
  const beginTurn = (chatId: string, text: string, attachments: Attachment[] = []) => {
    const { model } = get();
    const user: UserMessage = { id: uid("u"), role: "user", text, attachments, at: Date.now() };
    const turn: AssistantTurn = {
      id: uid("a"),
      role: "assistant",
      status: "running",
      phase: "thinking",
      startedAt: Date.now(),
      seconds: 0,
      steps: [],
      text: "",
      model: modelName(model),
    };
    set((s) => ({
      chats: mapChat(s.chats, chatId, (c) => ({ ...c, messages: [...c.messages, user, turn], updatedAt: Date.now() })),
    }));
    startTurn(chatId, turn.id, text);
  };

  const isRunning = (chatId: string) => {
    const chat = get().chats.find((c) => c.id === chatId);
    const last = chat?.messages[chat.messages.length - 1];
    return last?.role === "assistant" && last.status === "running";
  };

  return {
    projects: initialProjects,
    chats: initialChats,
    route: { name: "home" },
    homeProjectId: initialProjects[0].id,
    sidebarOpen: true,
    panelOpen: false,
    panelTab: "changes",
    panelWidth: 460,
    diffPath: null,
    viewerPath: null,
    model: "opus",
    effort: "high",
    permission: settings.defaultPermission,
    planMode: false,
    drafts: {},
    settings,
    installedPlugins: Object.fromEntries(pluginCatalog.map((p) => [p.id, p.installed])),
    automations: initialAutomations,
    mcp: mcpServers,
    paletteOpen: false,
    shortcutsOpen: false,
    confirm: null,
    toasts: [],
    renaming: null,
    speed: Math.max(0.25, Number(params.get("speed")) || 1),
    alwaysAllow: {},

    navigate: (route) => set({ route }),
    openChat: (id) =>
      set((s) => ({
        route: { name: "chat", id },
        chats: mapChat(s.chats, id, (c) => ({ ...c, unread: false })),
        diffPath: null,
      })),
    newChat: (projectId) =>
      set((s) => ({
        route: { name: "home" },
        homeProjectId: projectId === undefined ? s.homeProjectId : projectId,
        panelOpen: false,
      })),
    setHomeProject: (id) => set({ homeProjectId: id }),
    toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
    togglePanel: (open) => set((s) => ({ panelOpen: open ?? !s.panelOpen })),
    openPanel: (tab, path) =>
      set((s) => ({
        panelOpen: true,
        panelTab: tab,
        diffPath: tab === "changes" && path !== undefined ? path : s.diffPath,
        viewerPath: tab === "files" && path !== undefined ? path : s.viewerPath,
      })),
    setPanelWidth: (w) => set({ panelWidth: Math.round(Math.min(760, Math.max(340, w))) }),
    setDiffPath: (p) => set({ diffPath: p }),
    setViewerPath: (p) => set({ viewerPath: p }),
    setModel: (id) => set({ model: id }),
    setEffort: (e) => set({ effort: e }),
    setPermission: (p) => set({ permission: p }),
    togglePlanMode: (on) => set((s) => ({ planMode: on ?? !s.planMode })),
    setDraft: (key, patch) =>
      set((s) => {
        const prev = s.drafts[key] ?? { text: "", attachments: [] };
        return { drafts: { ...s.drafts, [key]: { ...prev, ...patch } } };
      }),

    send: (key, raw) => {
      const text = raw.trim();
      const draft = get().drafts[key] ?? { text: "", attachments: [] };
      if (!text && draft.attachments.length === 0) return;
      const attachments = draft.attachments;
      set((s) => ({ drafts: { ...s.drafts, [key]: { text: "", attachments: [] } } }));

      if (key === "home") {
        const chat: Chat = {
          id: uid("c"),
          title: titleFrom(text || attachments[0]?.name || "新聊天"),
          projectId: get().homeProjectId,
          updatedAt: Date.now(),
          messages: [],
          queue: [],
        };
        set((s) => ({ chats: [chat, ...s.chats], route: { name: "chat", id: chat.id } }));
        beginTurn(chat.id, text, attachments);
        return;
      }

      if (isRunning(key)) {
        set((s) => ({ chats: mapChat(s.chats, key, (c) => ({ ...c, queue: [...c.queue, text] })) }));
        return;
      }
      beginTurn(key, text, attachments);
    },

    stop: (chatId) => {
      const chat = get().chats.find((c) => c.id === chatId);
      const last = chat?.messages[chat.messages.length - 1];
      if (!last || last.role !== "assistant" || last.status !== "running") return;
      cancelTurn(chatId);
      set((s) => ({
        chats: mapChat(s.chats, chatId, (c) => ({
          ...c,
          messages: c.messages.map((m) =>
            m.id === last.id && m.role === "assistant"
              ? {
                  ...m,
                  status: "stopped",
                  phase: "idle",
                  seconds: (Date.now() - m.startedAt) / 1000,
                  steps: m.steps.map((st) => (st.status === "running" ? { ...st, status: "failed" } : st)),
                  approval: m.approval && !m.approval.decision ? { ...m.approval, decision: "deny" } : m.approval,
                }
              : m,
          ),
        })),
      }));
    },

    removeQueued: (chatId, index) =>
      set((s) => ({ chats: mapChat(s.chats, chatId, (c) => ({ ...c, queue: c.queue.filter((_, i) => i !== index) })) })),

    sendQueuedNow: (chatId, index) => {
      const chat = get().chats.find((c) => c.id === chatId);
      const text = chat?.queue[index];
      if (!text) return;
      get().removeQueued(chatId, index);
      get().stop(chatId);
      beginTurn(chatId, text);
    },

    decideApproval: (chatId, turnId, decision) => {
      get().patchTurn(chatId, turnId, (t) => (t.approval ? { ...t, approval: { ...t.approval, decision } } : t));
      if (decision === "always") {
        const cmd = get()
          .chats.find((c) => c.id === chatId)
          ?.messages.find((m) => m.id === turnId);
        const command = cmd?.role === "assistant" ? cmd.approval?.command : undefined;
        if (command) {
          set((s) => ({ alwaysAllow: { ...s.alwaysAllow, [chatId]: [...(s.alwaysAllow[chatId] ?? []), command] } }));
        }
      }
      resolvePending(turnId, decision);
    },

    answerQuestion: (chatId, turnId, answer) => {
      get().patchTurn(chatId, turnId, (t) => (t.question ? { ...t, question: { ...t.question, answer } } : t));
      resolvePending(turnId, answer);
    },

    acceptPlan: (chatId, turnId) => {
      get().patchTurn(chatId, turnId, (t) => (t.plan ? { ...t, plan: { ...t.plan, state: "accepted" } } : t));
      set({ planMode: false });
      beginTurn(chatId, "按计划实施");
    },

    retry: (chatId, turnId) => {
      const chat = get().chats.find((c) => c.id === chatId);
      if (!chat || isRunning(chatId)) return;
      const index = chat.messages.findIndex((m) => m.id === turnId);
      const prompt = chat.messages
        .slice(0, index)
        .reverse()
        .find((m): m is UserMessage => m.role === "user");
      if (!prompt) return;
      const promptIndex = chat.messages.indexOf(prompt);
      set((s) => ({ chats: mapChat(s.chats, chatId, (c) => ({ ...c, messages: c.messages.slice(0, promptIndex) })) }));
      beginTurn(chatId, prompt.text, prompt.attachments);
    },

    editAndResend: (chatId, messageId, text) => {
      const chat = get().chats.find((c) => c.id === chatId);
      if (!chat || !text.trim()) return;
      if (isRunning(chatId)) get().stop(chatId);
      const index = chat.messages.findIndex((m) => m.id === messageId);
      if (index < 0) return;
      const original = chat.messages[index] as UserMessage;
      set((s) => ({ chats: mapChat(s.chats, chatId, (c) => ({ ...c, messages: c.messages.slice(0, index) })) }));
      beginTurn(chatId, text.trim(), original.attachments);
    },

    revertFile: (chatId, turnId, path) => {
      const flip = (reverted: boolean) =>
        get().patchTurn(chatId, turnId, (t) => ({
          ...t,
          files: t.files?.map((f) => (f.path === path ? { ...f, reverted } : f)),
        }));
      flip(true);
      get().toast({ text: `已撤销 ${path.split("/").pop()}`, action: { label: "恢复", run: () => flip(false) } });
    },

    revertTurn: (chatId, turnId) => {
      const turn = get()
        .chats.find((c) => c.id === chatId)
        ?.messages.find((m) => m.id === turnId);
      if (turn?.role !== "assistant" || !turn.files) return;
      const before = turn.files;
      get().patchTurn(chatId, turnId, (t) => ({ ...t, files: t.files?.map((f) => ({ ...f, reverted: true })) }));
      get().toast({
        text: `已撤销 ${before.length} 个文件的改动`,
        action: { label: "恢复", run: () => get().patchTurn(chatId, turnId, (t) => ({ ...t, files: before })) },
      });
    },

    renameChat: (id, title) =>
      set((s) => ({
        chats: mapChat(s.chats, id, (c) => ({ ...c, title: title.trim() || c.title })),
        renaming: null,
      })),
    setRenaming: (id, where = "sidebar") => set({ renaming: id ? { id, where } : null }),
    togglePin: (id) => set((s) => ({ chats: mapChat(s.chats, id, (c) => ({ ...c, pinned: !c.pinned })) })),
    toggleUnread: (id) => set((s) => ({ chats: mapChat(s.chats, id, (c) => ({ ...c, unread: !c.unread })) })),
    archiveChat: (id) => {
      const chat = get().chats.find((c) => c.id === id);
      if (!chat) return;
      if (isRunning(id)) get().stop(id);
      set((s) => ({
        chats: mapChat(s.chats, id, (c) => ({ ...c, archived: true, pinned: false })),
        route: s.route.name === "chat" && s.route.id === id ? { name: "home" } : s.route,
      }));
      get().toast({ text: `已归档「${chat.title}」`, action: { label: "撤销", run: () => get().restoreChat(id) } });
    },
    restoreChat: (id) => set((s) => ({ chats: mapChat(s.chats, id, (c) => ({ ...c, archived: false })) })),
    deleteChat: (id) => {
      const chat = get().chats.find((c) => c.id === id);
      if (!chat) return;
      if (isRunning(id)) get().stop(id);
      const index = get().chats.indexOf(chat);
      set((s) => ({
        chats: s.chats.filter((c) => c.id !== id),
        route: s.route.name === "chat" && s.route.id === id ? { name: "home" } : s.route,
      }));
      get().toast({
        text: `已删除「${chat.title}」`,
        tone: "danger",
        action: {
          label: "撤销",
          run: () =>
            set((s) => {
              const chats = [...s.chats];
              chats.splice(index, 0, chat);
              return { chats };
            }),
        },
      });
    },
    toggleProject: (id) =>
      set((s) => ({ projects: s.projects.map((p) => (p.id === id ? { ...p, expanded: !p.expanded } : p)) })),
    addProject: (name) => {
      const project: Project = { id: uid("p"), name, path: `C:\\Projects\\${name}`, branch: "main", expanded: true };
      set((s) => ({ projects: [...s.projects, project], homeProjectId: project.id, route: { name: "home" } }));
      get().toast({ text: `已添加项目「${name}」`, tone: "success" });
    },

    updateSettings: (patch) =>
      set((s) => {
        const next = { ...s.settings, ...patch };
        try {
          localStorage.setItem(SETTINGS_KEY, JSON.stringify(next));
        } catch {
          /* 忽略隐私模式下的写入失败 */
        }
        return { settings: next };
      }),
    resetSettings: () => {
      localStorage.removeItem(SETTINGS_KEY);
      set({ settings: defaultSettings });
      get().toast({ text: "已恢复默认设置" });
    },
    togglePlugin: (id) => {
      const on = !get().installedPlugins[id];
      set((s) => ({ installedPlugins: { ...s.installedPlugins, [id]: on } }));
      const name = pluginCatalog.find((p) => p.id === id)?.name ?? id;
      get().toast({ text: on ? `已安装 ${name}` : `已卸载 ${name}`, tone: on ? "success" : "default" });
    },
    toggleAutomation: (id) =>
      set((s) => ({ automations: s.automations.map((a) => (a.id === id ? { ...a, enabled: !a.enabled } : a)) })),
    addAutomation: (a) => {
      set((s) => ({ automations: [...s.automations, { ...a, id: uid("auto"), last: "从未运行" }] }));
      get().toast({ text: `已创建自动化「${a.name}」`, tone: "success" });
    },
    toggleMcp: (id) =>
      set((s) => ({
        mcp: s.mcp.map((m) =>
          m.id === id ? { ...m, enabled: !m.enabled, status: m.enabled ? "disabled" : "connected" } : m,
        ),
      })),
    addMcp: (name, transport) => {
      set((s) => ({
        mcp: [...s.mcp, { id: uid("mcp"), name, transport, tools: 0, status: "connected", enabled: true }],
      }));
      get().toast({ text: `已连接 MCP 服务器 ${name}`, tone: "success" });
    },
    setPalette: (open) => set({ paletteOpen: open }),
    setShortcuts: (open) => set({ shortcutsOpen: open }),
    askConfirm: (req) => set({ confirm: req }),
    toast: (t) => {
      const id = uid("toast");
      set((s) => ({ toasts: [...s.toasts.slice(-2), { ...t, id }] }));
      setTimeout(() => get().dismissToast(id), 4800);
    },
    dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),

    patchTurn: (chatId, turnId, patch) =>
      set((s) => ({
        chats: mapChat(s.chats, chatId, (c) => ({
          ...c,
          messages: c.messages.map((m) => (m.id === turnId && m.role === "assistant" ? patch(m) : m)),
        })),
      })),

    finishTurn: (chatId, turnId) => {
      const s = get();
      const viewing = s.route.name === "chat" && s.route.id === chatId;
      s.patchTurn(chatId, turnId, (t) => ({
        ...t,
        status: "done",
        phase: "idle",
        seconds: (Date.now() - t.startedAt) / 1000,
      }));
      const chat = get().chats.find((c) => c.id === chatId);
      set((st) => ({
        chats: mapChat(st.chats, chatId, (c) => ({ ...c, unread: viewing ? c.unread : true, updatedAt: Date.now() })),
      }));
      if (!viewing && s.settings.notifyOnDone && chat) {
        s.toast({ text: `「${chat.title}」已完成`, tone: "success", action: { label: "查看", run: () => get().openChat(chatId) } });
      }
      if (chat && chat.queue.length > 0) {
        const [next, ...rest] = chat.queue;
        set((st) => ({ chats: mapChat(st.chats, chatId, (c) => ({ ...c, queue: rest })) }));
        setTimeout(() => beginTurn(chatId, next), 240 / get().speed);
      }
    },
  };
});

export function modelName(id: string): string {
  return (
    {
      opus: "Claude Opus 5",
      sonnet: "Claude Sonnet 5",
      haiku: "Claude Haiku 4.5",
      deepseek: "DeepSeek V4",
    } as Record<string, string>
  )[id] ?? id;
}

export function selectChat(s: AppState): Chat | undefined {
  return s.route.name === "chat" ? s.chats.find((c) => c.id === (s.route as { id: string }).id) : undefined;
}

export function isTurnRunning(m: Message | undefined): boolean {
  return !!m && m.role === "assistant" && m.status === "running";
}

export function setRouteSettings(section: SettingsSection) {
  useApp.getState().navigate({ name: "settings", section });
}
