import { createContext, useContext } from "react";

/** Search temporarily exposes loaded evidence without changing disclosure preferences. */
export const TranscriptSearchContext = createContext(false);
export const useTranscriptSearch = () => useContext(TranscriptSearchContext);
