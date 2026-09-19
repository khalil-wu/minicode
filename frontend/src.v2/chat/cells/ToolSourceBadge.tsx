import type { ToolCallRecord } from "../../lib/tool-call-reducer";

export function ToolSourceBadge({ source }: { source?: ToolCallRecord["callSource"] }) {
  if (source?.kind === "extension") return <span className="activity-cell-detail-meta"
    data-parent-call={source.parent_call_id} title="由扩展调用，使用所属任务的工具权限">扩展</span>;
  if (source?.kind !== "code_mode") return null;
  return <span className="activity-cell-detail-meta" data-code-cell={source.cell_id} data-parent-call={source.parent_call_id}
    title={`由脚本 ${source.cell_id} 调用；上级调用 ${source.parent_call_id}`}>工具组合</span>;
}
