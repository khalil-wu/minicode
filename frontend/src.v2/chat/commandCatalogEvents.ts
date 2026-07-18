import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { pushToast } from "../overlays/ToastContainer";
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
    case "skill_activated": {
      const ev = e as unknown as { skill_name?: string; trigger_mode?: string; data?: { skill_name?: string; trigger_mode?: string } };
      const name = ev.skill_name ?? ev.data?.skill_name;
      const triggerMode = ev.trigger_mode ?? ev.data?.trigger_mode;
      if (name) {
        const skill = useAppStore.getState().availableSkills.find((item) => item.name === name);
        if (!triggerMode || triggerMode === "explicit") {
          useAppStore.getState().addSelectedSkill({
            name,
            description: skill?.description,
            sourceLevel: skill?.source_level,
          });
          pushToast(`Skill active: ${name}`, "success");
        }
      }
      return true;
    }
    case "skill_deactivated": {
      const ev = e as unknown as { skill_name?: string; data?: { skill_name?: string } };
      const name = ev.skill_name ?? ev.data?.skill_name;
      if (name) {
        useAppStore.getState().removeSelectedSkill(name);
        pushToast(`Skill inactive: ${name}`, "info");
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
