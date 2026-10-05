import { clamp } from "./clamp";

export const TEXT_SCALE_MIN = 11 / 14;
export const TEXT_SCALE_MAX = 21 / 14;

export const clampTextScale = (x: number): number =>
  clamp(TEXT_SCALE_MIN, TEXT_SCALE_MAX, x);
