import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { normalizeSkillList, normalizeSlashCommands } from "../lib/catalog-normalizers";

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
      const ev = e as unknown as {
        commands?: {
          name: string;
          command: string;
          label: string;
          description: string;
          type: string;
          enabled?: boolean;
          args?: { value: string; description: string }[];
        }[];
      };
      if (ev.commands) {
        s.setSlashCommands(normalizeSlashCommands(ev.commands));
      }
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
