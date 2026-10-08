import type { AgentProviderCapabilities } from "../protocol/capabilities";
import type { RuntimeSessionSnapshot } from "../protocol/events";
import type { AppStore, EffortLevel } from "../stores/types";
import { workspaceRootsEqual } from "./workspace-path";

type SelectionProjection = AgentProviderCapabilities & { current_model?: unknown };
type SelectionState = Pick<AppStore,
  "currentModel" | "currentProvider" | "currentProviderId" | "currentProviderBaseUrl" | "currentWireApi" | "effortLevel"
>;

/** Apply only fields present in the authoritative model projection. */
export const modelSelectionPatch = (selection?: SelectionProjection): Partial<SelectionState> => {
  if (!selection) return {};
  const patch: Partial<SelectionState> = {};
  const model = selection.current_model ?? selection.model;
  if (typeof model === "string") patch.currentModel = model;
  if (typeof selection.provider === "string") patch.currentProvider = selection.provider;
  if (typeof selection.provider_id === "string") patch.currentProviderId = selection.provider_id;
  if (typeof selection.base_url === "string") patch.currentProviderBaseUrl = selection.base_url;
  if (typeof selection.wire_api === "string") patch.currentWireApi = selection.wire_api;
  const effort = typeof selection.effective_reasoning_effort === "string"
    ? selection.effective_reasoning_effort.trim().toLowerCase() : "";
  if (effort) patch.effortLevel = effort as EffortLevel;
  return patch;
};

export const runtimeModelSelectionPatch = (
  session: RuntimeSessionSnapshot,
  state: Pick<AppStore, "conversationId" | "workingDirectory" | "runtimeCapabilities">,
): Partial<SelectionState> & Partial<Pick<AppStore, "runtimeCapabilities">> => {
  if (session.active_conversation_id === undefined
    || (session.active_conversation_id ?? null) !== (state.conversationId ?? null)
    || session.workspace_root === undefined
    || !workspaceRootsEqual(session.workspace_root ?? "", state.workingDirectory)) return {};
  const provider = session.provider_capabilities ?? session.capabilities?.provider_capabilities;
  return {
    ...modelSelectionPatch({
      ...provider,
      ...(typeof session.selected_model === "string" ? { model: session.selected_model } : {}),
    }),
    ...(provider ? { runtimeCapabilities: { ...state.runtimeCapabilities, provider_capabilities: provider } } : {}),
  };
};
