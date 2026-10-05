import { useEffect, useRef, useState } from "react";
import { LoaderCircle, Mic, Square, X } from "lucide-react";
import { useAppStore } from "../stores";
import { apiBase, authHeaders, errorMessageFromResponseText } from "../protocol/api";
import { fetchVoiceService } from "../protocol/voice";
import { openSettings } from "../lib/settings-navigation";
import { pushToast } from "../overlays/ToastContainer";

export function VoiceInput() {
  const owner = useAppStore((state) => state.conversationId);
  const enabled = useAppStore((state) => state.workbenchPreferences.speechEnabled);
  const route = useAppStore((state) => JSON.stringify([state.workbenchPreferences.speechProvider, state.workbenchPreferences.speechModel, state.workbenchPreferences.microphoneId]));
  const [phase, setPhase] = useState<"idle" | "checking" | "recording" | "transcribing">("idle");
  const [seconds, setSeconds] = useState(0);
  const [level, setLevel] = useState(0);
  const recording = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const meter = useRef<AudioContext | null>(null);
  const timer = useRef<ReturnType<typeof setInterval>>();
  const request = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const stopCapture = () => {
    clearInterval(timer.current);
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
    const audio = meter.current; meter.current = null;
    void audio?.close();
  };
  const cancel = () => {
    generation.current++; request.current?.abort();
    if (recording.current?.state === "recording") recording.current.stop();
    stopCapture(); setPhase("idle"); setLevel(0);
  };
  useEffect(() => () => { generation.current++; request.current?.abort(); if (recording.current?.state === "recording") recording.current.stop(); stopCapture(); }, []);
  useEffect(() => { cancel(); }, [owner, enabled, route]);

  const start = async () => {
    const state = useAppStore.getState();
    const preferences = state.workbenchPreferences;
    if (!preferences.speechEnabled || !preferences.speechModel.trim()) { openSettings("voice"); return; }
    const current = ++generation.current;
    const controller = request.current = new AbortController();
    setPhase("checking");
    try {
      const service = await fetchVoiceService(preferences.speechProvider, preferences.speechModel, controller.signal);
      if (generation.current !== current) return;
      if (!service.can_attempt) { setPhase("idle"); openSettings("voice"); pushToast(service.reason, "warning"); return; }
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") throw new Error("当前页面不支持录音，请使用 MiniCode 桌面端。");
      const media = await navigator.mediaDevices.getUserMedia({ audio: preferences.microphoneId ? { deviceId: { exact: preferences.microphoneId } } : true });
      if (generation.current !== current) { media.getTracks().forEach((track) => track.stop()); return; }
      stream.current = media;
      const recorder = recording.current = new MediaRecorder(media);
      const chunks: Blob[] = [];
      const audio = meter.current = new AudioContext();
      const analyser = audio.createAnalyser();
      analyser.fftSize = 256;
      audio.createMediaStreamSource(media).connect(analyser);
      await audio.resume();
      if (generation.current !== current) return;
      const samples = new Uint8Array(analyser.fftSize);
      const started = Date.now();
      setSeconds(0); setLevel(0);
      timer.current = setInterval(() => {
        analyser.getByteTimeDomainData(samples);
        const amplitude = Math.sqrt(samples.reduce((sum, value) => sum + ((value - 128) / 128) ** 2, 0) / samples.length);
        setLevel(Math.min(1, amplitude * 5)); setSeconds(Math.floor((Date.now() - started) / 1000));
      }, 160);
      recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
      recorder.onstop = () => {
        if (generation.current !== current) return;
        stopCapture();
        setPhase("transcribing");
        const form = new FormData();
        const mime = recorder.mimeType;
        form.append("audio", new Blob(chunks, { type: mime }), mime.includes("mp4") ? "recording.mp4" : "recording.webm");
        form.append("model", preferences.speechModel); form.append("language", preferences.speechLanguage);
        form.append("vocabulary", preferences.speechVocabulary); form.append("provider", service.provider);
        form.append("expected_endpoint", service.endpoint);
        void fetch(new URL("/api/voice/transcribe", apiBase()), { method: "POST", headers: authHeaders(), body: form, signal: controller.signal }).then(async (response) => {
          if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
          return response.json() as Promise<{ text: string; error?: string }>;
        }).then(({ text, error }) => {
          if (error) throw new Error(error);
          if (generation.current !== current || useAppStore.getState().conversationId !== owner) return;
          const draft = useAppStore.getState().draft;
          useAppStore.getState().setDraft(draft + (draft && !draft.endsWith("\n") ? "\n" : "") + text);
          window.dispatchEvent(new Event("composer:focus"));
        }).catch((error) => { if (!controller.signal.aborted) pushToast(error instanceof Error ? error.message : String(error), "error"); })
          .finally(() => { if (generation.current === current) setPhase("idle"); });
      };
      recorder.onerror = () => { if (generation.current === current) { cancel(); pushToast("录音中断，请检查麦克风后重试。", "error"); } };
      recorder.start(); setPhase("recording");
    } catch (error) {
      if (generation.current !== current) return;
      stopCapture(); setPhase("idle");
      if (!controller.signal.aborted) pushToast(error instanceof Error ? error.message : String(error), "error");
    }
  };
  const label = phase === "recording" ? "结束听写并转为草稿" : phase === "transcribing" ? "取消转录" : phase === "checking" ? "取消准备听写" : enabled ? "开始听写" : "配置听写";
  return <div className="composer-dictation" data-phase={phase}>
    {phase !== "idle" && <span className="composer-dictation-status" role="status">{phase === "recording" ? "录音中 " + String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0") : phase === "checking" ? "准备听写…" : "正在转成文字…"}</span>}
    {phase === "recording" && <span className="composer-dictation-meter" role="meter" aria-label="麦克风音量" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(level * 100)}><i style={{ transform: "scaleX(" + level + ")" }} /></span>}
    <button type="button" className="composer-voice-btn" aria-label={label} title={label} data-recording={phase === "recording"} onClick={() => {
      if (phase === "recording") recording.current?.stop();
      else if (phase !== "idle") cancel();
      else void start();
    }}>{phase === "recording" ? <Square size={14} fill="currentColor" /> : phase !== "idle" ? <LoaderCircle size={16} className="animate-spin" /> : <Mic size={16} />}</button>
    {phase === "recording" && <button type="button" className="composer-voice-btn" aria-label="放弃录音" title="放弃录音" onClick={cancel}><X size={14} /></button>}
  </div>;
}
