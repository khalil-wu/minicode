import { create } from "zustand";
import { subscribeWithSelector } from "zustand/middleware";
import type { AppStore } from "./types";

import { createUISlice } from "./ui-slice";
import { createWorkspaceSlice } from "./workspace-slice";
import { createChatSlice } from "./chat-slice";
import { createComposerSlice } from "./composer-slice";
import { createAgentSlice } from "./agent-slice";
import { createApprovalSlice } from "./approval-slice";
import { createInspectorSlice } from "./inspector-slice";
import { createEditorSlice } from "./editor-slice";
import { applyTheme, applyTextScale } from "./shared-helpers";

export const useAppStore = create<AppStore>()(
  subscribeWithSelector((...a) => ({
    ...createUISlice(...a),
    ...createWorkspaceSlice(...a),
    ...createChatSlice(...a),
    ...createComposerSlice(...a),
    ...createAgentSlice(...a),
    ...createApprovalSlice(...a),
    ...createInspectorSlice(...a),
    ...createEditorSlice(...a),
  })),
);

if (typeof window !== "undefined") {
  (window as typeof window & { __zustandStore?: typeof useAppStore }).__zustandStore = useAppStore;
  applyTheme(useAppStore.getState().themeMode);
  applyTextScale(useAppStore.getState().textScale);
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
    if (useAppStore.getState().themeMode === "system") applyTheme("system");
  });
}
