import { useSyncExternalStore } from "react";
import { LoaderCircle, Mic, RotateCcw, Square, X } from "../lib/icons";
import { useAppStore } from "../stores";
import { dictationController } from "./dictation-controller";

export function VoiceInput() {
  const enabled = useAppStore((state) => state.workbenchPreferences.speechEnabled);
  const { phase, seconds, level } = useSyncExternalStore(dictationController.subscribe, dictationController.getSnapshot);
  const label = phase === "save-error" ? "重试保存录音" : phase === "recording" ? "结束听写并转为草稿" : phase === "transcribing" ? "取消转录" : phase === "checking" ? "取消准备听写" : enabled ? "开始听写" : "配置听写";
  return <div className="composer-dictation" data-phase={phase}>
    {phase !== "idle" && <span className="composer-dictation-status" role="status">{phase === "save-error" ? "录音未保存" : phase === "recording" ? "录音中 " + String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0") : phase === "checking" ? "准备听写…" : "正在转成文字…"}</span>}
    {phase === "recording" && <span className="composer-dictation-meter" role="meter" aria-label="麦克风音量" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(level * 100)}><i style={{ transform: "scaleX(" + level + ")" }} /></span>}
    <button type="button" className="composer-voice-btn" aria-label={label} title={label} data-recording={phase === "recording"} onClick={() => {
      if (phase === "save-error") dictationController.retrySave();
      else if (phase === "recording") dictationController.stop();
      else if (phase !== "idle") dictationController.stop("cancelled");
      else void dictationController.start();
    }}>{phase === "save-error" ? <RotateCcw size={16} /> : phase === "recording" ? <Square size={14} fill="currentColor" /> : phase !== "idle" ? <LoaderCircle size={16} className="animate-spin" /> : <Mic size={16} />}</button>
    {phase === "recording" && <button type="button" className="composer-voice-btn" aria-label="放弃录音" title="放弃录音" onClick={() => dictationController.stop("cancelled")}><X size={14} /></button>}
  </div>;
}
