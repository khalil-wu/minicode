export const formatModelLabel = (model: string | null | undefined, fallback = "--"): string => {
  const value = String(model || "").trim();
  return value || fallback;
};
