import { useCallback, useRef, useState } from "react";

interface Props {
  orientation: "vertical" | "horizontal";
  onResize: (delta: number) => void;
}

export const ResizeHandle = ({ orientation, onResize }: Props) => {
  const startRef = useRef(0);
  const draggingRef = useRef(false);
  const [hovered, setHovered] = useState(false);
  const [dragging, setDragging] = useState(false);

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      e.preventDefault();
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      startRef.current = orientation === "vertical" ? e.clientX : e.clientY;
      draggingRef.current = true;
      setDragging(true);
      document.body.classList.add("layout-dragging");
    },
    [orientation],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!draggingRef.current) return;
      const cur = orientation === "vertical" ? e.clientX : e.clientY;
      const delta = cur - startRef.current;
      if (delta !== 0) {
        startRef.current = cur;
        requestAnimationFrame(() => onResize(delta));
      }
    },
    [orientation, onResize],
  );

  const onPointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      try {
        (e.target as HTMLElement).releasePointerCapture(e.pointerId);
      } catch {
        /* noop */
      }
      draggingRef.current = false;
      setDragging(false);
      document.body.classList.remove("layout-dragging");
    },
    [],
  );

  const isV = orientation === "vertical";
  return (
    <div
      role="separator"
      aria-orientation={isV ? "vertical" : "horizontal"}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerEnter={() => setHovered(true)}
      onPointerLeave={() => { if (!draggingRef.current) setHovered(false); }}
      style={{
        flex: "0 0 auto",
        width: isV ? 6 : "100%",
        height: isV ? "100%" : 6,
        cursor: isV ? "col-resize" : "row-resize",
        background: "transparent",
        position: "relative",
        zIndex: 2,
      }}
    >
      <div
        style={{
          position: "absolute",
          ...(isV
            ? { top: 0, bottom: 0, left: "50%", transform: "translateX(-50%)", width: dragging ? 3 : hovered ? 2 : 1 }
            : { left: 0, right: 0, top: "50%", transform: "translateY(-50%)", height: dragging ? 3 : hovered ? 2 : 1 }),
          background: dragging
            ? "var(--accent-primary)"
            : hovered
              ? "var(--border-strong)"
              : "var(--border-subtle)",
          borderRadius: 2,
          transition: "background 150ms ease, width 150ms ease, height 150ms ease",
        }}
      />
    </div>
  );
};
