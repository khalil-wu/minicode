/**
 * Safe parsing utilities to prevent crashes from invalid input
 */

/**
 * Safely parse JSON string with fallback value
 * @param text - JSON string to parse
 * @param fallback - Value to return if parsing fails
 * @returns Parsed value or fallback
 */
export const safeJsonParse = <T>(text: string, fallback: T): T => {
  try {
    return JSON.parse(text) as T;
  } catch {
    return fallback;
  }
};

/**
 * Safely construct URL object
 * @param url - URL string to parse
 * @param base - Optional base URL
 * @returns URL object or null if invalid
 */
export const safeURL = (url: string, base?: string | URL): URL | null => {
  try {
    return new URL(url, base);
  } catch {
    return null;
  }
};
