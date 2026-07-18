import { ApiError, apiBase, authHeaders, errorMessageFromResponseText } from "./api";

export interface ReplayExportPayload {
  kind: "minicode_ws_replay_export";
  schema_version: 1;
  session_id: string;
  conversation_id?: string | null;
  after_seq: number;
  current_seq: number;
  event_count: number;
  first_seq?: number | null;
  last_seq?: number | null;
  sequence_gaps: Array<{ after: number; before: number; missing: number }>;
  can_replay_without_gap: boolean;
  type_counts: Record<string, number>;
  omitted_fields: string[];
  truncated_fields: string[];
  events: Record<string, unknown>[];
}

export const fetchReplayExport = async ({
  sessionId,
  conversationId,
  afterSeq = 0,
  limit = 500,
}: {
  sessionId: string;
  conversationId?: string | null;
  afterSeq?: number;
  limit?: number;
}): Promise<ReplayExportPayload> => {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  params.set("after_seq", String(afterSeq));
  if (conversationId) params.set("conversation_id", conversationId);
  const res = await fetch(`${apiBase()}/api/replay/${encodeURIComponent(sessionId)}?${params.toString()}`, {
    cache: "no-store",
    headers: authHeaders(),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, errorMessageFromResponseText(text, res.statusText));
  }
  return (await res.json()) as ReplayExportPayload;
};
