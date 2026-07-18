export interface MentionFileItem {
  name: string;
  description: string;
  type: "file" | "folder";
  path: string;
  section?: string;
}

export const mentionTreeCache = new Map<string, MentionFileItem[]>();
export const mentionSearchCache = new Map<string, MentionFileItem[]>();

export const __clearMentionFileCacheForTests = () => {
  mentionTreeCache.clear();
  mentionSearchCache.clear();
};
