import type { FileOpenTarget } from "../desktop/runtime";

export function FileOpenTargetIcon({ target }: { target: FileOpenTarget }) {
  return target.icon ? <img src={target.icon.startsWith("data:") ? target.icon : `${import.meta.env.BASE_URL}${target.icon}`}
    alt="" width={16} height={16} style={{ objectFit: "contain" }} /> : null;
}
