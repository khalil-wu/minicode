import { useState } from "react";
import { useAppStore } from "../stores";
import { pushToast } from "./ToastContainer";
import { isDesktop, exportDiagnostics, revealPath } from "../desktop/runtime";
import { sendClientCommand } from "../protocol/ws-outbox";
import {
  Section,
  inputStyle,
  secondaryActionStyle,
  preStyle,
} from "./settingsShared";

export const AdvancedTab = () => {
  const envVars = useAppStore((s) => s.envVars);
  const [newEnvName, setNewEnvName] = useState("");
  const [newEnvValue, setNewEnvValue] = useState("");
  const [newEnvDescription, setNewEnvDescription] = useState("");
  const [diagResult, setDiagResult] = useState<Record<string, unknown> | null>(null);
  const [diagLoading, setDiagLoading] = useState(false);

  return (
    <>
      <Section title="环境变量">
        {envVars.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {envVars.map((v) => (
              <div key={v.name} style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 8px", background: "var(--bg-secondary)", borderRadius: 4 }}>
                <span style={{ flex: 1, fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)" }}>{v.name}</span>
                <span style={{ fontSize: "var(--text-2xs)", color: "var(--text-muted)" }}>{v.scope}</span>
                <button
                  onClick={() => {
                    sendClientCommand({ type: "env.delete", name: v.name });
                  }}
                  style={{ ...secondaryActionStyle, padding: "2px 6px", fontSize: "var(--text-2xs)", color: "var(--state-danger)" }}
                >
                  删除
                </button>
              </div>
            ))}
          </div>
        )}
        {envVars.length === 0 && <div style={{ fontSize: "var(--text-xxs)", color: "var(--text-muted)" }}>尚未配置环境变量。</div>}
        <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
          <input
            placeholder="变量名"
            value={newEnvName}
            onChange={(e) => setNewEnvName(e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, ""))}
            style={{ ...inputStyle, width: 120, fontFamily: "var(--font-mono)", fontSize: "var(--text-xxs)" }}
          />
          <input
            placeholder="变量值"
            type="password"
            value={newEnvValue}
            onChange={(e) => setNewEnvValue(e.target.value)}
            style={{ ...inputStyle, flex: 1, minWidth: 120 }}
          />
          <input
            placeholder="说明（可选）"
            value={newEnvDescription}
            onChange={(e) => setNewEnvDescription(e.target.value)}
            style={{ ...inputStyle, flex: 1, minWidth: 120 }}
          />
          <button
            onClick={() => {
              if (!newEnvName || !newEnvValue) return;
              sendClientCommand({ type: "env.set", name: newEnvName, value: newEnvValue, description: newEnvDescription });
              setNewEnvName("");
              setNewEnvValue("");
              setNewEnvDescription("");
            }}
            disabled={!newEnvName || !newEnvValue}
            style={secondaryActionStyle}
          >
            添加
          </button>
        </div>
      </Section>

      {isDesktop() && (
        <>
          <Section title="导出">
            <button
              onClick={async () => {
                setDiagLoading(true);
                try {
                  const result = await exportDiagnostics();
                  setDiagResult((result ?? null) as Record<string, unknown> | null);
                  pushToast("诊断信息已导出", "success");
                } catch {
                  pushToast("导出失败", "error");
                }
                setDiagLoading(false);
              }}
              disabled={diagLoading}
              style={secondaryActionStyle}
            >
              {diagLoading ? "正在导出…" : "导出诊断信息"}
            </button>
            {diagResult && "logPath" in diagResult && <button onClick={() => revealPath(diagResult.logPath as string)} style={secondaryActionStyle}>显示日志文件</button>}
          </Section>
          {diagResult && <pre style={preStyle}>{JSON.stringify(diagResult, null, 2)}</pre>}
        </>
      )}
    </>
  );
};
