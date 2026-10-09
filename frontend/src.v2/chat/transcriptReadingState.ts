import { createContext, useCallback, useContext, useRef, useState, type Dispatch, type SetStateAction } from "react";
import type { VirtualItem } from "@tanstack/react-virtual";

export type TranscriptViewport = {
  scrollTop: number;
  isFollowing: boolean;
  showAllHistory: boolean;
  measurements: VirtualItem[];
};
type ConversationReadingState = {
  viewport?: TranscriptViewport;
  turns: Map<string, Map<string, unknown>>;
};

// Reading choices belong to the transcript, not to a mounted virtual row.
// They survive switching and virtualization; deleting the owner releases them.
const conversationReadingStates = new Map<string, ConversationReadingState>();
export const TranscriptReadingContext = createContext<Map<string, unknown> | null>(null);

export function conversationReadingState(conversationId: string): ConversationReadingState {
  let reading = conversationReadingStates.get(conversationId);
  if (!reading) {
    reading = { turns: new Map() };
    conversationReadingStates.set(conversationId, reading);
  }
  return reading;
}

export function turnReadingState(conversationId: string, turnId: string): Map<string, unknown> {
  const reading = conversationReadingState(conversationId);
  let preferences = reading.turns.get(turnId);
  if (!preferences) {
    preferences = new Map();
    reading.turns.set(turnId, preferences);
  }
  return preferences;
}

export const releaseConversationReadingState = (conversationId: string): void => {
  conversationReadingStates.delete(conversationId);
};

export function useTranscriptReadingPreference<T>(key: string, initial: T) {
  const preferences = useContext(TranscriptReadingContext);
  const userToggled = useRef(preferences?.has(key) ?? false);
  const [value, setValue] = useState<T>(() => preferences?.has(key) ? preferences.get(key) as T : initial);
  const update: Dispatch<SetStateAction<T>> = useCallback((next) => {
    setValue((previous) => {
      const value = typeof next === "function" ? (next as (value: T) => T)(previous) : next;
      if (userToggled.current) preferences?.set(key, value);
      return value;
    });
  }, [key, preferences]);
  return [value, update, userToggled] as const;
}
