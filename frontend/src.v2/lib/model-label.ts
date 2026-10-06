export const formatModelLabel = (model: string | null | undefined, fallback = "--"): string => {
  const value = String(model || "").trim();
  if (/^gpt-\d/i.test(value)) return value.slice(4).replace(/-([a-z])/gi, (_match, letter: string) => " " + letter.toUpperCase());
  return value || fallback;
};
