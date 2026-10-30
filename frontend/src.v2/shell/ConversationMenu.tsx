import { useEffect, useRef, useState, type ComponentType } from "react";
import type { ConversationMeta } from "../stores/types";
import { pushToast } from "../overlays/ToastContainer";

export interface ConversationMenuProps {
  anchor: HTMLElement | null;
  position?: { x: number; y: number };
  menuId: string;
  conversationId: string;
  workspaceRoot?: string;
  conversationType?: ConversationMeta["conversationType"];
  environment?: ConversationMeta["environment"];
  archived: boolean;
  isIsolated: boolean;
  canCopy: boolean;
  canHandoff: boolean;
  pinned?: boolean;
  hasUnreadTurn?: boolean;
  currentProject?: string;
  projectOptions?: { id: string; name: string }[];
  sectionOptions?: { id: string; name: string }[];
  currentSection?: string;
  onSectionChange?: (sectionId: string | null) => void;
  onNewSection?: () => void;
  onPin: () => void;
  onReadState: () => void;
  onProjectChange: (projectId: string) => void;
  onRename: () => void;
  onCopy: () => void;
  onCopyLink: () => void;
  onCopyMarkdown: () => void;
  onHandoff: () => void;
  onArchive: () => void;
  onClose: () => void;
}

type MenuSurface = ComponentType<ConversationMenuProps>;
let surface: MenuSurface | undefined;
let loading: Promise<MenuSurface> | undefined;

export function loadConversationMenu(): Promise<MenuSurface> {
  return loading ??= import("./ConversationMenuSurface").then(module => {
    surface = module.ConversationMenuSurface;
    return surface;
  }, error => { loading = undefined; throw error; });
}

/** The open request owns dismissal even while its interaction code is loading. */
export const ConversationMenu = (props: ConversationMenuProps) => {
  const [Surface, setSurface] = useState<MenuSurface | undefined>(() => surface);
  const owner = useRef(props);
  owner.current = props;
  useEffect(() => {
    if (Surface) return;
    let active = true;
    const close = (restoreFocus = false) => {
      if (!active) return;
      active = false;
      removeListeners();
      owner.current.onClose();
      if (restoreFocus) owner.current.anchor?.focus({ preventScroll: true });
    };
    const outside = (event: Event) => {
      if (!owner.current.position && owner.current.anchor && event.composedPath().includes(owner.current.anchor)) return;
      close();
    };
    const keyboard = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Tab") { close(); return; }
      const typeahead = event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey;
      if (!typeahead && !["Escape", "Enter", " ", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault(); event.stopPropagation();
      if (event.key === "Escape") close(true);
    };
    function removeListeners() {
      window.removeEventListener("mousedown", outside);
      window.removeEventListener("focusin", outside);
      document.removeEventListener("keydown", keyboard, true);
    }
    window.addEventListener("mousedown", outside);
    window.addEventListener("focusin", outside);
    document.addEventListener("keydown", keyboard, true);
    void loadConversationMenu().then(component => {
      if (!active) return;
      setSurface(() => component);
    }, error => {
      if (!active) return;
      close();
      pushToast(error instanceof Error ? error.message : String(error), "error");
    });
    return () => {
      active = false;
      removeListeners();
    };
  }, [Surface, props.conversationId, props.menuId]);
  return Surface ? <Surface {...props} /> : null;
};
