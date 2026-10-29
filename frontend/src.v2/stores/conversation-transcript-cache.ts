import type { AppStore } from "./types";

type Transcripts = AppStore["conversationMessages"];
const memberships = new WeakMap<Transcripts, object>();
const changedOwners = new WeakMap<Transcripts, string>();

export function conversationTranscriptMembership(transcripts: Transcripts): object {
  let key = memberships.get(transcripts);
  if (!key) { key = {}; memberships.set(transcripts, key); }
  return key;
}

export const changedTranscriptOwner = (transcripts: Transcripts): string | undefined => changedOwners.get(transcripts);

/** The single-owner writer knows whether this update added a cached conversation. */
export function cacheConversationTranscript(transcripts: Transcripts, owner: string, messages: AppStore["messages"]): Transcripts {
  if (transcripts[owner] === messages) return transcripts;
  const next = { ...transcripts, [owner]: messages };
  if (owner in transcripts) memberships.set(next, conversationTranscriptMembership(transcripts));
  changedOwners.set(next, owner);
  return next;
}
