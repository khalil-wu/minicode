import { createContext, useContext } from "react";

/**
 * Whether the owning right-sidebar panel is the visible tab. Panels stay
 * mounted while hidden so their local state survives; standalone renders
 * default to visible.
 */
export const PanelVisibilityContext = createContext(true);

export const usePanelVisible = (): boolean => useContext(PanelVisibilityContext);
