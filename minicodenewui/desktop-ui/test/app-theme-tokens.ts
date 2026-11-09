import { readFileSync } from "node:fs";

const tokens = readFileSync("src.v2/styles/tokens.css", "utf8");

/** A real renderer has these stylesheet tokens before its first React render. */
export function installAppThemeTokens() {
  const style = document.createElement("style");
  style.textContent = tokens;
  document.head.appendChild(style);
  return () => style.remove();
}
