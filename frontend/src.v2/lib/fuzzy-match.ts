/**
 * Lightweight fuzzy matching for autocomplete menus.
 * No external dependencies.
 */

export function fuzzyScore(query: string, target: string): number | null {
  const q = query.trim().toLowerCase();
  const raw = query.trim();
  const t = target.toLowerCase();

  if (!q) return 0;

  let score = 0;
  let qi = 0;
  let lastMatchIdx = -1;

  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      score += 1;
      // Consecutive match bonus
      if (lastMatchIdx === ti - 1) score += 2;
      // Start-of-word bonus (after separator or at position 0)
      if (ti === 0 || /[\s\-_/\\.]/.test(t[ti - 1])) score += 3;
      // Exact case match bonus
      if (target[ti] === raw[qi]) score += 0.5;
      lastMatchIdx = ti;
      qi++;
    }
  }

  // All query chars must be found
  if (qi < q.length) return null;

  // Penalize long targets slightly (prefer shorter matches)
  score -= t.length * 0.05;

  return score;
}

export function fuzzyFilter<T>(
  items: T[],
  query: string,
  getText: (item: T) => string,
): T[] {
  // Treat leading/trailing whitespace as a no-op: trim only the query,
  // never the target text or the caller's items array.
  const trimmed = query.trim();
  if (!trimmed) return items;

  const scored: { item: T; score: number }[] = [];
  for (const item of items) {
    const s = fuzzyScore(trimmed, getText(item));
    if (s !== null) scored.push({ item, score: s });
  }

  scored.sort((a, b) => b.score - a.score);
  return scored.map((s) => s.item);
}
