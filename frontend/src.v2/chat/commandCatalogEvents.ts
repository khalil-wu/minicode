import { useAppStore } from "../stores";
import { invalidateConversationDraftSelection } from "../stores/conversation-drafts";
import type { CommandsListEvent, ServerEvent } from "../protocol/events";
import { normalizeSkillList, normalizeSlashCommands } from "../lib/catalog-normalizers";
import { addInspectorPayload } from "./inspectorEntries";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { sendClientCommand } from "../protocol/ws-outbox";
import { captureExtensionSettingsOwner, publishExtensionSettingsChange } from "../lib/extension-settings-events";

export const handleCommandCatalogEvent = (e: ServerEvent): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "skills/changed": {
      publishExtensionSettingsChange("skills", captureExtensionSettingsOwner(), e);
      if (s.activeConversationDraftId) {
        void invalidateConversationDraftSelection(s.activeConversationDraftId);
        return true;
      }
      const owner = { conversation_id: s.conversationId || undefined, workspace_root: s.workingDirectory };
      sendClientCommand({ type: "skills.list", ...owner }, { silent: true });
      sendClientCommand({ type: "commands.list", ...owner }, { silent: true });
      sendClientCommand({ type: "runtime.capabilities.inspect", ...owner, source: "skills.changed" }, { silent: true });
      return true;
    }
    case "skills.list": {
      if (s.activeConversationDraftId) return true;
      if (e.conversation_id !== (s.conversationId || "") || typeof e.workspace_root !== "string"
        || !workspaceRootsEqual(e.workspace_root, s.workingDirectory)) return true;
      if (e.skills) {
        s.setAvailableSkills(normalizeSkillList(e.skills, e.workspace_root));
      }
      return true;
    }
    case "commands.list": {
      if (s.activeConversationDraftId) return true;
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
