import { useAppStore } from "../stores";
import type { CommandsListEvent, ServerEvent } from "../protocol/events";
import { normalizeSkillList, normalizeSlashCommands } from "../lib/catalog-normalizers";
import { addInspectorPayload } from "./inspectorEntries";

export const handleCommandCatalogEvent = (e: ServerEvent): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "skills.list": {
      if (e.skills) {
        s.setAvailableSkills(normalizeSkillList(e.skills));
      }
      return true;
    }
    case "commands.list": {
      const ev = e as CommandsListEvent;
      const activeConversationId = String(s.conversationId || "").trim() || null;
      const owner = typeof ev.conversation_id === "string"
        ? ev.conversation_id.trim() || null
        : null;
      // A catalog is a snapshot of one executable Pi/host command scope. Late
      // responses from a previous conversation must not replace the active
      // palette; the explicit null owner is only valid before a conversation
      // exists on either side.
      if (owner !== activeConversationId) return true;
      const commands = normalizeSlashCommands(ev.commands);
      s.setSlashCommands(commands);
      const sourceCounts = commands.reduce<Record<string, number>>((counts, command) => {
        const source = command.source || "unknown";
        counts[source] = (counts[source] ?? 0) + 1;
        return counts;
      }, {});
      addInspectorPayload("session", `commands:${owner || "session"}`, {
        event: ev.type,
        conversation_id: owner,
        request_id: ev.request_id,
        count: commands.length,
        sources: sourceCounts,
        commands: commands.map((command) => ({
          name: command.command,
          type: command.type,
          source: command.source,
          availability: command.availability,
          extension_path: command.extensionPath,
          source_path: command.sourcePath,
        })),
      });
      return true;
    }
    case "skills.marketplace.list": {
      if (e.skills) {
        s.setMarketplaceSkills(e.skills.map((sk: { name: string; title?: string; description: string; triggers?: string[]; installed?: boolean }) => ({
          name: sk.name,
          title: sk.title ?? sk.name,
          description: sk.description,
          triggers: sk.triggers ?? [],
          installed: sk.installed ?? false,
        })));
      }
      return true;
    }
    default:
      return false;
  }
};
