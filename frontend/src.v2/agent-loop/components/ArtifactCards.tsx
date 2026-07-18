import type { DiffCellState } from "../../chat/cells/cellTypes";
import { FileChangesCard } from "./FileChangesCard";
import { withStableRenderKeys } from "./renderKeys";

export function ArtifactCards({
  cells,
}: {
  cells: DiffCellState[];
}) {
  if (cells.length === 0) return null;

  return (
    <section className="agent-loop-artifacts" aria-label="Agent deliverables">
      {withStableRenderKeys(cells).map(({ cell, key }) => (
        <FileChangesCard key={key} cell={cell} />
      ))}
    </section>
  );
}
