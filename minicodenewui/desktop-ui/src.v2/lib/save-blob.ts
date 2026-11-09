import { desktop, isDesktop } from "../desktop/runtime";

/** Keep the original bytes alive until the native Save as operation completes. */
export async function saveBlobFile(blob: Blob, filename: string): Promise<boolean> {
  if (isDesktop()) {
    return desktop()!.saveFile(filename, new Uint8Array(await blob.arrayBuffer()));
  }
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Browser downloads expose no completion event. The page owns this URL until
  // it is left, so a pending download cannot lose its original byte source.
  window.addEventListener("pagehide", () => URL.revokeObjectURL(url), { once: true });
  return true;
}
