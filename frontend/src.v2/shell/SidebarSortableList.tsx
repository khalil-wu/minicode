import { useState, type ReactNode, type Ref } from "react";
import { createPortal } from "react-dom";
import { DndContext, DragOverlay, PointerSensor, closestCenter, useSensor, useSensors, type DraggableAttributes, type DragEndEvent, type DragOverEvent } from "@dnd-kit/core";
import { SortableContext, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import "./PinnedConversationList.css";

export const moveSidebarBefore = (identifiers: string[], movedId: string, beforeId: string | null): string[] => {
  if (beforeId === movedId) return identifiers;
  const remaining = identifiers.filter(identifier => identifier !== movedId);
  const position = beforeId === null ? remaining.length : remaining.indexOf(beforeId);
  return position === -1 ? identifiers : [...remaining.slice(0, position), movedId, ...remaining.slice(position)];
};
export type SidebarDragBinding = { dragActivatorRef: Ref<HTMLButtonElement>; dragAttributes?: DraggableAttributes };
type Scope = "pinned" | "project" | "recent" | "projects";
type Props = {
  identifiers: string[]; scope: Scope; ariaLabel: string;
  renderRow: (identifier: string, binding: SidebarDragBinding) => ReactNode;
  renderOverlay: (identifier: string) => ReactNode;
  onReorder: (movedId: string, beforeId: string | null, visibleIds: string[]) => Promise<boolean>;
  disabled?: boolean; renamingId?: string | null; onDragStart: () => void;
};
const SortableSidebarRow = ({ identifier, dragging, disabled, scope, renderRow, onMove }: {
  identifier: string; dragging: boolean; disabled: boolean; scope: Scope; renderRow: Props["renderRow"];
  onMove: (identifier: string, offset: number) => void;
}) => {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({ id: identifier, disabled });
  return <div ref={element => { setNodeRef(element); if (element) element.inert = isDragging; }} role="listitem"
    data-sidebar-sort-id={identifier} data-sidebar-sort-scope={scope} data-sidebar-sort-dragging={isDragging || undefined}
    data-pinned-sortable-id={scope === "pinned" ? identifier : undefined} data-pinned-dragging={scope === "pinned" && isDragging || undefined}
    style={{ transform: dragging ? undefined : CSS.Translate.toString(transform), transition: dragging ? undefined : transition }}
    {...listeners} onKeyDown={undefined} onPointerDown={event => {
      const target = event.target as HTMLElement;
      if (scope === "projects" && target.closest(".mc-workspace-group-toggle") !== event.currentTarget.querySelector("button")) return;
      if (!target.closest(".session-row-actions, .session-row-pin, .mc-workspace-new-session, input")) listeners?.onPointerDown?.(event);
    }} onKeyDownCapture={event => {
      if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || event.target !== event.currentTarget.querySelector("button") || disabled) return;
      if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault(); event.stopPropagation(); onMove(identifier, event.key === "ArrowUp" ? -1 : 1);
      }
    }}>
    {renderRow(identifier, { dragActivatorRef: setActivatorNodeRef, dragAttributes: disabled ? undefined : attributes })}
  </div>;
};
export const SidebarSortableList = ({ identifiers: canonicalIds, scope, ariaLabel, renderRow, renderOverlay, onReorder, disabled = false, renamingId, onDragStart }: Props) => {
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }));
  const [activeId, setActiveId] = useState<string | null>(null);
  const [beforeId, setBeforeId] = useState<string | null | undefined>(undefined);
  const [pendingOrder, setPendingOrder] = useState<string[] | null>(null);
  const identifiers = pendingOrder && pendingOrder.length === canonicalIds.length && canonicalIds.every(identifier => pendingOrder.includes(identifier)) ? pendingOrder : canonicalIds;
  const commitMove = async (movedId: string, before: string | null) => {
    const next = moveSidebarBefore(identifiers, movedId, before);
    if (next.every((identifier, index) => identifier === identifiers[index])) return;
    setPendingOrder(next);
    try { await onReorder(movedId, before, identifiers); } finally { setPendingOrder(order => order === next ? null : order); }
  };
  const placement = (event: DragOverEvent | DragEndEvent): string | null | undefined => {
    if (!event.over) return undefined;
    const over = String(event.over.id), translated = event.active.rect.current.translated;
    if (!translated) return undefined;
    const activator = event.activatorEvent;
    const pointerY = "clientY" in activator ? Number(activator.clientY) + event.delta.y : translated.top + translated.height / 2;
    return pointerY < event.over.rect.top + event.over.rect.height / 2 ? over : identifiers[identifiers.indexOf(over) + 1] ?? null;
  };
  return <DndContext sensors={sensors} collisionDetection={closestCenter}
    onDragStart={event => { setActiveId(String(event.active.id)); setBeforeId(undefined); onDragStart(); }}
    onDragMove={event => setBeforeId(placement(event))} onDragOver={event => setBeforeId(placement(event))}
    onDragCancel={() => { setActiveId(null); setBeforeId(undefined); }}
    onDragEnd={event => { const before = placement(event); setActiveId(null); setBeforeId(undefined); if (before !== undefined) void commitMove(String(event.active.id), before); }}>
    <SortableContext items={identifiers} strategy={verticalListSortingStrategy}>
      <div role="list" aria-label={ariaLabel} className={`mc-sidebar-sortable-list ${scope === "pinned" ? "mc-pinned-sortable-list" : ""}`} data-sidebar-sort-list={scope}
        data-sidebar-list-dragging={activeId !== null || undefined} data-pinned-list-dragging={scope === "pinned" && activeId !== null || undefined}>
        {identifiers.map(identifier => <div key={identifier} className="mc-pinned-slot">
          {activeId && beforeId === identifier && <div className="mc-pinned-drop-line" aria-hidden="true" />}
          <SortableSidebarRow identifier={identifier} dragging={activeId !== null} disabled={disabled || renamingId === identifier} scope={scope}
            renderRow={renderRow} onMove={(id, offset) => {
              const current = identifiers.indexOf(id), target = current + offset;
              if (target < 0 || target >= identifiers.length) return;
              void commitMove(id, offset < 0 ? identifiers[target] : identifiers[target + 1] ?? null);
            }} />
        </div>)}
        {activeId && beforeId === null && <div className="mc-pinned-drop-line mc-pinned-drop-line-end" aria-hidden="true" />}
      </div>
    </SortableContext>
    {createPortal(<DragOverlay dropAnimation={null} adjustScale={false}>
      {activeId && canonicalIds.includes(activeId) && <div className={`mc-sidebar-sort-overlay ${scope === "pinned" ? "mc-pinned-drag-overlay" : ""}`}
        data-sidebar-sort-overlay={scope} aria-hidden="true" ref={element => { if (element) element.inert = true; }}>{renderOverlay(activeId)}</div>}
    </DragOverlay>, document.body)}
  </DndContext>;
};
