export function enableAgentLoopTimelineV2(): boolean {
  if (typeof window === "undefined") return true;
  try {
    return window.localStorage.getItem("minicode.agentLoopTimelineV2") !== "0";
  } catch {
    return true;
  }
}
