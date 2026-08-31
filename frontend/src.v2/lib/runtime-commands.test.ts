import { describe, expect, it, vi } from "vitest";
import {
  buildRuntimeSlashArgMenuItems,
  buildRuntimeSlashMenuItems,
  buildRuntimeSlashPaletteItems,
  executeRuntimeSlashCommand,
  getActiveRuntimeSlashCommand,
  getRuntimeSlashCommandLine,
  isRuntimeSlashCommandInput,
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

    expect(handled).toEqual({ sent: true, reset: "input" });
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
  const makePanelDeps = (slashPanelOpen: boolean) => {
    const deps = {
      slashPanelOpen,
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

  it("opens the slash panel without issuing unrelated capability requests", () => {
    const deps = makePanelDeps(false);

    expect(syncRuntimeSlashPanelForDraft("/status", deps)).toBe(true);

    expect(deps.openSlashPanel).toHaveBeenCalledOnce();
    expect(deps.setMenuFilter).toHaveBeenCalledWith("/status");
    expect(deps.sentCommands).toEqual([]);
  });

  it("keeps the panel filter current while typing a slash command", () => {
    const deps = makePanelDeps(true);

    expect(syncRuntimeSlashPanelForDraft("/review code", deps)).toBe(true);

    expect(deps.openSlashPanel).not.toHaveBeenCalled();
    expect(deps.closeSlashPanel).not.toHaveBeenCalled();
    expect(deps.setMenuFilter).toHaveBeenCalledWith("/review code");
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

  it("closes an open slash panel when the draft is no longer a slash line", () => {
    const deps = makePanelDeps(true);

    expect(syncRuntimeSlashPanelForDraft("ordinary message", deps)).toBe(false);

    expect(deps.closeSlashPanel).toHaveBeenCalledOnce();
    expect(deps.setMenuFilter).toHaveBeenCalledWith("");
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
