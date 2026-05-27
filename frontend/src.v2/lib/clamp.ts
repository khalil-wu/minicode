export const clamp = (min: number, max: number, x: number): number => {
  if (!Number.isFinite(x)) return min;
  return Math.min(max, Math.max(min, x));
};
