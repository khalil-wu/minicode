export function withStableRenderKeys<T extends { id: string; kind?: string; type?: string }>(cells: T[]) {
  const seen = new Map<string, number>();
  return cells.map((cell) => {
    const base = `${cell.kind ?? cell.type ?? "item"}:${cell.id}`;
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    return {
      cell,
      key: count === 0 ? base : `${base}:${count}`,
    };
  });
}
