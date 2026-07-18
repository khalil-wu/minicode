import type { ContentBlock } from "../stores/types";
import {
  projectTurn,
  type ProjectTurnOptions,
  type TurnActivityItem,
  type TurnActivityStatus,
  type TurnProjection,
} from "../lib/turn-projection";
import { projectLegacyActivityItem } from "../agent-loop/projection/project-activity-items";
import type { ActivityItem } from "../agent-loop/activity-item";

/** Chat-only presentation evidence. Legacy turn shapes stop at this adapter. */
export interface ChatActivityItem extends TurnActivityItem {
  canonical?: ActivityItem;
}

export type ChatActivityStatus = TurnActivityStatus;

export interface ChatTurnProjection extends Omit<TurnProjection, "activityItems"> {
  activityItems: ChatActivityItem[];
}

export function projectChatTurn(
  blocks: ContentBlock[],
  options: ProjectTurnOptions,
  messageId?: string,
): ChatTurnProjection {
  const projection = projectTurn(blocks, options);
  return {
    ...projection,
    activityItems: projection.activityItems.map((item) => ({
      ...item,
      canonical: projectLegacyActivityItem(item, messageId),
    })),
  };
}

export function refreshChatActivityItem(item: ChatActivityItem, messageId?: string): ChatActivityItem {
  return {
    ...item,
    canonical: projectLegacyActivityItem(item, messageId, item.canonical?.turnId),
  };
}
