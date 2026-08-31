import { beforeEach, describe, expect, it } from "vitest";
import { handleControlEvent } from "./controlEvents";
import { useAppStore } from "../stores";
import type { ServerEvent } from "../protocol/events";

describe("handleControlEvent", () => {
  beforeEach(() => {
    useAppStore.setState({
      pendingApproval: null,
      approvalQueue: [],
      pendingDiffReview: null,
      diffReviewQueue: [],
      diffReview: null,
      pendingAskUser: null,
      askUserQueue: [],
    });
  });

  it("clears cancelled approval, diff, and ask-user state", () => {
    useAppStore.setState({
      pendingApproval: {
        requestId: "approval-1",
        conversationId: "conv-a",
        toolName: "run_command",
        args: {},
      },
      approvalQueue: [{
        requestId: "approval-2",
        conversationId: "conv-a",
        toolName: "read_file",
        args: {},
      }],
      pendingDiffReview: { requestId: "diff-1", conversationId: "conv-a", diff: "patch" },
      diffReview: {
        requestId: "diff-1",
        conversationId: "conv-a",
        toolName: "write_file",
        diff: "patch",
        files: [],
        status: "pending",
        fileDecisions: {},
      },
      pendingAskUser: { requestId: "ask-1", conversationId: "conv-a", question: "Continue?" },
    });

    expect(handleControlEvent({
      type: "approval.cancelled",
      conversation_id: "conv-a",
      request_ids: ["approval-1", "diff-1", "ask-1"],
      reason: "user_interrupted",
    } as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(state.pendingApproval?.requestId).toBe("approval-2");
    expect(state.approvalQueue).toEqual([]);
    expect(state.pendingDiffReview).toBeNull();
    expect(state.diffReview).toBeNull();
    expect(state.pendingAskUser).toBeNull();
  });

  it("ignores an unowned legacy cancellation instead of clearing every prompt", () => {
    useAppStore.setState({
      pendingApproval: {
        requestId: "approval-current",
        conversationId: "conv-current",
        toolName: "write_file",
        args: {},
      },
      pendingAskUser: {
        requestId: "ask-current",
        conversationId: "conv-current",
        question: "Continue?",
      },
    });

    expect(handleControlEvent({
      type: "approval.cancelled",
      request_ids: ["approval-current", "ask-current"],
      reason: "legacy_replay",
    } as ServerEvent)).toBe(true);

    expect(useAppStore.getState().pendingApproval?.requestId).toBe("approval-current");
    expect(useAppStore.getState().pendingAskUser?.requestId).toBe("ask-current");
  });

  it("keeps prompts from other conversations even when request ids are listed", () => {
    useAppStore.setState({
      pendingApproval: {
        requestId: "approval-a",
        conversationId: "conv-a",
        toolName: "write_file",
        args: {},
      },
      approvalQueue: [{
        requestId: "approval-b",
        conversationId: "conv-b",
        toolName: "read_file",
        args: {},
      }],
    });

    expect(handleControlEvent({
      type: "approval.cancelled",
      conversation_id: "conv-a",
      request_ids: ["approval-a", "approval-b"],
      reason: "user_interrupted",
    } as ServerEvent)).toBe(true);

    expect(useAppStore.getState().pendingApproval).toMatchObject({
      requestId: "approval-b",
      conversationId: "conv-b",
    });
    expect(useAppStore.getState().approvalQueue).toEqual([]);
  });

  it("stores inactive approval and ask-user prompts without displaying them in the active conversation", () => {
    useAppStore.setState({ conversationId: "conv-active" });

    expect(handleControlEvent({
      type: "control_request",
      request_id: "approval-other",
      conversation_id: "conv-other",
      request: {
        subtype: "can_use_tool",
        tool_name: "write_file",
        input: {},
      },
    } as unknown as ServerEvent)).toBe(true);
    expect(handleControlEvent({
      type: "control_request",
      request_id: "ask-other",
      conversation_id: "conv-other",
      request: {
        subtype: "elicitation",
        question: "Continue other conversation?",
      },
    } as unknown as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(state.pendingApproval).toMatchObject({
      requestId: "approval-other",
      conversationId: "conv-other",
      toolName: "write_file",
    });
    expect(state.pendingAskUser).toMatchObject({
      requestId: "ask-other",
      conversationId: "conv-other",
    });
  });

  it("does not guess the active conversation for unowned blocking prompts", () => {
    useAppStore.setState({ conversationId: "conv-active" });

    expect(handleControlEvent({
      type: "control_request",
      request_id: "approval-legacy",
      request: {
        subtype: "can_use_tool",
        tool_name: "write_file",
        input: { path: "demo.txt" },
      },
    } as unknown as ServerEvent)).toBe(true);
    expect(handleControlEvent({
      type: "control_request",
      request_id: "ask-legacy",
      request: {
        subtype: "elicitation",
        question: "Continue?",
      },
    } as unknown as ServerEvent)).toBe(true);
    expect(handleControlEvent({
      type: "approval.file_diff",
      tool_call_id: "diff-legacy",
      path: "demo.txt",
      patch: "+unsafe",
      is_large: false,
      is_truncated: false,
    } as unknown as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(state.pendingApproval).toBeNull();
    expect(state.pendingAskUser).toBeNull();
    expect(state.pendingDiffReview).toBeNull();
    expect(state.diffReview).toBeNull();
  });

  it("does not replace the active diff panel when another conversation asks for review", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      diffReview: {
        requestId: "active-diff",
        conversationId: "conv-active",
        toolName: "write_file",
        diff: "+active",
        files: [],
        status: "pending",
        mode: "approval",
        fileDecisions: {},
        lineComments: [],
      },
    });

    expect(handleControlEvent({
      type: "control_request",
      request_id: "background-diff",
      conversation_id: "conv-other",
      request: {
        subtype: "can_use_tool",
        tool_name: "write_file",
        input: { path: "other.txt" },
        diff: "+background",
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().pendingDiffReview).toMatchObject({
      requestId: "background-diff",
      conversationId: "conv-other",
    });
    expect(useAppStore.getState().diffReview?.requestId).toBe("active-diff");
  });

  it("routes lazy approval file diffs to the owning queued review", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      pendingDiffReview: {
        requestId: "diff-active",
        conversationId: "conv-active",
        diff: "+active",
      },
      diffReviewQueue: [{
        requestId: "diff-other",
        conversationId: "conv-other",
        diff: "",
        reviewState: {
          requestId: "diff-other",
          conversationId: "conv-other",
          toolName: "write_file",
          diff: "",
          files: [{ path: "other.txt", isLarge: true }],
          selectedPath: "other.txt",
          status: "pending",
          mode: "approval",
          fileDecisions: {},
          lineComments: [],
        },
      }],
      diffReview: {
        requestId: "diff-active",
        conversationId: "conv-active",
        toolName: "write_file",
        diff: "+active",
        files: [],
        status: "pending",
        mode: "approval",
        fileDecisions: {},
        lineComments: [],
      },
    });

    expect(handleControlEvent({
      type: "approval.file_diff",
      tool_call_id: "diff-other",
      conversation_id: "conv-active",
      path: "other.txt",
      patch: "@@ -1 +1 @@\n-old\n+new",
      is_large: true,
      is_truncated: false,
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().diffReviewQueue[0]?.diff).toBe("");

    expect(handleControlEvent({
      type: "approval.file_diff",
      tool_call_id: "diff-other",
      conversation_id: "conv-other",
      path: "other.txt",
      patch: "@@ -1 +1 @@\n-old\n+new",
      is_large: true,
      is_truncated: false,
    } as unknown as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(state.diffReview?.requestId).toBe("diff-active");
    expect(state.diffReviewQueue[0]?.diff).toContain("+new");
    expect(state.diffReviewQueue[0]?.reviewState?.files[0]).toMatchObject({
      path: "other.txt",
      patch: "@@ -1 +1 @@\n-old\n+new",
      isLarge: true,
      isTruncated: false,
    });
  });

  it("maps active control-protocol tool approval requests into pending approval state", () => {
    useAppStore.setState({ conversationId: "conv-active" });

    expect(handleControlEvent({
      type: "control_request",
      request_id: "ctrl-approval",
      conversation_id: "conv-active",
      request: {
        subtype: "can_use_tool",
        tool_name: "write_file",
        input: { path: "demo.txt" },
        tool_use_id: "ctrl-approval",
        source_agent: "reviewer",
        source_thread: "conv-active",
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().pendingApproval).toMatchObject({
      requestId: "ctrl-approval",

      conversationId: "conv-active",
      toolName: "write_file",
      args: { path: "demo.txt" },
      sourceAgent: "reviewer",
      sourceThread: "conv-active",
    });
  });

  it("maps control-protocol tool approvals with diff into composer diff review state", () => {
    useAppStore.setState({
      conversationId: "conv-active",
      panelSlots: [],
      rightPanelOpen: false,
    });

    expect(handleControlEvent({
      type: "control_request",
      request_id: "ctrl-diff",
      conversation_id: "conv-active",
      request: {
        subtype: "can_use_tool",
        tool_name: "write_file",
        input: { path: "demo.txt" },
        tool_use_id: "ctrl-diff",
        diff: {
          format: "structured",
          files: [{
            path: "demo.txt",
            patch: "@@ -1 +1 @@\n-old\n+new",
            additions: 1,
            deletions: 1,
          }],
        },
      },
    } as unknown as ServerEvent)).toBe(true);

    const state = useAppStore.getState();
    expect(state.pendingApproval).toBeNull();
    expect(state.pendingDiffReview).toMatchObject({
      requestId: "ctrl-diff",

      conversationId: "conv-active",
      diff: "@@ -1 +1 @@\n-old\n+new",
      filePath: "demo.txt",
    });
    expect(state.diffReview).toMatchObject({
      requestId: "ctrl-diff",

      conversationId: "conv-active",
      toolName: "write_file",
      selectedPath: "demo.txt",
      files: [expect.objectContaining({ path: "demo.txt", additions: 1, deletions: 1 })],
    });
    expect(state.panelSlots.some((slot) => slot.id === "approval-diff")).toBe(false);
    expect(state.rightPanelOpen).toBe(false);
  });

  it("maps active control-protocol elicitation requests into ask-user state", () => {
    useAppStore.setState({ conversationId: "conv-active" });

    expect(handleControlEvent({
      type: "control_request",
      request_id: "ctrl-ask",
      conversation_id: "conv-active",
      request: {
        subtype: "elicitation",
        prompt: "The MCP server needs a deployment target before it can continue.",
        question: "Which deployment target should be used?",
        tool_use_id: "ctrl-ask",
        schema: { type: "string" },
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().pendingAskUser).toMatchObject({
      requestId: "ctrl-ask",

      conversationId: "conv-active",
      prompt: "The MCP server needs a deployment target before it can continue.",
      question: "Which deployment target should be used?",
      inputSchema: { type: "string" },
    });
  });

  it("maps MiniCode provider OAuth prompts into the existing ask-user control", () => {
    useAppStore.setState({ conversationId: "conv-active" });

    expect(handleControlEvent({
      type: "control_request",
      request_id: "oauth-prompt",
      conversation_id: "conv-active",
      request: {
        subtype: "provider_auth_prompt",
        prompt: "Enter the verification code",
        provider: "github-copilot",
        prompt_type: "manual_code",
        placeholder: "ABCD-1234",
        allow_empty: false,
        allow_custom: true,
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().pendingAskUser).toMatchObject({
      requestId: "oauth-prompt",

      conversationId: "conv-active",
      question: "Enter the verification code",
      provider: "github-copilot",
      promptType: "manual_code",
      placeholder: "ABCD-1234",
      allowEmpty: false,
      allowCustom: true,
      secret: false,
    });
  });

  it("preserves provider OAuth secret/select semantics and real option ids", () => {
    useAppStore.setState({ conversationId: "conv-active" });

    expect(handleControlEvent({
      type: "control_request",
      request_id: "oauth-secret",
      conversation_id: "conv-active",
      expires_at: 4_102_444_800_000,
      request: {
        subtype: "provider_auth_prompt",
        prompt: "Enter the API key exactly",
        provider: "provider-one",
        prompt_type: "secret",
        placeholder: "sk-...",
        allow_empty: true,
        allow_custom: true,
      },
    } as unknown as ServerEvent)).toBe(true);
    expect(useAppStore.getState().pendingAskUser).toMatchObject({
      requestId: "oauth-secret",
      promptType: "secret",
      placeholder: "sk-...",
      allowEmpty: true,
      allowCustom: true,
      secret: true,
      expiresAt: 4_102_444_800_000,
    });

    useAppStore.getState().clearAskUser("oauth-secret");
    expect(handleControlEvent({
      type: "control_request",
      request_id: "oauth-select",
      conversation_id: "conv-active",
      request: {
        subtype: "provider_auth_prompt",
        prompt: "Choose a login method",
        provider: "openai",
        prompt_type: "select",
        allow_empty: false,
        allow_custom: false,
        options: [
          { id: "browser", label: "Browser login", description: "Use a callback page" },
          { id: "device_code", label: "Device code login" },
        ],
      },
    } as unknown as ServerEvent)).toBe(true);
    expect(useAppStore.getState().pendingAskUser).toMatchObject({
      requestId: "oauth-select",
      promptType: "select",
      allowEmpty: false,
      allowCustom: false,
      secret: false,
      options: [
        { label: "Browser login", value: "browser", description: "Use a callback page" },
        { label: "Device code login", value: "device_code" },
      ],
    });
  });

  it("stores conversation scope on active prompts", () => {
    useAppStore.setState({ conversationId: "conv-active" });

    expect(handleControlEvent({
      type: "control_request",
      request_id: "ask-active",
      conversation_id: "conv-active",
      request: {
        subtype: "elicitation",
        question: "Continue active conversation?",
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().pendingAskUser).toMatchObject({
      requestId: "ask-active",
      conversationId: "conv-active",
      question: "Continue active conversation?",
    });
  });

  it("maps control-protocol elicitation choices into ask-user options", () => {
    useAppStore.setState({ conversationId: "conv-active" });

    expect(handleControlEvent({
      type: "control_request",
      request_id: "ctrl-choice",
      conversation_id: "conv-active",
      request: {
        subtype: "elicitation",
        tool_use_id: "ctrl-choice",
        prompt: "Select the implementation language.",
        question: "Pick one",
        choices: [
          { label: "TypeScript", value: "ts" },
          { title: "Python" },
          "Rust",
        ],
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().pendingAskUser).toMatchObject({
      requestId: "ctrl-choice",
      options: [
        { label: "TypeScript", value: "ts" },
        { label: "Python", value: "Python" },
        { label: "Rust", value: "Rust" },
      ],
    });
  });

  it("stores inactive control-protocol tool approvals and questions for later restore", () => {
    useAppStore.setState({ conversationId: "conv-active" });

    expect(handleControlEvent({
      type: "control_request",
      request_id: "ctrl-other",
      conversation_id: "conv-other",
      request: {
        subtype: "can_use_tool",
        tool_name: "write_file",
        input: {},
        tool_use_id: "ctrl-other",
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(handleControlEvent({
      type: "control_request",
      request_id: "ctrl-ask-other",
      conversation_id: "conv-other",
      request: {
        subtype: "elicitation",
        tool_use_id: "ctrl-ask-other",
        prompt: "The background conversation needs a choice.",
        question: "Other question",
      },
    } as unknown as ServerEvent)).toBe(true);

    expect(useAppStore.getState().pendingApproval).toMatchObject({
      requestId: "ctrl-other",

      conversationId: "conv-other",
    });
    expect(useAppStore.getState().pendingAskUser).toMatchObject({
      requestId: "ctrl-ask-other",

      conversationId: "conv-other",
    });
  });
});
