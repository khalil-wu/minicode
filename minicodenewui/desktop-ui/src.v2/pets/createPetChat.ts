import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { sendClientCommand } from "../protocol/ws-outbox";
import { useAppStore } from "../stores";
import { getWebSocket } from "../hooks/useWebSocket";

type HatchPreparation = { skill: { name: string; path: string }; prompt: string };

export async function createPetChat() {
  const sessionId = getWebSocket()?.sessionId;
  if (!sessionId) throw new Error("Connect the local session before creating a pet.");
  const url = new URL(`${apiBase()}/api/pets/hatch/prepare`);
  url.searchParams.set("session_id", sessionId);
  const response = await fetchWithTimeout(url, { method: "POST", headers: authHeaders() });
  if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
  const prepared = await response.json() as HatchPreparation;
  const creation = useAppStore.getState().createConversation({ appMode: "cowork", bindWorkspace: false });
  const requestedId = useAppStore.getState().pendingConversationCreateId;
  if (!await creation) throw new Error("Unable to create the pet conversation.");
  const state = useAppStore.getState();
  if (state.conversationId !== requestedId) throw new Error("The active conversation changed while Hatch Pet was preparing.");
  useAppStore.setState({ selectedSkills: [{ kind: "skill", name: prepared.skill.name, path: prepared.skill.path }] });
  state.setDraft(prepared.prompt);
  sendClientCommand({ type: "skills.list" }, { silent: true });
  if (state.settingsOpen) state.toggleSettings();
  window.dispatchEvent(new Event("composer:focus"));
}
