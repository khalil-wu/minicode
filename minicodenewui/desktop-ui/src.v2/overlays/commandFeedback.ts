import type { CommandResultEvent } from "../protocol/events";
import { commandResultSucceeded } from "../protocol/ws-outbox";
import { pushToast } from "./ToastContainer";

export const reportCommandFailure = (
  result: CommandResultEvent,
  action: string,
  fallback = "后端未返回具体原因",
): boolean => {
  if (commandResultSucceeded(result)) return false;
  pushToast(`${action}失败：${String(result.message || fallback)}`, "error");
  return true;
};
