import { clamp } from "./clamp";

export const TEXT_SCALE_MIN = 0.88;
export const TEXT_SCALE_MAX = 1.24;

export const clampTextScale = (x: number): number =>
  clamp(TEXT_SCALE_MIN, TEXT_SCALE_MAX, x);
