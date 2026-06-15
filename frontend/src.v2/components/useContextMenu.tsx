import { useCallback, useMemo, useState } from "react";
import type React from "react";
import { ContextMenu, type ContextMenuItem } from "./ContextMenu";

/**
 * Hook that wires up a right-click context menu.
 *
 * Usage:
 *   const { onContextMenu, menu } = useContextMenu(items);
 *   return <div onContextMenu={onContextMenu}>{menu}</div>;
 *
 * `items` may be a static array or a function returning items (evaluated lazily on right-click).
 */
export function useContextMenu(
  itemsOrFactory: ContextMenuItem[] | (() => ContextMenuItem[]),
) {
  const [state, setState] = useState<{
    position: { x: number; y: number };
    items: ContextMenuItem[];
  } | null>(null);

  const onContextMenu = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const items =
        typeof itemsOrFactory === "function" ? itemsOrFactory() : itemsOrFactory;
      setState({ position: { x: e.clientX, y: e.clientY }, items });
    },
    // itemsOrFactory may be a new reference on every render; we intentionally
    // capture the latest value via ref-less closure by including it here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [itemsOrFactory],
  );

  const close = useCallback(() => setState(null), []);

  const menu = useMemo(
    () =>
      state ? (
        <ContextMenu
          items={state.items}
          position={state.position}
          onClose={close}
        />
      ) : null,
    [state, close],
  );

  return { onContextMenu, menu, close };
}
