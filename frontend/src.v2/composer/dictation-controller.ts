import { useAppStore } from "../stores";
import { composerWindowOwnerId } from "../stores/composer-drafts";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { currentDictationPreferences, openDictationProviderSettings } from "../lib/dictation-preferences";
import { dictationPreferencesOf, type DictationPreferences } from "../lib/workbench-preferences";
import { fetchVoiceService, saveDictationRecording, transcribeDictationRecording, type VoiceServiceStatus } from "../protocol/voice";
import { pushToast } from "../overlays/ToastContainer";

export type DictationTarget = { kind: "composer"; conversationId: string | null; workspaceRoot: string; windowOwnerId: string }
  | { kind: "side"; id: string; workspaceRoot: string; windowOwnerId: string }
  | { kind: "external"; token: string; insert: (text: string) => Promise<boolean> };
export type DictationState = { phase: "idle" | "checking" | "recording" | "transcribing" | "save-error"; seconds: number; level: number };
type StopReason = "saved" | "cancelled" | "interrupted";
type Capture = {
  target: DictationTarget; preferences: DictationPreferences; preferencesReady: boolean; request: AbortController; chunks: Blob[];
  service?: VoiceServiceStatus; recorder?: MediaRecorder; stream?: MediaStream; meter?: AudioContext;
  timer?: ReturnType<typeof setInterval>; startedAt: number; durationMs?: number; reason?: StopReason;
};
const listeners = new Set<() => void>();
let state: DictationState = { phase: "idle", seconds: 0, level: 0 };
let capture: Capture | null = null;
let watchingOwner = false;
let persistenceError: unknown;
const publish = (patch: Partial<DictationState>) => { state = { ...state, ...patch }; listeners.forEach((listener) => listener()); };
const historyChanged = () => window.dispatchEvent(new Event("dictation:history-changed"));

export function composerDictationTarget(): DictationTarget | undefined {
  const owner = useAppStore.getState();
  const sideElement = document.activeElement?.closest<HTMLElement>("[data-dictation-side-chat]");
  if (sideElement) {
    const sideId = sideElement.dataset.dictationSideChat;
    const side = sideId ? owner.sideChats[sideId] : undefined;
    if (!side || side.workspaceRoot === undefined) return undefined;
    return { kind: "side", id: side.id, workspaceRoot: side.workspaceRoot, windowOwnerId: composerWindowOwnerId() };
  }
  return { kind: "composer", conversationId: owner.conversationId, workspaceRoot: owner.workingDirectory, windowOwnerId: composerWindowOwnerId() };
}

function ownsComposer(target: Extract<DictationTarget, { kind: "composer" }>): boolean {
  const owner = useAppStore.getState();
  return owner.conversationId === target.conversationId && workspaceRootsEqual(owner.workingDirectory, target.workspaceRoot)
    && composerWindowOwnerId() === target.windowOwnerId;
}

function releaseAudio(current: Capture) {
  clearInterval(current.timer);
  current.stream?.getTracks().forEach((track) => track.stop()); current.stream = undefined;
  const meter = current.meter; current.meter = undefined;
  void meter?.close();
}

async function saveAndTranscribe(current: Capture) {
  releaseAudio(current);
  publish({ phase: "transcribing", level: 0 });
  const audio = new Blob(current.chunks, { type: current.recorder!.mimeType });
  if (!audio.size) { capture = null; publish({ phase: "idle" }); return; }
  let saved = false;
  persistenceError = undefined;
  try {
    const target = current.target;
    const record = await saveDictationRecording(audio, { language: current.preferences.speechLanguage,
      duration_ms: current.durationMs!, status: current.reason ?? "interrupted",
      conversation_id: target.kind === "composer" ? target.conversationId ?? "" : target.kind === "side" ? target.id : "",
      workspace_root: target.kind !== "external" ? target.workspaceRoot : "" });
    saved = true;
    historyChanged();
    if (current.reason !== "saved" || current.request.signal.aborted) return;
    const text = await transcribeDictationRecording(record.id, current.service!, current.preferences.speechModel,
      current.preferences.dictationDictionary, current.request.signal);
    historyChanged();
    if (current.request.signal.aborted) return;
    if (target.kind === "external") await target.insert(text);
    else if (target.kind === "side") {
      const owner = useAppStore.getState();
      const side = owner.sideChats[target.id];
      if (side && workspaceRootsEqual(side.workspaceRoot, target.workspaceRoot) && composerWindowOwnerId() === target.windowOwnerId) {
        owner.setSideChatDraft(target.id, side.draft + (side.draft && !side.draft.endsWith("\n") ? "\n" : "") + text);
        window.dispatchEvent(new Event("composer:focus:" + target.id));
      }
    }
    else if (ownsComposer(target)) {
      const owner = useAppStore.getState();
      owner.setDraft(owner.draft + (owner.draft && !owner.draft.endsWith("\n") ? "\n" : "") + text);
      window.dispatchEvent(new Event("composer:focus"));
    }
  } catch (error) {
    historyChanged();
    if (!saved) persistenceError = error;
    if (!saved || !current.request.signal.aborted) pushToast(error instanceof Error ? error.message : String(error), "error");
  } finally {
    if (capture === current) {
      if (saved) { capture = null; publish({ phase: "idle", seconds: 0, level: 0 }); }
      else publish({ phase: "save-error", level: 0 });
    }
  }
}

export const dictationController = {
  subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
  getSnapshot: () => state,
  getTarget: () => capture?.target,
  hasPendingAudio: () => capture !== null,
  retrySave() { if (capture && state.phase === "save-error") void saveAndTranscribe(capture); },
  async settle() {
    if (!capture) return;
    this.stop("interrupted");
    if (state.phase === "save-error") this.retrySave();
    await new Promise<void>((resolve, reject) => {
      const check = () => {
        if (state.phase !== "idle" && state.phase !== "save-error") return;
        listeners.delete(check);
        if (persistenceError) reject(persistenceError); else resolve();
      };
      listeners.add(check); check();
    });
  },
  async start(target: DictationTarget | undefined = composerDictationTarget()) {
    if (!watchingOwner) {
      useAppStore.subscribe(() => this.ownerChanged());
      window.addEventListener("dictation:service-changed", event => {
        const provider = (event as CustomEvent<{ provider: string }>).detail?.provider;
        if (capture?.service?.provider === provider && provider) this.stop("interrupted");
      });
      watchingOwner = true;
    }
    if (capture) return;
    if (!target) { pushToast("无法确认侧聊所属的工作区，请重新选择该侧聊。", "error"); return; }
    const current: Capture = { target, preferences: dictationPreferencesOf(useAppStore.getState().workbenchPreferences), preferencesReady: false,
      request: new AbortController(), chunks: [], startedAt: 0 };
    capture = current; publish({ phase: "checking", seconds: 0, level: 0 });
    try {
      const preferences = await currentDictationPreferences();
      if (capture !== current || current.request.signal.aborted) return;
      current.preferences = preferences; current.preferencesReady = true;
      if (!preferences.speechEnabled || !preferences.speechModel.trim()) {
        capture = null; publish({ phase: "idle" }); await openDictationProviderSettings(); return;
      }
      const service = await fetchVoiceService(preferences.speechProvider, preferences.speechModel, current.request.signal);
      if (capture !== current || current.request.signal.aborted) return;
      current.service = service;
      if (!service.can_attempt) { await openDictationProviderSettings(); throw new Error(service.reason); }
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") throw new Error("当前页面不支持录音，请使用 MiniCode 桌面端。");
      const stream = await navigator.mediaDevices.getUserMedia({ audio: preferences.microphoneId ? { deviceId: { exact: preferences.microphoneId } } : true });
      if (capture !== current || current.request.signal.aborted) { stream.getTracks().forEach((track) => track.stop()); return; }
      current.stream = stream;
      const recorder = current.recorder = new MediaRecorder(stream);
      const meter = current.meter = new AudioContext();
      const analyser = meter.createAnalyser(); analyser.fftSize = 256;
      meter.createMediaStreamSource(stream).connect(analyser); await meter.resume();
      if (capture !== current || current.request.signal.aborted) { releaseAudio(current); return; }
      const samples = new Uint8Array(analyser.fftSize);
      current.startedAt = Date.now();
      current.timer = setInterval(() => {
        analyser.getByteTimeDomainData(samples);
        const amplitude = Math.sqrt(samples.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0) / samples.length);
        publish({ seconds: Math.floor((Date.now() - current.startedAt) / 1000), level: Math.min(1, amplitude * 5) });
      }, 160);
      recorder.ondataavailable = (event) => { if (event.data.size) current.chunks.push(event.data); };
      recorder.onstop = () => {
        current.durationMs ??= Math.max(0, Date.now() - current.startedAt);
        current.reason ??= "interrupted";
        void saveAndTranscribe(current);
      };
      recorder.onerror = () => { this.stop("interrupted"); pushToast("录音中断，请检查麦克风后重试。", "error"); };
      recorder.start(); publish({ phase: "recording" });
    } catch (error) {
      releaseAudio(current);
      if (capture === current) { capture = null; publish({ phase: "idle" }); }
      if (!current.request.signal.aborted) pushToast(error instanceof Error ? error.message : String(error), "error");
    }
  },
  stop(reason: StopReason = "saved") {
    const current = capture;
    if (!current) return;
    current.reason = reason;
    if (current.recorder?.state === "recording") {
      current.durationMs = Math.max(0, Date.now() - current.startedAt);
      current.recorder.stop(); releaseAudio(current);
    } else {
      current.request.abort(); releaseAudio(current);
      if (!current.recorder) { capture = null; publish({ phase: "idle", level: 0 }); }
    }
  },
  ownerChanged() {
    const current = capture;
    if (!current) return;
    const preferences = useAppStore.getState().workbenchPreferences;
    const owner = useAppStore.getState();
    if ((current.target.kind === "composer" && !ownsComposer(current.target))
      || (current.target.kind === "side" && (!owner.sideChats[current.target.id] || owner.activeSideChatId !== current.target.id || !workspaceRootsEqual(owner.workingDirectory, current.target.workspaceRoot)))
      || (current.preferencesReady && (!preferences.speechEnabled
      || preferences.speechProvider !== current.preferences.speechProvider || preferences.speechModel !== current.preferences.speechModel
      || preferences.microphoneId !== current.preferences.microphoneId))) this.stop("interrupted");
  },
};
