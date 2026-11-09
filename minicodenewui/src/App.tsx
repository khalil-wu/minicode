import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { useEffect } from "react";
import { selectChat, useApp } from "./store";
import { Sidebar } from "./shell/Sidebar";
import { Topbar } from "./shell/Topbar";
import { ChatView, HomeView } from "./chat/ChatView";
import { Panel } from "./panel/Panel";
import { SettingsNav, SettingsPage } from "./pages/Settings";
import { AutomationsPage, PluginsPage } from "./pages/Pages";
import { CommandPalette, ConfirmDialog, ShortcutsDialog, Toasts, TooltipLayer } from "./overlays/Overlays";

function useSystemDark() {
  const theme = useApp((s) => s.settings.theme);
  useEffect(() => {
    if (theme !== "system") return;
    const mq = matchMedia("(prefers-color-scheme: dark)");
    const apply = () => (document.documentElement.dataset.theme = mq.matches ? "dark" : "light");
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, [theme]);
}

function useAppearance() {
  const s = useApp((st) => st.settings);
  useEffect(() => {
    const root = document.documentElement;
    if (s.theme !== "system") root.dataset.theme = s.theme;
    root.dataset.accent = s.accent;
    root.dataset.density = s.density;
    root.dataset.chatFont = s.chatFont;
    root.dataset.reduceMotion = String(s.reduceMotion);
    root.style.setProperty("--fs-chat", `${s.fontSize}px`);
  }, [s]);
  useSystemDark();
}

function useGlobalKeys() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const st = useApp.getState();
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      const key = e.key.toLowerCase();
      const map: Record<string, () => void> = {
        k: () => st.setPalette(!st.paletteOpen),
        n: () => st.newChat(),
        b: () => st.toggleSidebar(),
        j: () => st.togglePanel(),
        ",": () => st.navigate({ name: "settings", section: "general" }),
        "/": () => st.setShortcuts(!st.shortcutsOpen),
        l: () => window.dispatchEvent(new Event("minicode:focus-composer")),
      };
      const fn = map[key];
      if (fn && !e.shiftKey && !e.altKey) {
        e.preventDefault();
        fn();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

export function App() {
  useAppearance();
  useGlobalKeys();
  const route = useApp((s) => s.route);
  const sidebarOpen = useApp((s) => s.sidebarOpen);
  const panelOpen = useApp((s) => s.panelOpen);
  const chat = useApp(selectChat);
  const reduceMotion = useApp((s) => s.settings.reduceMotion);
  const inSettings = route.name === "settings";
  const showPanel = panelOpen && !inSettings;

  // 当前聊天被删除时，路由已经回到首页；这里兜底处理找不到聊天的情况
  const mainKey = route.name === "chat" ? `chat-${route.id}` : route.name;

  return (
    <MotionConfig reducedMotion={reduceMotion ? "always" : "user"}>
      <div className="app">
        <AnimatePresence initial={false}>
          {(sidebarOpen || inSettings) && (
            <motion.div
              key={inSettings ? "settings-nav" : "sidebar"}
              className="sidebar-wrap"
              initial={{ width: 0, opacity: 0 }}
              animate={{ width: 268, opacity: 1 }}
              exit={{ width: 0, opacity: 0 }}
              transition={{ type: "spring", stiffness: 420, damping: 42 }}
            >
              {inSettings ? <SettingsNav section={route.section} /> : <Sidebar />}
            </motion.div>
          )}
        </AnimatePresence>

        <main className="main">
          {!inSettings && <Topbar />}
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={mainKey}
              className="main-body"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.12 }}
            >
              {route.name === "home" && <HomeView />}
              {route.name === "chat" && (chat ? <ChatView chat={chat} /> : <HomeView />)}
              {route.name === "settings" && <SettingsPage section={route.section} />}
              {route.name === "plugins" && <PluginsPage />}
              {route.name === "automations" && <AutomationsPage />}
            </motion.div>
          </AnimatePresence>
        </main>

        <AnimatePresence initial={false}>{showPanel && <Panel key="panel" />}</AnimatePresence>
      </div>
      <CommandPalette />
      <ShortcutsDialog />
      <ConfirmDialog />
      <Toasts />
      <TooltipLayer />
    </MotionConfig>
  );
}
