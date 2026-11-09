import type { AdditionalPermissionProfile } from "./common-types";

export type GuardianStatus = "in_progress" | "approved" | "denied" | "timed_out" | "aborted";
export type GuardianAction =
  | { type: "command"; source: "unified_exec"; command: string; cwd: string }
  | { type: "write_stdin"; approval_id: string; process_id: string; stdin: string; cwd: string }
  | { type: "apply_patch"; cwd: string; files: string[] }
  | { type: "request_permissions"; reason?: string | null; permissions: AdditionalPermissionProfile }
  | { type: "network_access"; target: string; host: string; protocol: string; port: number }
  | { type: "mcp_tool_call"; server: string; tool_name: string; connector_id?: string | null; connector_name?: string | null; tool_title?: string | null };

export interface GuardianAssessment {
  id: string;
  target_item_id?: string;
  turn_id: string;
  started_at_ms: number;
  completed_at_ms?: number;
  status: GuardianStatus;
  action: GuardianAction;
  risk_level?: "low" | "medium" | "high" | "critical";
  user_authorization?: "unknown" | "low" | "medium" | "high";
  rationale?: string;
  decision_source?: "agent";
  review_reason?: string;
}

export interface GuardianAssessmentEvent extends GuardianAssessment {
  type: "guardian_assessment";
  conversation_id: string;
  message_id: string;
}

export interface GuardianWarningEvent {
  type: "guardian_warning";
  conversation_id: string;
  message_id: string;
  turn_id: string;
  message: string;
  interruption?: boolean;
}

export interface ApproveGuardianDeniedActionCommand {
  type: "thread/approveGuardianDeniedAction";
  threadId: string;
  conversation_id: string;
  event: GuardianAssessment;
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string";
const optionalText = (value: unknown): boolean => value === undefined || value === null || text(value);
const time = (value: unknown): boolean => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

export function isGuardianAction(value: unknown): value is GuardianAction {
  if (!record(value)) return false;
  switch (value.type) {
    case "command": return value.source === "unified_exec" && text(value.command) && text(value.cwd);
    case "write_stdin": return text(value.approval_id) && text(value.process_id) && text(value.stdin) && text(value.cwd);
    case "apply_patch": return text(value.cwd) && Array.isArray(value.files) && value.files.every(text);
    case "request_permissions": return optionalText(value.reason) && record(value.permissions);
    case "network_access": return text(value.target) && text(value.host) && text(value.protocol) && typeof value.port === "number" && Number.isInteger(value.port);
    case "mcp_tool_call": return text(value.server) && text(value.tool_name)
      && optionalText(value.connector_id) && optionalText(value.connector_name) && optionalText(value.tool_title);
    default: return false;
  }
}

export function isGuardianAssessment(value: unknown): value is GuardianAssessment {
  return record(value) && text(value.id) && value.id.length > 0 && text(value.turn_id)
    && optionalText(value.target_item_id) && time(value.started_at_ms)
    && (value.completed_at_ms === undefined || time(value.completed_at_ms))
    && ["in_progress", "approved", "denied", "timed_out", "aborted"].includes(String(value.status))
    && isGuardianAction(value.action)
    && (value.risk_level === undefined || ["low", "medium", "high", "critical"].includes(String(value.risk_level)))
    && (value.user_authorization === undefined || ["unknown", "low", "medium", "high"].includes(String(value.user_authorization)))
    && optionalText(value.rationale) && optionalText(value.review_reason)
    && (value.decision_source === undefined || value.decision_source === "agent");
}
