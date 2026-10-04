import { pushToast } from "../overlays/ToastContainer";

/** User clipboard actions share one browser/permission failure boundary. */
export async function copyText(text: string, subject: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
  } catch (error) {
    pushToast(`复制${subject}失败：${error instanceof Error ? error.message : String(error)}`, "error", 3000);
    return false;
  }
  return true;
}
