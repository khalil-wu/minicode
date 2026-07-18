export const sourceFaviconUrl = (href: string): string | null => {
  try {
    const url = new URL(href);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return `${url.origin}/favicon.ico`;
  } catch {
    return null;
  }
};

