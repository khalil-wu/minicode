import { pushToast } from "../overlays/ToastContainer";

/** User clipboard actions share one browser/permission failure boundary. */
export async function copyContent(content: string | Record<"text/plain" | "text/html", string>, subject: string, successMessage?: string): Promise<boolean> {
  try {
    if (typeof content !== "string" && "write" in navigator.clipboard && typeof ClipboardItem !== "undefined") {
      await navigator.clipboard.write([new ClipboardItem(Object.fromEntries(
        Object.entries(content).map(([type, text]) => [type, new Blob([text], { type })]),
      ))]);
    } else await navigator.clipboard.writeText(typeof content === "string" ? content : content["text/plain"]);
  } catch (error) {
    pushToast(`复制${subject}失败：${error instanceof Error ? error.message : String(error)}`, "error", 3000);
    return false;
  }
  if (successMessage) pushToast(successMessage, "success", 2000);
  return true;
}

export async function copyText(text: string, subject: string, successMessage?: string): Promise<boolean> {
  return copyContent(text, subject, successMessage);
}
