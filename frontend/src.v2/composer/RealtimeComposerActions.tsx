import { useSyncExternalStore, type ReactNode } from "react";
import { AudioLines, LoaderCircle, Mic, MicOff, Volume2, VolumeX } from "../lib/icons";
import { useAppStore } from "../stores";
import { useI18n } from "../i18n";
import { dictationController } from "./dictation-controller";
import { realtimeController } from "./realtime-controller";
import { VoiceInput } from "./VoiceInput";
import "./realtime.css";

export function RealtimeComposerActions({ sendButton }: { sendButton: ReactNode }) {
  const { t } = useI18n();
  const call = useSyncExternalStore(realtimeController.subscribe, realtimeController.getSnapshot);
  const dictation = useSyncExternalStore(dictationController.subscribe, dictationController.getSnapshot);
  const conversationId = useAppStore(state => state.conversationId);
  const capability = useAppStore(state => state.runtimeCapabilities?.realtime);
  const hasMessage = useAppStore(state => !!state.draft.trim() || !!state.attachments.length
    || !!state.quotedMessage || !!state.selectedMentions.length || !!state.selectedSkills.length);
  const blocked = useAppStore(state => !state.isConnected || !!state.pendingConversationSwitchId
    || !!state.pendingConversationCreateId || state.isStreaming || !!state.draft.trim() || !!state.attachments.length
    || !!state.quotedMessage || !!state.selectedMentions.length || !!state.selectedSkills.length || state.goalMode);
  const ownsCall = call.phase !== "inactive" && (!call.conversationId || call.conversationId === conversationId);
  if (ownsCall) {
    const micLabel = call.microphoneMuted ? t("取消麦克风静音") : t("麦克风静音");
    const outputLabel = call.outputMuted ? t("取消扬声器静音") : t("扬声器静音");
    return <div className="composer-realtime-controls" data-phase={call.phase}>
      <button type="button" className="composer-voice-btn" aria-label={micLabel} title={micLabel}
        aria-pressed={call.microphoneMuted} disabled={call.phase !== "active"} onClick={realtimeController.toggleMicrophone}>
        {call.microphoneMuted ? <MicOff size={16} /> : <Mic size={16} />}
      </button>
      <button type="button" className="composer-voice-btn" aria-label={outputLabel} title={outputLabel}
        aria-pressed={call.outputMuted} disabled={call.phase !== "active"} onClick={realtimeController.toggleOutput}>
        {call.outputMuted ? <VolumeX size={16} /> : <Volume2 size={16} />}
      </button>
      <button type="button" className="composer-realtime-end" aria-label={t("结束语音聊天")} title={t("结束语音聊天")}
        onClick={() => { void realtimeController.stop(); }}>
        {call.phase === "active" ? <AudioLines size={18} style={{ opacity: .5 + Math.min(1, call.level * 4) / 2 }} /> : <LoaderCircle size={16} className="animate-spin" />}
        <span>{call.phase === "starting" ? t("取消") : t("结束")}</span>
      </button>
      {call.phase === "active" && hasMessage ? sendButton : null}
    </div>;
  }
  const available = capability?.enabled && capability.configured && capability.transports.includes("websocket")
    && call.phase === "inactive" && dictation.phase === "idle" && !blocked
    && typeof navigator.mediaDevices?.getUserMedia === "function"
    && typeof AudioContext !== "undefined" && typeof AudioWorkletNode !== "undefined";
  const remote = !!window.__MINICODE_RUNTIME__?.remoteHost;
  const label = conversationId ? t("开始语音聊天") : t("开始新的语音聊天");
  return <><VoiceInput />{available
    ? <button type="button" className="composer-voice-btn composer-realtime-start" aria-label={label}
        disabled={remote} title={remote ? t("语音不支持远程项目") : label}
        onClick={() => { void realtimeController.start(capability); }}><AudioLines size={18} /></button>
    : sendButton}</>;
}
