export interface CompletionNotification {
  thread_source: string;
  automation_kind: "cron" | "heartbeat" | null;
  automation_notification_decision: "NOTIFY" | "DONT_NOTIFY" | null;
  heartbeat_assistant_message: {
    decision: "NOTIFY" | "DONT_NOTIFY";
    visible_text: string;
    notification_message: string | null;
  } | null;
  last_agent_message: string | null;
  has_pending_continuation: boolean;
  eligible: boolean;
  body: string | null;
  should_mark_unread: boolean;
}
