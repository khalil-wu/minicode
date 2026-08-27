const MEDIA_TYPE_BY_EXTENSION: Readonly<Record<string, string>> = {
  avif: "image/avif",
  bmp: "image/bmp",
  csv: "text/csv",
  css: "text/css",
  gif: "image/gif",
  heic: "image/heic",
  heif: "image/heif",
  html: "text/html",
  htm: "text/html",
  ico: "image/x-icon",
  jpeg: "image/jpeg",
  jpg: "image/jpeg",
  js: "text/javascript",
  json: "application/json",
  jsx: "text/jsx",
  md: "text/markdown",
  mdx: "text/markdown",
  pdf: "application/pdf",
  png: "image/png",
  svg: "image/svg+xml",
  tif: "image/tiff",
  tiff: "image/tiff",
  ts: "text/typescript",
  tsx: "text/tsx",
  txt: "text/plain",
  webp: "image/webp",
  xml: "application/xml",
  yaml: "application/yaml",
  yml: "application/yaml",
};

const extensionForPath = (path: string): string => {
  let cleanPath = String(path || "").split(/[?#]/, 1)[0];
  // Workspace paths can arrive from a transcript or resource URL with one
  // level of percent encoding. Decode only for classification; the original
  // path remains untouched for URL construction.
  try {
    cleanPath = decodeURIComponent(cleanPath);
  } catch {
    // A malformed URL escape is not a valid filename suffix.
  }
  cleanPath = cleanPath.replace(/\\/g, "/");
  const name = cleanPath.split("/").filter(Boolean).pop() || cleanPath;
  const separator = name.lastIndexOf(".");
  return separator >= 0 ? name.slice(separator + 1).toLowerCase() : "";
};

export const mediaTypeForPath = (path: string): string =>
  MEDIA_TYPE_BY_EXTENSION[extensionForPath(path)] || "application/octet-stream";

export const isImagePath = (path: string): boolean =>
  mediaTypeForPath(path).startsWith("image/");

export const isPdfPath = (path: string): boolean =>
  mediaTypeForPath(path) === "application/pdf";

export const isPreviewableMediaPath = (path: string): boolean =>
  isImagePath(path) || isPdfPath(path);

export const isNativePreviewMediaType = (mediaType: string): boolean => {
  const normalized = String(mediaType || "").split(";", 1)[0].trim().toLowerCase();
  return normalized === "application/pdf" || normalized.startsWith("image/");
};

export const isTextMediaType = (mediaType: string, name = ""): boolean => {
  const normalized = String(mediaType || "").split(";", 1)[0].trim().toLowerCase();
  return normalized.startsWith("text/")
    || /(?:json|xml|yaml|toml|javascript|typescript|sql)/i.test(normalized)
    || /\.(?:txt|md|mdx|json|csv|ya?ml|toml|xml|html?|css|jsx?|tsx?|py|java|go|rs|c|cc|cpp|h|hpp|sh|ps1|sql)$/i.test(name);
};

export const kindForMediaType = (mediaType: string): "image" | "document" | "binary" => {
  const normalized = String(mediaType || "").split(";", 1)[0].trim().toLowerCase();
  if (normalized.startsWith("image/")) return "image";
  if (normalized === "application/pdf" || isTextMediaType(normalized)) return "document";
  return "binary";
};
