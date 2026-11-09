import { describe, expect, it, vi } from "vitest";
import type { SlashCommand } from "../stores/types";
import {
  buildRuntimeSlashArgMenuItems,
  buildRuntimeSlashMenuItems,
  buildRuntimeSlashPaletteItems,
  executeRuntimeSlashCommand,
  getActiveRuntimeSlashCommand,
  getRuntimeSlashCommandLine,
  isRuntimeSlashCommandInput,
  isRuntimeSlashSkillPicker,
  parseRuntimeSlashInput,
  resolveRuntimeSlashMenuSelection,
  shouldTokenizeRuntimeSlashCommand,
  syncRuntimeSlashPanelForDraft,
  type RuntimeCommandState,
  type RuntimeSlashCommandDeps,
} from "./runtime-commands";

const makeDeps = (state: RuntimeCommandState = {}) => {
  const deps: RuntimeSlashCommandDeps & {
    state: RuntimeCommandState;
    sentCommands: unknown[];
    setPatches: unknown[];
  } = {
    state,
    sentCommands: [],
    setPatches: [],
    getState: () => deps.state,
    setState: (patch) => deps.setPatches.push(patch),
    sendClientCommand: (command) => {
      deps.sentCommands.push(command);
      return true;
    },
    sendChatMessage: vi.fn(() => true),
    confirmClear: vi.fn(async () => true),
  };
  return deps;
};

describe("executeRuntimeSlashCommand", () => {
  it("forwards slash commands to the authoritative backend command dispatcher", async () => {
    const deps = makeDeps();

    const handled = await executeRuntimeSlashCommand("/plan inspect the queue flow", deps);

    expect(handled).toEqual({ sent: true, reset: "input" });
    expect(deps.sendChatMessage).toHaveBeenCalledWith({
      displayContent: "/plan inspect the queue flow",
      backendContent: "/plan inspect the queue flow",
      skipLocalAppend: true,
    });
    expect(deps.sentCommands).toEqual([]);
  });

  it("uses the client-owned message id path for prompt-template commands", async () => {
    const deps = makeDeps();
    deps.state.slashCommands = [
      { name: "review", command: "review", label: "/review", description: "Review changes", type: "template" },
    ];

    const handled = await executeRuntimeSlashCommand("/review inspect the diff", deps);

    expect(handled).toEqual({ sent: true, reset: "composer" });
    expect(deps.sendChatMessage).toHaveBeenCalledWith({
      displayContent: "/review inspect the diff",
      backendContent: "/review inspect the diff",
      skipLocalAppend: false,
    });
  });

  it("keeps namespaced template commands intact through execution", async () => {
    const deps = makeDeps();
    deps.state.slashCommands = [
      {
        name: "review:security",
        command: "review:security",
        label: "/review:security",
        description: "Review security changes",
        type: "template",
      },
    ];

    await executeRuntimeSlashCommand("/review:security inspect auth", deps);

    expect(deps.sendChatMessage).toHaveBeenCalledWith({
      displayContent: "/review:security inspect auth",
      backendContent: "/review:security inspect auth",
      skipLocalAppend: false,
    });
  });
});

describe("parseRuntimeSlashInput", () => {
  it("extracts inline file, folder, and url mentions from slash rest text", () => {
    expect(parseRuntimeSlashInput("/review check @file:src/app.ts @folder:src/lib @url:https://example.test/doc")).toEqual({
      command: "/review",
      rest: "check",
      commandLine: "/review check",
      mentions: [
        { kind: "file", path: "src/app.ts", name: "app.ts" },
        { kind: "folder", path: "src/lib", name: "lib" },
        { kind: "url", path: "https://example.test/doc", name: "https://example.test/doc" },
      ],
    });
  });

  it("keeps hyphenated slash commands intact", () => {
    expect(parseRuntimeSlashInput("/user-workflow refactor this")?.commandLine).toBe(
      "/user-workflow refactor this",
    );
  });

  it("keeps Claude Code style namespaced and dotted commands intact", () => {
    expect(parseRuntimeSlashInput("/review:security check auth")?.command).toBe(
      "/review:security",
    );
    expect(parseRuntimeSlashInput("/release.notes draft")?.command).toBe(
      "/release.notes",
    );
  });

  it("keeps custom prompt body text in the submission", () => {
    expect(parseRuntimeSlashInput("/prompts:pua inspect the queue\nkeep the draft")).toEqual({
      command: "/prompts:pua",
      rest: "inspect the queue\nkeep the draft",
      commandLine: "/prompts:pua inspect the queue\nkeep the draft",
      mentions: [],
    });
  });

  it("returns null for ordinary chat text and escaped slash text", () => {
    expect(parseRuntimeSlashInput("please /review this")).toBeNull();
    expect(parseRuntimeSlashInput("//not-a-command")).toBeNull();
  });

});

describe("runtime slash command selectors", () => {
  it("recognizes slash command input without treating escaped slash text as a command", () => {
    expect(isRuntimeSlashCommandInput("/review this")).toBe(true);
    expect(isRuntimeSlashCommandInput("/user-workflow this")).toBe(true);
    expect(isRuntimeSlashCommandInput("/review:security this")).toBe(true);
    expect(isRuntimeSlashCommandInput("/release.notes this")).toBe(true);
    expect(isRuntimeSlashCommandInput("//review this")).toBe(false);
    expect(isRuntimeSlashCommandInput("please /review this")).toBe(false);
  });

  it("returns the active slash line only when previous lines are empty", () => {
    expect(getRuntimeSlashCommandLine("\n/review this")).toBe("/review this");
    expect(getRuntimeSlashCommandLine("notes\n/review this")).toBeNull();
    expect(getActiveRuntimeSlashCommand("/user-workflow refactor")).toBe("/user-workflow");
    expect(getActiveRuntimeSlashCommand("/review:security inspect")).toBe("/review:security");
    expect(getActiveRuntimeSlashCommand("/")).toBeNull();
  });

  it("uses only backend command metadata when tokenizing", () => {
    expect(shouldTokenizeRuntimeSlashCommand("/review", [
      { name: "review", command: "review", label: "/review", description: "Review", type: "template" },
    ])).toBe(true);
    expect(shouldTokenizeRuntimeSlashCommand("/review", [
      { name: "review", command: "review", label: "/review", description: "Review", type: "protocol" },
    ])).toBe(false);
    expect(shouldTokenizeRuntimeSlashCommand("/debug", [])).toBe(false);
    expect(shouldTokenizeRuntimeSlashCommand("/usage", [])).toBe(false);
  });
});

describe("syncRuntimeSlashPanelForDraft", () => {
  const makePanelDeps = (slashPanelOpen: boolean, slashCommands: SlashCommand[] = []) => {
    const deps = {
      slashPanelOpen,
      slashCommands,
      openSlashPanel: vi.fn(),
      closeSlashPanel: vi.fn(),
      setMenuFilter: vi.fn(),
      sentCommands: [] as unknown[],
      sendClientCommand: (command: unknown) => {
        deps.sentCommands.push(command);
        return true;
      },
    };
    return deps;
  };

  it.each(["/", "/status", "/prompts:pua"])("opens the slash panel for %s without unrelated capability requests", (draft) => {
    const deps = makePanelDeps(false);

    expect(syncRuntimeSlashPanelForDraft(draft, deps)).toBe(true);

    expect(deps.openSlashPanel).toHaveBeenCalledOnce();
    expect(deps.setMenuFilter).toHaveBeenCalledWith(draft);
    expect(deps.sentCommands).toEqual([]);
  });

  it("keeps the panel filter current while typing a slash command name", () => {
    const deps = makePanelDeps(true);

    expect(syncRuntimeSlashPanelForDraft("/review:sec", deps)).toBe(true);

    expect(deps.openSlashPanel).not.toHaveBeenCalled();
    expect(deps.closeSlashPanel).not.toHaveBeenCalled();
    expect(deps.setMenuFilter).toHaveBeenCalledWith("/review:sec");
  });

  it.each(["/review ", "/review code", "/prompts:pua inspect the queue"])("closes command matching once %s enters its body", (draft) => {
    const deps = makePanelDeps(true, [
      { name: "review", command: "review", label: "/review", description: "Review", type: "template" },
      { name: "prompts:pua", command: "prompts:pua", label: "/prompts:pua", description: "Prompt", type: "template" },
    ]);

    expect(syncRuntimeSlashPanelForDraft(draft, deps)).toBe(false);

    expect(deps.openSlashPanel).not.toHaveBeenCalled();
    expect(deps.closeSlashPanel).toHaveBeenCalledOnce();
    expect(deps.setMenuFilter).toHaveBeenCalledWith("");
    expect(getRuntimeSlashCommandLine(draft)).toBe(draft);
    expect(getActiveRuntimeSlashCommand(draft)).toBe(draft.split(" ")[0]);
  });

  it("keeps a closed menu closed when a complete slash prompt is restored", () => {
    const deps = makePanelDeps(false);

    expect(syncRuntimeSlashPanelForDraft("/prompts:pua keep the existing prompt", deps)).toBe(false);

    expect(deps.openSlashPanel).not.toHaveBeenCalled();
    expect(deps.setMenuFilter).not.toHaveBeenCalled();
  });

  it.each(["/effort ", "/effort hi"])("keeps declared argument completions open for %s", (draft) => {
    const deps = makePanelDeps(false, [{
      name: "effort", command: "effort", label: "/effort", description: "Effort", type: "local",
      args: [{ value: "high", description: "Deeper reasoning" }],
    }]);

    expect(syncRuntimeSlashPanelForDraft(draft, deps)).toBe(true);

    expect(deps.openSlashPanel).toHaveBeenCalledOnce();
    expect(deps.setMenuFilter).toHaveBeenCalledWith(draft);
    expect(deps.sentCommands).toEqual([]);
  });

  it("closes argument completions when no declared argument matches", () => {
    const deps = makePanelDeps(true, [{
      name: "effort", command: "effort", label: "/effort", description: "Effort", type: "local",
      args: [{ value: "high", description: "Deeper reasoning" }],
    }]);

    expect(syncRuntimeSlashPanelForDraft("/effort unknown", deps)).toBe(false);

    expect(deps.closeSlashPanel).toHaveBeenCalledOnce();
    expect(deps.setMenuFilter).toHaveBeenCalledWith("");
  });

  it("requests skills once for the skills slash affordance", () => {
    const deps = makePanelDeps(false);

    expect(syncRuntimeSlashPanelForDraft("/skills", deps)).toBe(true);

    expect(deps.sentCommands).toEqual([{ type: "skills.list" }]);
  });

  it("requests skills for the singular skill picker affordance", () => {
    const deps = makePanelDeps(false);

    expect(syncRuntimeSlashPanelForDraft("/skill", deps)).toBe(true);

    expect(deps.sentCommands).toEqual([{ type: "skills.list" }]);
  });

  it.each(["/skill debugger", "/skills debugger"])("preserves the native skill argument picker for %s", (draft) => {
    const command = draft.split(" ")[0].slice(1);
    const deps = makePanelDeps(false, [{
      name: command, command, label: `/${command}`, description: "Skills", type: "local", source: "builtin",
    }]);

    expect(syncRuntimeSlashPanelForDraft(draft, deps)).toBe(true);

    expect(deps.setMenuFilter).toHaveBeenCalledWith(draft);
    expect(deps.sentCommands).toEqual([{ type: "skills.list" }]);
  });

  it("updates skill filtering without reloading an already open picker", () => {
    const deps = makePanelDeps(true);

    expect(syncRuntimeSlashPanelForDraft("/skill debugger", deps)).toBe(true);

    expect(deps.setMenuFilter).toHaveBeenCalledWith("/skill debugger");
    expect(deps.sentCommands).toEqual([]);
  });

  it("does not request skills or retain its picker for a template command named skill", () => {
    const commands: SlashCommand[] = [{
      name: "skill", command: "skill", label: "/skill", description: "Custom prompt", type: "template", source: "project",
    }];
    const commandDeps = makePanelDeps(false, commands);
    expect(syncRuntimeSlashPanelForDraft("/skill", commandDeps)).toBe(true);
    expect(commandDeps.sentCommands).toEqual([]);

    const bodyDeps = makePanelDeps(true, commands);
    expect(syncRuntimeSlashPanelForDraft("/skill keep the body", bodyDeps)).toBe(false);
    expect(bodyDeps.closeSlashPanel).toHaveBeenCalledOnce();
  });

  it("closes an open slash panel when the draft is no longer a slash line", () => {
    const deps = makePanelDeps(true);

    expect(syncRuntimeSlashPanelForDraft("ordinary message", deps)).toBe(false);

    expect(deps.closeSlashPanel).toHaveBeenCalledOnce();
    expect(deps.setMenuFilter).toHaveBeenCalledWith("");
  });
});

describe("isRuntimeSlashSkillPicker", () => {
  it.each(["/skill ", "/skills debugger", "/SKILL debugger"])("recognizes the legacy argument stage for %s", (value) => {
    expect(isRuntimeSlashSkillPicker(value, [])).toBe(true);
  });

  it.each(["/skill", "/skills", "/skillful ", "/review skill"])("does not classify %s as a skill argument picker", (value) => {
    expect(isRuntimeSlashSkillPicker(value, [])).toBe(false);
  });

  it.each([
    { type: "local", source: "builtin", expected: true },
    { type: "local", source: undefined, expected: true },
    { type: "local", source: "extension", expected: false },
    { type: "template", source: "builtin", expected: false },
    { type: "template", source: "project", expected: false },
    { type: "protocol", source: "builtin", expected: false },
  ] as const)("respects $type/$source catalog ownership", ({ type, source, expected }) => {
    expect(isRuntimeSlashSkillPicker("/skill debugger", [{
      name: "skill", command: "skill", label: "/skill", description: "Skill", type, source,
    }])).toBe(expected);
  });
});

describe("buildRuntimeSlashMenuItems", () => {
  it("does not invent commands when backend metadata is unavailable", () => {
    const items = buildRuntimeSlashMenuItems([]);
    expect(items).toEqual([]);
  });

  it("normalizes backend command labels without local metadata overrides", () => {
    const items = buildRuntimeSlashMenuItems([
      { name: "usage", command: "usage", label: "usage", description: "", type: "protocol" },
      { name: "custom", command: "custom", label: "/custom", description: "Custom command", type: "protocol" },
    ]);

    expect(items).toMatchObject([
      { name: "/usage", description: "", section: "Commands" },
      { name: "/custom", description: "Custom command", section: "Commands" },
    ]);
  });

  it("deduplicates command names while preserving backend order", () => {
    const items = buildRuntimeSlashMenuItems([
      { name: "custom-z", command: "custom-z", label: "/custom-z", description: "Z", type: "protocol" },
      { name: "status", command: "status", label: "/status", description: "Status", type: "protocol" },
      { name: "status-duplicate", command: "status", label: "/status", description: "Duplicate", type: "protocol" },
      { name: "custom-a", command: "custom-a", label: "/custom-a", description: "A", type: "protocol" },
    ]);

    expect(items.map((item) => item.name)).toEqual(["/custom-z", "/status", "/custom-a"]);
    expect(items.find((item) => item.name === "/status")?.description).toBe("Status");
  });

  it("labels extension commands as MiniCode-owned", () => {
    const items = buildRuntimeSlashMenuItems([
      {
        name: "inspect",
        command: "inspect",
        label: "/inspect",
        description: "Inspect extension state",
        type: "template",
        source: "extension",
      },
    ]);

    expect(items[0].description).toContain("MiniCode extension");
    expect(items[0].description).not.toContain("Pi extension");
  });
});

describe("buildRuntimeSlashArgMenuItems", () => {
  const effortCommand = {
    name: "effort",
    command: "effort",
    label: "/effort",
    description: "Set reasoning effort",
    type: "local" as const,
    args: [
      { value: "low", description: "最快" },
      { value: "medium", description: "平衡" },
      { value: "high", description: "更深入" },
      { value: "max", description: "最强" },
    ],
  };

  it("shows the bare command plus all argument completions for an exact command", () => {
    const items = buildRuntimeSlashArgMenuItems("/effort", [effortCommand]);

    expect(items?.map((item) => item.name)).toEqual([
      "/effort",
      "/effort low",
      "/effort medium",
      "/effort high",
      "/effort max",
    ]);
    expect(items?.[1].description).toBe("最快");
  });

  it("filters argument completions by the partial argument text", () => {
    const items = buildRuntimeSlashArgMenuItems("/effort m", [effortCommand]);

    expect(items?.map((item) => item.name)).toEqual(["/effort medium", "/effort max"]);
  });

  it("returns null for command-name prefixes, unknown commands, and commands without args", () => {
    expect(buildRuntimeSlashArgMenuItems("/eff", [effortCommand])).toBeNull();
    expect(buildRuntimeSlashArgMenuItems("/unknown", [effortCommand])).toBeNull();
    expect(buildRuntimeSlashArgMenuItems("/usage", [
      { name: "usage", command: "usage", label: "/usage", description: "Usage", type: "local" },
    ])).toBeNull();
  });

  it("returns null when the partial argument matches nothing", () => {
    expect(buildRuntimeSlashArgMenuItems("/effort zzz", [effortCommand])).toBeNull();
  });
});

describe("buildRuntimeSlashPaletteItems", () => {
  it("builds command-palette runnable slash entries from backend metadata", () => {
    const items = buildRuntimeSlashPaletteItems([
      { name: "status", command: "status", label: "/status", description: "Show runtime status", type: "local" },
      { name: "tasks", command: "tasks", label: "tasks", description: "Show running tasks", type: "local" },
    ]);

    expect(items).toMatchObject([
      {
        id: "slash.status",
        name: "/status",
        description: "Show runtime status",
        commandLine: "/status",
        section: "Commands",
      },
      {
        id: "slash.tasks",
        name: "/tasks",
        description: "Show running tasks",
        commandLine: "/tasks",
        section: "Commands",
      },
    ]);
  });

  it("can exclude commands that already have first-class palette actions", () => {
    const items = buildRuntimeSlashPaletteItems([
      { name: "status", command: "status", label: "/status", description: "Status", type: "local" },
      { name: "clear", command: "clear", label: "/clear", description: "Clear", type: "local" },
      { name: "skills", command: "skills", label: "/skills", description: "Skills", type: "local" },
      { name: "compact", command: "compact", label: "/compact", description: "Compact", type: "local" },
      { name: "new", command: "new", label: "/new", description: "New", type: "local" },
    ], {
      exclude: ["clear", "/skills", "compact", "new"],
    });

    expect(items.some((item) => item.name === "/clear")).toBe(false);
    expect(items.some((item) => item.name === "/skills")).toBe(false);
    expect(items.some((item) => item.name === "/compact")).toBe(false);
    expect(items.some((item) => item.name === "/new")).toBe(false);
    expect(items.some((item) => item.name === "/status")).toBe(true);
  });
});

describe("resolveRuntimeSlashMenuSelection", () => {
  it("normalizes empty selections into a close action", () => {
    expect(resolveRuntimeSlashMenuSelection("", {})).toEqual({ kind: "close" });
  });

  it("opens the second-level skill picker for the singular command", () => {
    expect(resolveRuntimeSlashMenuSelection("/skill", {})).toEqual({ kind: "skill_picker" });
  });

  it("resolves exact skill slash menu selections without treating them as backend commands", () => {
    expect(resolveRuntimeSlashMenuSelection("/debugger", {
      availableSkills: [{ name: "debugger", description: "Debugging", source_level: "user" }],
      slashCommands: [],
    })).toEqual({
      kind: "skill",
      skill: {
        name: "debugger",
        description: "Debugging",
        sourceLevel: "user",
      },
    });
  });

  it("resolves template slash commands into composer tokens", () => {
    expect(resolveRuntimeSlashMenuSelection("/review", {
      slashCommands: [
        { name: "review", command: "review", label: "/review", description: "Review", type: "template" },
      ],
    })).toEqual({ kind: "tokenize", command: "/review" });
  });

  it("preserves an actual prompt command when a skill has the same name", () => {
    expect(resolveRuntimeSlashMenuSelection("/pua", {
      availableSkills: [{ name: "pua", description: "Skill content", source_level: "user" }],
      slashCommands: [
        { name: "pua", command: "pua", label: "/pua", description: "Prompt content", type: "template" },
      ],
    })).toEqual({ kind: "tokenize", command: "/pua" });
  });

  it("preserves an actual local command when a skill has the same name", () => {
    expect(resolveRuntimeSlashMenuSelection("/status", {
      availableSkills: [{ name: "status", description: "Skill content", source_level: "user" }],
      slashCommands: [
        { name: "status", command: "status", label: "/status", description: "Runtime status", type: "local", source: "builtin" },
      ],
    })).toEqual({ kind: "execute", commandLine: "/status" });
  });

  it.each(["skill-path:C%3A%2Fskills%2Fpua%2FSKILL.md", "skill-name:pua"])("preserves explicit skill selection %s despite a same-name command", (value) => {
    expect(resolveRuntimeSlashMenuSelection(value, {
      availableSkills: [{ name: "pua", path: "C:/skills/pua/SKILL.md", description: "Skill content", source_level: "user" }],
      slashCommands: [
        { name: "pua", command: "pua", label: "/pua", description: "Prompt content", type: "template" },
      ],
    })).toEqual({
      kind: "skill",
      skill: { name: "pua", path: "C:/skills/pua/SKILL.md", description: "Skill content", sourceLevel: "user" },
    });
  });

  it("keeps the native skill picker when a skill has its reserved name", () => {
    expect(resolveRuntimeSlashMenuSelection("/skill", {
      availableSkills: [{ name: "skill", description: "Skill content", source_level: "user" }],
      slashCommands: [
        { name: "skill", command: "skill", label: "/skill", description: "Choose skill", type: "local", source: "builtin" },
      ],
    })).toEqual({ kind: "skill_picker" });
  });

  it("does not turn a template named skill into the legacy picker", () => {
    expect(resolveRuntimeSlashMenuSelection("/skill", {
      slashCommands: [
        { name: "skill", command: "skill", label: "/skill", description: "Custom prompt", type: "template", source: "project" },
      ],
    })).toEqual({ kind: "tokenize", command: "/skill" });
  });

  it("does not turn a non-builtin local command named skill into the legacy picker", () => {
    expect(resolveRuntimeSlashMenuSelection("/skill", {
      slashCommands: [
        { name: "skill", command: "skill", label: "/skill", description: "Extension action", type: "local", source: "extension" },
      ],
    })).toEqual({ kind: "execute", commandLine: "/skill" });
  });

  it("resolves namespaced template commands without truncating the namespace", () => {
    expect(resolveRuntimeSlashMenuSelection("/review:security", {
      slashCommands: [
        {
          name: "review:security",
          command: "review:security",
          label: "/review:security",
          description: "Security review",
          type: "template",
        },
      ],
    })).toEqual({ kind: "tokenize", command: "/review:security" });
  });

  it("resolves protocol slash commands into runtime execution", () => {
    expect(resolveRuntimeSlashMenuSelection("/usage", {
      slashCommands: [
        { name: "usage", command: "usage", label: "/usage", description: "Usage", type: "protocol" },
      ],
    })).toEqual({ kind: "execute", commandLine: "/usage" });
  });
});
