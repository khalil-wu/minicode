import { beforeEach, describe, expect, it, vi } from "vitest";
import { handleCommandCatalogEvent } from "./commandCatalogEvents";
import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";

const commandEntry = (overrides: Record<string, unknown> = {}) => ({
  id: "extension:review",
  name: "review",
  command: "review",
  label: "/review",
  description: "Review the current workspace changes.",
  type: "template",
  kind: "prompt",
  source: "extension",
  enabled: true,
  availability: { kind: "available", scope: "conversation", reason: "Loaded from extension." },
  args: [{ value: "security", description: "Focus on security." }],
  extension_path: "C:\\extensions\\review",
  source_path: "C:\\extensions\\review\\commands\\review.md",
  template: "Review $ARGUMENTS",
  search_text: "review security",
  argument_hint: "[focus]",
  argument_names: ["focus"],
  base_dir: "C:\\extensions\\review",
  is_skill_file: false,
  ...overrides,
});

describe("handleCommandCatalogEvent", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAppStore.setState({
      conversationId: "conv-active",
      availableSkills: [{ name: "frontend-dev", description: "Frontend workflow", source_level: "project", active: false }],
      selectedSkills: [],
      slashCommands: [{
        name: "stale",
        command: "stale",
        label: "/stale",
        description: "Old catalog",
        type: "local",
      }],
      inspectorEntries: [],
      inspectorFocus: null,
    });
  });

  it("normalizes skill metadata from skills.list", () => {
    expect(handleCommandCatalogEvent({
      type: "skills.list",
      skills: [{
        name: "openai-docs",
        description: "Use official docs",
        display_name: "OpenAI Docs",
        icon: "book-open",
        triggers: ["codex"],
        mcp_dependencies: ["docs"],
        allow_implicit_invocation: false,
        default_prompt: "Prefer official docs.",
        source_level: "builtin",
        active: true,
      }],
    } as ServerEvent)).toBe(true);

    expect(useAppStore.getState().availableSkills).toEqual([{
      name: "openai-docs",
      description: "Use official docs",
      path: undefined,
      display_name: "OpenAI Docs",
      short_description: undefined,
      icon: undefined,
      icon_large: undefined,
      brand_color: undefined,
      version: undefined,
      mcp_dependencies: ["docs"],
      allow_implicit_invocation: false,
      default_prompt: "Prefer official docs.",
      source_level: "builtin",
      active: true,
    }]);
  });

  it("ignores removed runtime skill activation events", () => {
    expect(handleCommandCatalogEvent({
      type: "skill_activated",
      skill_name: "frontend-dev",
      trigger_mode: "implicit",
    } as ServerEvent)).toBe(false);

    expect(useAppStore.getState().selectedSkills).toEqual([]);
  });

  it("applies an active owner catalog without losing executable metadata", () => {
    expect(handleCommandCatalogEvent({
      type: "commands.list",
      conversation_id: "conv-active",
      request_id: "commands-request-1",
      commands: [commandEntry()],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().slashCommands).toEqual([
      {
        id: "extension:review",
        name: "review",
        command: "review",
        label: "/review",
        description: "Review the current workspace changes.",
        type: "template",
        kind: "prompt",
        source: "extension",
        availability: { kind: "available", scope: "conversation", reason: "Loaded from extension." },
        panel: undefined,
        args: [{ value: "security", description: "Focus on security." }],
        extensionPath: "C:\\extensions\\review",
        sourcePath: "C:\\extensions\\review\\commands\\review.md",
        template: "Review $ARGUMENTS",
        searchText: "review security",
        argumentHint: "[focus]",
        argumentNames: ["focus"],
        baseDir: "C:\\extensions\\review",
        isSkillFile: false,
      },
    ]);
    expect(useAppStore.getState().inspectorEntries).toEqual([
      expect.objectContaining({
        targetKind: "session",
        targetId: "commands:conv-active",
        payload: expect.objectContaining({
          conversation_id: "conv-active",
          request_id: "commands-request-1",
          count: 1,
          sources: { extension: 1 },
          commands: [expect.objectContaining({
            name: "review",
            source: "extension",
            availability: { kind: "available", scope: "conversation", reason: "Loaded from extension." },
            extension_path: "C:\\extensions\\review",
            source_path: "C:\\extensions\\review\\commands\\review.md",
          })],
        }),
      }),
    ]);
  });

  it("ignores a late catalog owned by another conversation", () => {
    const before = useAppStore.getState().slashCommands;
    expect(handleCommandCatalogEvent({
      type: "commands.list",
      conversation_id: "conv-stale",
      commands: [commandEntry({ command: "wrong", name: "wrong", label: "/wrong" })],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().slashCommands).toEqual(before);
    expect(useAppStore.getState().inspectorEntries).toEqual([]);
  });

  it("accepts the explicit session catalog only before an active conversation exists", () => {
    useAppStore.setState({ conversationId: null, slashCommands: [], inspectorEntries: [] });
    expect(handleCommandCatalogEvent({
      type: "commands.list",
      conversation_id: null,
      commands: [commandEntry({ source: "builtin" })],
    } as unknown as ServerEvent)).toBe(true);
    expect(useAppStore.getState().slashCommands[0]).toMatchObject({ command: "review", source: "builtin" });

    useAppStore.setState({ conversationId: "conv-active", slashCommands: [] });
    expect(handleCommandCatalogEvent({
      type: "commands.list",
      conversation_id: null,
      commands: [commandEntry()],
    } as unknown as ServerEvent)).toBe(true);
    expect(useAppStore.getState().slashCommands).toEqual([]);
  });

  it("preserves extension or project precedence over a builtin command with the same name", () => {
    expect(handleCommandCatalogEvent({
      type: "commands.list",
      conversation_id: "conv-active",
      commands: [
        commandEntry({ source: "project", source_path: "C:\\repo\\.minicode\\commands\\review.md" }),
        commandEntry({ id: "builtin:review", source: "builtin", template: "Builtin review" }),
      ],
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().slashCommands).toHaveLength(1);
    expect(useAppStore.getState().slashCommands[0]).toMatchObject({
      id: "extension:review",
      command: "review",
      source: "project",
      sourcePath: "C:\\repo\\.minicode\\commands\\review.md",
      template: "Review $ARGUMENTS",
    });
  });
});
