import { useI18n } from "../i18n";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ArrowLeft, ChevronDown, ChevronRight, LoaderCircle, Monitor, Server } from "lucide-react";
import { ContextMenu } from "../components/ContextMenu";
import { pushToast } from "./ToastContainer";
import { Section } from "./settingsShared";
import { showConfirm } from "./DialogService";
import { currentPersonalizationHost, listPersonalizationHosts, personalizationRequest, type MemorySettingsPayload, type MemorySettingsUpdate, type PersonalizationPayload } from "../protocol/memory-settings";
import type { PersonalizationHost } from "../desktop/runtime";
import "./PersonalizationTab.css";

type InstructionsState = { payload?: PersonalizationPayload; draft?: string; loading?: boolean; saving?: boolean; loadError?: string; saveError?: string };
type MemoryState = { payload?: MemorySettingsPayload; loading?: boolean; pending?: boolean; loadError?: string; error?: string; resetting?: boolean };
const errorMessage = (error: unknown): string => error instanceof Error ? error.message : String(error);

export const PersonalizationTab = ({ active = true }: { active?: boolean }) => {
  const { t: tr } = useI18n();
  const [selectedHost, setSelectedHost] = useState<PersonalizationHost>(currentPersonalizationHost);
  const hostId = selectedHost.id;
  const currentHost = useRef(hostId);
  currentHost.current = hostId;
  const [hosts, setHosts] = useState<PersonalizationHost[]>([selectedHost]);
  const [hostError, setHostError] = useState("");
  const [hostMenu, setHostMenu] = useState(false);
  const hostButton = useRef<HTMLButtonElement>(null);
  const [memories, setMemories] = useState<Record<string, MemoryState>>({});
  const memoryEpoch = useRef<Record<string, number>>({});
  const [instructions, setInstructions] = useState<Record<string, InstructionsState>>({});
  const instructionEpoch = useRef<Record<string, number>>({});
  const [editingInstructions, setEditingInstructions] = useState(false);
  const memory = memories[hostId] ?? {};
  const editor = instructions[hostId] ?? {};
  const contents = editor.draft ?? editor.payload?.instructions ?? "";
  const dirty = editor.payload !== undefined && editor.draft !== undefined && editor.draft !== editor.payload.instructions;
  const tooLarge = editor.payload !== undefined && new TextEncoder().encode(contents).length > editor.payload.max_bytes;
  const patchMemory = (id: string, patch: Partial<MemoryState>) => setMemories(current => ({ ...current, [id]: { ...current[id], ...patch } }));
  const patchInstructions = (id: string, patch: Partial<InstructionsState>) => setInstructions(current => ({ ...current, [id]: { ...current[id], ...patch } }));

  const loadHosts = useCallback(async () => {
    try { setHosts(await listPersonalizationHosts()); setHostError(""); }
    catch (error) { setHostError(errorMessage(error)); }
  }, []);
  const loadMemory = useCallback(async (id: string) => {
    const epoch = (memoryEpoch.current[id] ?? 0) + 1;
    memoryEpoch.current[id] = epoch;
    patchMemory(id, { loading: true, loadError: "", error: "" });
    try {
      const payload = await personalizationRequest<MemorySettingsPayload>(id, "read-memory");
      if (memoryEpoch.current[id] === epoch) patchMemory(id, { payload });
    } catch (error) {
      if (memoryEpoch.current[id] === epoch) patchMemory(id, { loadError: errorMessage(error) });
    } finally {
      if (memoryEpoch.current[id] === epoch) patchMemory(id, { loading: false });
    }
  }, []);
  useEffect(() => { if (active) void loadHosts(); }, [active, loadHosts]);
  useEffect(() => { if (active) void loadMemory(hostId); }, [active, hostId, loadMemory]);

  const loadInstructions = async (id: string) => {
    const epoch = (instructionEpoch.current[id] ?? 0) + 1;
    instructionEpoch.current[id] = epoch;
    patchInstructions(id, { loading: true, loadError: "" });
    try {
      const payload = await personalizationRequest<PersonalizationPayload>(id, "read-instructions");
      if (instructionEpoch.current[id] === epoch) patchInstructions(id, { payload });
    } catch (error) {
      if (instructionEpoch.current[id] === epoch) patchInstructions(id, { loadError: errorMessage(error) });
    } finally {
      if (instructionEpoch.current[id] === epoch) patchInstructions(id, { loading: false });
    }
  };
  const closeInstructions = () => {
    patchInstructions(hostId, { draft: undefined, saveError: "" });
    setEditingInstructions(false);
  };
  const backToPersonalization = async () => {
    if (editor.saving) return;
    if (dirty && !await showConfirm({ title: tr("放弃未保存的更改？"), message: tr("返回后将放弃对自定义指令的更改。"), confirmLabel: tr("放弃更改"), cancelLabel: tr("继续编辑") })) return;
    closeInstructions();
  };
  const save = async () => {
    if (!dirty || tooLarge || editor.saving) return;
    const owner = hostId, submitted = contents;
    instructionEpoch.current[owner] = (instructionEpoch.current[owner] ?? 0) + 1;
    patchInstructions(owner, { loading: false, saving: true, saveError: "" });
    try {
      const payload = await personalizationRequest<PersonalizationPayload>(owner, "update-instructions", { instructions: submitted });
      patchInstructions(owner, { payload, draft: undefined });
      if (currentHost.current === owner) { setEditingInstructions(false); pushToast(tr("自定义指令已保存"), "success"); }
    } catch (error) {
      const message = errorMessage(error);
      patchInstructions(owner, { saveError: message });
      if (currentHost.current === owner) pushToast(tr("自定义指令保存失败：{v0}", { v0: message }), "error");
    } finally { patchInstructions(owner, { saving: false }); }
  };
  const onEditorKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (!active || event.nativeEvent.isComposing || !(event.metaKey || event.ctrlKey) || !["s", "Enter"].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation(); void save();
  };
  const updateMemory = async (updates: MemorySettingsUpdate) => {
    const owner = hostId;
    memoryEpoch.current[owner] = (memoryEpoch.current[owner] ?? 0) + 1;
    patchMemory(owner, { pending: true, loading: false, error: "" });
    try { patchMemory(owner, { payload: await personalizationRequest<MemorySettingsPayload>(owner, "update-memory", updates) }); }
    catch (error) { patchMemory(owner, { error: errorMessage(error) }); }
    finally { patchMemory(owner, { pending: false }); }
  };
  const resetMemory = async () => {
    const owner = hostId;
    if (!await showConfirm({ title: tr("删除全部 MiniCode 记忆？"), message: tr("这会删除 {v0} 上的全部 MiniCode 记忆。", { v0: selectedHost.displayName }), confirmLabel: tr("删除"), cancelLabel: tr("取消"), danger: true })) return;
    patchMemory(owner, { resetting: true, error: "" });
    try {
      await personalizationRequest(owner, "reset-memory", { confirmed: true });
      if (currentHost.current === owner) pushToast(tr("MiniCode 记忆已删除"), "success");
    } catch (error) { patchMemory(owner, { error: errorMessage(error) }); }
    finally { patchMemory(owner, { resetting: false }); }
  };

  if (editingInstructions) return <section className="personalization-instructions-page" aria-labelledby="personalization-instructions-title" onKeyDown={onEditorKeyDown}>
    <header className="settings-page-heading personalization-editor-heading">
      <button type="button" className="personalization-back" onClick={() => void backToPersonalization()} disabled={editor.saving}><ArrowLeft aria-hidden="true" />{tr("返回")}</button>
      <div className="personalization-editor-actions">
        <button type="button" className="settings-action-button" onClick={closeInstructions} disabled={editor.saving}>{tr("取消")}</button>
        <button type="button" className="settings-action-button" data-primary="true" onClick={() => void save()} disabled={!dirty || tooLarge || editor.saving}>{editor.saving && <LoaderCircle className="settings-spin" aria-hidden="true" />}{editor.saving ? tr("保存中") : tr("保存")}</button>
      </div>
      <h2 id="personalization-instructions-title">{tr("自定义指令")}</h2>
      <p>{editor.payload ? tr("编辑 {v0} 上的 {v1}。项目指令也可能适用。", { v0: selectedHost.displayName, v1: editor.payload.path.split(/[\\/]/).pop()! }) : tr("编辑所选电脑上的自定义指令，项目指令也可能适用。")}</p>
    </header>
    {editor.loadError && <div className="personalization-editor-error" role="alert"><span>{tr("自定义指令暂时不可用")}：{editor.loadError}</span><button type="button" className="settings-action-button" onClick={() => void loadInstructions(hostId)} disabled={editor.loading || editor.saving}>{tr("重试")}</button></div>}
    {editor.loading && !editor.payload ? <div className="personalization-loading" role="status"><LoaderCircle className="settings-spin" aria-hidden="true" />{tr("正在读取指令…")}</div>
      : editor.payload && <textarea className="personalization-instructions-input" aria-label={tr("自定义指令")} value={contents} onChange={event => patchInstructions(hostId, { draft: event.target.value, saveError: "" })} rows={13} spellCheck={false} disabled={editor.saving} placeholder={tr("添加自定义指令…")} autoFocus />}
    {tooLarge && editor.payload && <p className="personalization-inline-error" role="alert">{tr("自定义指令不能超过 {v0} KiB", { v0: editor.payload.max_bytes / 1024 })}</p>}
    {editor.saveError && <p className="personalization-inline-error" role="alert">{tr("自定义指令保存失败：{v0}", { v0: editor.saveError })}</p>}
  </section>;

  const payload = memory.payload;
  const enabled = payload?.feature_enabled === true && payload.memories.generate_memories && payload.memories.use_memories;
  const busy = memory.loading || memory.pending || memory.resetting;
  return <div className="personalization-tab">
    <Section title={tr("MiniCode 记忆")} description={tr("配置 MiniCode 在 {v0} 上管理记忆的方式。", { v0: selectedHost.displayName })}>
      <div className="personalization-memory-heading-actions">
        <button ref={hostButton} type="button" className="settings-action-button personalization-host-selector" aria-label={tr("选择记忆主机")} aria-haspopup="menu" aria-expanded={hostMenu} onClick={() => { void loadHosts(); setHostMenu(value => !value); }}>
          {selectedHost.kind === "local" ? <Monitor size={15} /> : <Server size={15} />}{selectedHost.kind === "local" ? tr("本地主机") : selectedHost.displayName}<ChevronDown size={14} />
        </button>
        <a href="https://developers.openai.com/codex/memories" target="_blank" rel="noreferrer">{tr("了解更多")}</a>
      </div>
      {hostMenu && <ContextMenu anchor={hostButton.current!} position={{ x: 0, y: 0 }} onClose={() => setHostMenu(false)} items={hosts.map(host => ({ label: host.kind === "local" ? tr("本地主机") : host.displayName, checked: host.id === hostId, checkType: "radio", onClick: () => setSelectedHost(host) }))} />}
      {hostError && <p role="alert" className="personalization-inline-error">{hostError}</p>}
      {!payload && memory.loading && <div className="personalization-loading" role="status"><LoaderCircle className="settings-spin" aria-hidden="true" />{tr("正在读取记忆设置…")}</div>}
      {memory.loadError && <div className="personalization-editor-error" role="alert"><span>{memory.loadError}</span><button type="button" className="settings-action-button" onClick={() => void loadMemory(hostId)} disabled={busy}>{tr("重试")}</button></div>}
      {memory.error && <p className="personalization-inline-error" role="alert">{memory.error}</p>}
      {payload && !memory.loadError && <div className="personalization-memory-card">
        <div className="personalization-memory-row"><div className="settings-row-copy"><div className="settings-row-title">{tr("启用 MiniCode 记忆功能")}</div><div className="settings-row-description">{tr("根据 {v0} 上的聊天创建记忆，并使用这些记忆为今后在 {v0} 上的聊天提供个性化体验。", { v0: selectedHost.displayName })}</div></div>
          <button type="button" className="settings-toggle" role="switch" aria-label={tr("启用 MiniCode 记忆")} aria-checked={enabled} data-active={enabled} disabled={busy || payload.feature_locked || payload.locked_fields.some(field => field === "generate_memories" || field === "use_memories")} onClick={() => void updateMemory({ enabled: !enabled })}><span /></button></div>
        <div className="personalization-memory-row"><div className="settings-row-copy"><div className="settings-row-title">{tr("允许从使用工具的聊天中生成记忆")}</div><div className="settings-row-description">{tr("从使用过 MCP 工具或网页搜索的聊天生成记忆")}</div></div>
          <button type="button" className="settings-toggle" role="switch" aria-label={tr("允许从使用工具的聊天中生成记忆")} aria-checked={!payload.memories.disable_on_external_context} data-active={!payload.memories.disable_on_external_context} disabled={busy || !payload.feature_enabled || payload.locked_fields.includes("disable_on_external_context")} onClick={() => void updateMemory({ disable_on_external_context: !payload.memories.disable_on_external_context })}><span /></button></div>
        <div className="personalization-memory-row"><div className="settings-row-copy"><div className="settings-row-title">{tr("删除 MiniCode 记忆")}</div><div className="settings-row-description">{tr("删除 {v0} 上的全部 MiniCode 记忆", { v0: selectedHost.displayName })}</div></div><button type="button" className="settings-action-button" data-danger="true" disabled={busy} onClick={() => void resetMemory()}>{memory.resetting && <LoaderCircle className="settings-spin" aria-hidden="true" />}{tr("删除")}</button></div>
      </div>}
    </Section>
    <Section title={tr("自定义指令")}><button type="button" className="personalization-instructions-entry" aria-label={tr("编辑所选主机指令")} onClick={() => { setEditingInstructions(true); patchInstructions(hostId, { saveError: "" }); void loadInstructions(hostId); }}><span className="settings-row-copy"><span className="settings-row-title">{tr("MiniCode 指令")}</span><span className="settings-row-description">{tr("编辑所选电脑上的自定义指令，项目指令也可能适用。")}</span></span><ChevronRight aria-hidden="true" /></button></Section>
  </div>;
};
