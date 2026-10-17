import { useSyncExternalStore } from "react";

interface ReviewExpansion {
  expanded: boolean;
  overrides: ReadonlyMap<string, boolean>;
  activePath?: string;
}
const routes = new Map<string, ReviewExpansion>();
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
function get(route: string) {
  if (!routes.has(route)) {
    routes.set(route, { expanded: true, overrides: new Map() });
    if (routes.size > 20) routes.delete(routes.keys().next().value!);
  }
  return routes.get(route)!;
}
function update(route: string, value: ReviewExpansion) { routes.set(route, value); listeners.forEach(listener => listener()); }

export function clearReviewExpansion(route: string) { routes.delete(route); listeners.forEach(listener => listener()); }

/** Source Qy RouteScope: in-memory global expansion and per-path overrides survive comparison changes. */
export function useReviewExpansion(route: string) {
  const state = useSyncExternalStore(subscribe, () => get(route));
  return {
    state,
    setFileOpen: (path: string, open: boolean) => {
      const current = get(route);
      update(route, { ...current, overrides: new Map([...current.overrides, [path, open]]) });
    },
    setExpanded: (expanded: boolean) => update(route, { ...get(route), expanded, overrides: new Map() }),
    setActivePath: (activePath?: string) => update(route, { ...get(route), activePath }),
    focusFile: (path: string) => update(route, { expanded: false, overrides: new Map([[path, true]]), activePath: path }),
  };
}
