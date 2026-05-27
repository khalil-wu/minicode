import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";
import { pushToast } from "../overlays/ToastContainer";

export const handleCommandCatalogEvent = (e: ServerEvent): boolean => {
  const s = useAppStore.getState();
  switch (e.type) {
    case "skills.list": {
      if (e.skills) {
        s.setAvailableSkills(e.skills.map((skill) => ({
          ...skill,
          active: Boolean((skill as typeof skill & { active?: boolean }).active),
        })));
      }
      return true;
    }
    case "skill_activated": {
      const ev = e as unknown as { skill_name?: string; data?: { skill_name?: string } };
      const name = ev.skill_name ?? ev.data?.skill_name;
      if (name) {
        const skill = useAppStore.getState().availableSkills.find((item) => item.name === name);
        useAppStore.getState().addSelectedSkill({
          name,
          description: skill?.description,
          sourceLevel: skill?.source_level,
        });
        pushToast(`Skill active: ${name}`, "success");
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
        }[];
      };
      if (ev.commands) {
        s.setSlashCommands(ev.commands.filter((c) => c.enabled !== false).map((c) => ({
          name: c.name,
          command: c.command ?? c.name,
          label: c.label ?? `/${c.name}`,
          description: c.description ?? "",
          type: (c.type as "local" | "template" | "protocol") ?? "local",
        })));
      }
      return true;
    }
    case "skills.marketplace.list": {
      if (e.skills) {
        s.setMarketplaceSkills(e.skills.map((sk) => ({
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
