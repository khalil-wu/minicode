/** Realtime session types. Session secrets remain on the owner host. */
export type RealtimeVersion = "v1" | "v2" | "v3";
export type RealtimePhase = "inactive" | "starting" | "active" | "stopping";

export interface RealtimeCapability {
  enabled: boolean;
  configured: boolean;
  transports: Array<"websocket" | "webrtc" | "existingCall">;
  version: RealtimeVersion;
  model: string;
  inputSampleRate: number;
  inputChannels: number;
  transportVersions?: Record<"websocket" | "webrtc" | "existingCall", RealtimeVersion[]>;
}

export interface RealtimeAudioChunk {
  data: string;
  sampleRate: number;
  numChannels: number;
  samplesPerChannel?: number | null;
  itemId?: string | null;
}

export type RealtimeItem = {
  id: string;
  realtimeSessionId: string;
} & (
  | { type: "realtimeSessionStarted" }
  | { type: "transcriptSegment"; role: "user" | "assistant"; text: string }
  | { type: "bemItemPromoted"; turnId: string; itemId: string; presentation: { type: "wholeItem" | "inlineMarkdown" } | { type: "inlineVisualization"; index: number } }
  | { type: "realtimeSessionClosed"; outcome: "ended" | "failed" }
);

type RealtimeOwner = { threadId: string; conversation_id: string; client_command_id?: string };
export type RealtimeCommand = RealtimeOwner & (
  | { type: "thread/realtime/start"; outputModality: "audio" | "text"; version?: RealtimeVersion;
      transport?: { type: "websocket" } | { type: "webrtc"; sdp: string } | { type: "existingCall"; callId: string };
      voice?: string; model?: string; prompt?: string | null; includeStartupContext?: boolean;
      initialItems?: Array<{ role: "user" | "assistant" | "system" | "developer"; text: string }>;
      realtimeSessionId?: string; realtimeStartInstructions?: string; realtimeEndInstructions?: string;
      clientManagedHandoffs?: boolean; flushTranscriptTailOnSessionEnd?: boolean; delegationAckFiller?: boolean;
      codexResponsesAsItems?: boolean; codexResponseItemPrefix?: string; backendReasoningStatus?: boolean;
      codexResponseHandoffMode?: "thinking" | "commentary" | "bemTags";
      codexResponseHandoffChannelPrefixes?: Record<string, string[]>;
      desktopVoice?: import("../desktop/runtime").DesktopVoiceClaim }
  | { type: "thread/realtime/appendAudio"; audio: RealtimeAudioChunk }
  | { type: "thread/realtime/appendText"; role?: "user" | "assistant" | "system" | "developer"; text: string }
  | { type: "thread/realtime/appendSpeech"; text: string }
  | { type: "thread/realtime/stop" }
  | { type: "thread/realtime/listVoices" }
);

export type RealtimeEvent = RealtimeOwner & (
  | { type: "thread/realtime/started"; realtimeSessionId: string | null; version: RealtimeVersion }
  | { type: "thread/realtime/itemAdded"; item: Record<string, unknown> }
  | { type: "thread/realtime/item/started" | "thread/realtime/item/completed"; item: RealtimeItem }
  | { type: "thread/realtime/item/transcript/delta"; itemId: string; delta: string }
  | { type: "thread/realtime/transcript/delta"; role: string; delta: string }
  | { type: "thread/realtime/transcript/done"; role: string; text: string }
  | { type: "thread/realtime/outputAudio/delta"; audio: RealtimeAudioChunk }
  | { type: "thread/realtime/sdp"; sdp: string }
  | { type: "thread/realtime/error"; message: string }
  | { type: "thread/realtime/closed"; reason: string | null }
);

export const REALTIME_EVENT_TYPES = [
  "thread/realtime/started", "thread/realtime/itemAdded", "thread/realtime/item/started",
  "thread/realtime/item/transcript/delta", "thread/realtime/item/completed", "thread/realtime/transcript/delta",
  "thread/realtime/transcript/done", "thread/realtime/outputAudio/delta", "thread/realtime/sdp",
  "thread/realtime/error", "thread/realtime/closed",
] as const;
export const REALTIME_COMMAND_TYPES = [
  "thread/realtime/start", "thread/realtime/appendAudio", "thread/realtime/appendText",
  "thread/realtime/appendSpeech", "thread/realtime/stop", "thread/realtime/listVoices",
] as const;

export const isRealtimeCommand = (command: { type: string }): boolean =>
  (REALTIME_COMMAND_TYPES as readonly string[]).includes(command.type);

export const isRealtimeEvent = (event: { type: string }): event is RealtimeEvent =>
  (REALTIME_EVENT_TYPES as readonly string[]).includes(event.type);

export const isRealtimeItem = (value: unknown): value is RealtimeItem => {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  if (typeof item.id !== "string" || !item.id || typeof item.realtimeSessionId !== "string" || !item.realtimeSessionId) return false;
  switch (item.type) {
    case "realtimeSessionStarted": return true;
    case "transcriptSegment": return (item.role === "user" || item.role === "assistant") && typeof item.text === "string";
    case "bemItemPromoted": return typeof item.turnId === "string" && typeof item.itemId === "string"
      && !!item.presentation && typeof item.presentation === "object";
    case "realtimeSessionClosed": return item.outcome === "ended" || item.outcome === "failed";
    default: return false;
  }
};
