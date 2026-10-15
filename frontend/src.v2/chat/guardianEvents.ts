import type { GuardianAssessmentEvent, GuardianWarningEvent } from "../protocol/guardian-types";
import type { ChatMessage, ContentBlock } from "../stores/types";
import { useAppStore } from "../stores";
import { updateMessagesForConversation } from "../stores/shared-helpers";

export function applyGuardianEvent(messages: ChatMessage[], event: GuardianAssessmentEvent | GuardianWarningEvent): ChatMessage[] | null {
  const index = messages.findIndex(message => message.id === event.message_id && message.role === "assistant" && message.turnId === event.turn_id);
  if (index < 0) return null;
  const message = messages[index];
  const blocks = [...(message.blocks ?? [])];
  if (event.type === "guardian_assessment") {
    const { type: _type, conversation_id: _conversation, message_id: _message, ...review } = event;
    const previousIndex = blocks.findIndex(block => block.type === "guardian_assessment" && block.review.id === review.id);
    const previous = blocks[previousIndex];
    if (previous?.type === "guardian_assessment" && previous.review.status !== "in_progress" && review.status === "in_progress") return null;
    const block: ContentBlock = { type: "guardian_assessment", review };
    if (previousIndex < 0) blocks.push(block); else blocks[previousIndex] = block;
  } else {
    const duplicate = blocks.some(block => block.type === "guardian_warning" && block.message === event.message && block.interruption === event.interruption);
    if (duplicate) return null;
    blocks.push({ type: "guardian_warning", message: event.message, interruption: event.interruption, turnId: event.turn_id });
  }
  const next = messages.slice();
  next[index] = { ...message, blocks };
  return next;
}

export function handleGuardianEvent(event: GuardianAssessmentEvent | GuardianWarningEvent): boolean {
  useAppStore.setState(state => updateMessagesForConversation(state, event.conversation_id, messages => applyGuardianEvent(messages, event)));
  return true;
}
