/* @vitest-environment jsdom */
import { beforeEach, expect, it, vi } from "vitest";
import { useAppStore } from "../stores";
import type { ApprovalPolicy } from "../protocol/capabilities";
import { normalizeInboundServerEvent } from "../protocol/server-event-validation";
import { handleRuntimeEvent } from "./runtimeEvents";

vi.mock("../hooks/useWebSocket", () => ({ getWebSocket: () => null }));
vi.mock("../overlays/ToastContainer", () => ({ pushToast: vi.fn() }));

const policy: ApprovalPolicy = { granular: {
  sandbox_approval: false, rules: true, skill_approval: false,
  request_permissions: true, mcp_elicitations: false,
} };

beforeEach(() => useAppStore.setState({ conversationId: "owned", workingDirectory: "C:/owned",
  runtimeSession: null, runtimeCapabilities: null, draft: "Retain this draft", permissionMode: "confirm" }));

it("preserves the complete configured policy from runtime capabilities into the store", () => {
  const event = normalizeInboundServerEvent({ type: "runtime.capabilities", conversation_id: "owned", workspace_root: "C:/owned",
    capabilities: { permission: { mode: "confirm", approval_policy: policy, sandbox_mode: "workspace-write",
      available_modes: ["confirm", "auto", "plan"], managed_requirements_source: "requirements.toml" } } });
  expect(event).not.toBeNull();
  expect(handleRuntimeEvent(event!)).toBe(true);
  expect(useAppStore.getState().runtimeCapabilities?.permission?.approval_policy).toEqual(policy);
  expect(useAppStore.getState().draft).toBe("Retain this draft");
});

it("retains granular settings across partial session updates without confusing tool decision levels", () => {
  const event = normalizeInboundServerEvent({ type: "task.update", partial: true, session: { session_id: "owned-session",
    active_conversation_id: "owned", workspace_root: "C:/owned", permission_mode: "confirm", approval_policy: policy,
    sandbox_mode: "workspace-write", managed_requirements_source: "requirements.toml" } });
  expect(event).not.toBeNull();
  handleRuntimeEvent(event!);
  const modelUpdate = normalizeInboundServerEvent({ type: "task.update", partial: true, session: { selected_model: "source-model" } });
  expect(modelUpdate).not.toBeNull();
  handleRuntimeEvent(modelUpdate!);
  expect(useAppStore.getState().runtimeSession?.approval_policy).toEqual(policy);
  expect(useAppStore.getState().runtimeSession?.selected_model).toBe("source-model");
  expect(useAppStore.getState().draft).toBe("Retain this draft");
});
