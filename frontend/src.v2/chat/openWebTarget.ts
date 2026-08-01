import { openWebInBrowser } from "./openWebInBrowser";

/** Markdown/web links belong in the embedded Browser. Preview is opened explicitly
 * by the preview tool/panel for a running local app. */
export function openWebTarget(url: string): boolean {
  return openWebInBrowser(url);
}
