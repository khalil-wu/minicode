import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowUpRight, MoreHorizontal, RefreshCw, X } from "../lib/icons";
import { desktop, type DesktopPet, type DesktopPetState } from "../desktop/runtime";
import { PetSprite } from "../pets/PetSprite";
import { SourcePetIcon } from "../pets/SourcePetIcon";
import { useI18n } from "../i18n";
import { useFocusTrap } from "../hooks/useFocusTrap";
import "./SourcePetsTab.css";

export function SourcePetsTab({ active = true, onCreatePet, isCreatingPet = false }: {
  active?: boolean; onCreatePet: () => void | Promise<void>; isCreatingPet?: boolean;
}) {
  const { t } = useI18n();
  const api = desktop()?.pets;
  const [state, setState] = useState<DesktopPetState>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [customizing, setCustomizing] = useState(false);
  useEffect(() => {
    if (!active || !api) return;
    let current = true;
    let receivedState = false;
    setLoading(true); setError("");
    const unsubscribe = api.onStateChanged(next => { receivedState = true; if (current) setState(next); });
    void api.getState().then(next => { if (current && !receivedState) setState(next); }).catch(reason => { if (current) setError(String(reason)); }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; unsubscribe(); };
  }, [active, api]);
  const run = async (operation: () => Promise<DesktopPetState | void>) => {
    setBusy(true); setError("");
    try { const next = await operation(); if (next !== undefined) setState(next); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  const create = async () => {
    setCreating(true); setError("");
    try { await onCreatePet(); }
    catch (reason) { setError(String(reason)); }
    finally { setCreating(false); }
  };
  const card = (pet: DesktopPet) => <button type="button" className="settings-pet-card" key={pet.id}
    aria-label={`${pet.displayName} ${pet.description}`} aria-pressed={state!.selectedPetId === pet.id} disabled={busy}
    onClick={() => void run(() => api!.update({ selectedPetId: pet.id }))}>
    <span className="settings-pet-card-artwork"><PetSprite pet={pet} size={80} active={active} reducedMotion={state!.reducedMotion} /></span>
    <span className="settings-pet-card-name">{pet.displayName}</span><span className="settings-pet-card-description">{pet.description}</span>
  </button>;
  if (!api) return <p className="settings-page-note">{t("Mini 仅可在桌面端使用。")}</p>;
  const selected = state?.pets.find(pet => pet.id === state.selectedPetId);
  return <>
    <div className="settings-pets-intro"><p className="settings-page-note">{t("Mini 管理聊天并显示需要关注的事项")}</p>
      <button type="button" className="settings-action-button" disabled={busy || loading || !state} onClick={() => void run(() => api.update({ petVisible: true }))}>{t("显示 Mini")}</button></div>
    {state && <div className="settings-card settings-pet-preview">
      <div className="settings-pet-preview-scene" aria-hidden="true" style={{ height: selected ? Math.ceil(state.size * 208 / 192) + 56 : 96 }}>
        {selected && <PetSprite pet={selected} size={state.size} active={active} reducedMotion={state.reducedMotion} />}
        <PetMiniBar />
      </div>
      <button type="button" className="settings-action-button settings-pet-customize" disabled={!selected} onClick={() => setCustomizing(true)}>{t("自定义")}</button>
    </div>}
    <section className="settings-group settings-pets-group">
      <div className="settings-pets-heading"><h3 className="settings-group-title">{t("我的虚拟宠物")}</h3>
        <div className="settings-pets-actions"><button type="button" className="settings-icon-button" aria-label={t("刷新宠物")} title={t("刷新")} disabled={busy || loading} onClick={() => void run(() => api.refresh())}><RefreshCw size={16} className={loading ? "animate-spin" : undefined} /></button>
          <button type="button" className="settings-action-button" aria-label={t("创建宠物")} disabled={creating || isCreatingPet || busy || loading} onClick={() => void create()}>{t(creating || isCreatingPet ? "正在创建…" : "创建")}</button>
        </div>
      </div>
      {loading && !state && <p className="settings-page-note" role="status">{t("正在加载宠物…")}</p>}
      {error && <p className="settings-pets-error" role="alert">{t("无法更新宠物状态：{v0}", { v0: error })}</p>}
      {state && <><div className="settings-pets-grid">
        <button type="button" className="settings-pet-card" aria-label={`${t("无")} ${t("轻量级 Mini 小伙伴")}`} aria-pressed={state.selectedPetId === null} disabled={busy}
          onClick={() => void run(() => api.update({ selectedPetId: null }))}>
          <span className="settings-pet-card-artwork"><PetMiniBar compact /></span><span className="settings-pet-card-name">{t("无")}</span><span className="settings-pet-card-description">{t("轻量级 Mini 小伙伴")}</span>
        </button>
        {state.pets.filter(pet => pet.assetRef).map(card)}
        {state.pets.filter(pet => !pet.assetRef).map(card)}
      </div><div className="settings-pets-folder"><span className="settings-page-note settings-path-description">{state.petDirectory}</span>
        <button type="button" className="settings-action-button settings-pets-open-folder" disabled={busy} onClick={() => void run(() => api.openFolder())}>{t("打开文件夹")}<ArrowUpRight size={14} /></button></div>
        {state.issues.map(issue => <p className="settings-pets-issue" key={issue} role="alert">{issue}</p>)}
      </>}
    </section>
    {active && customizing && state && <PetCustomizeDialog size={state.size} busy={busy} onClose={() => setCustomizing(false)} onSize={size => void run(() => api.update({ size }))} />}
  </>;
}

function PetMiniBar({ compact = false }: { compact?: boolean }) {
  return <span className={`settings-pet-mini-bar${compact ? " is-compact" : ""}`} aria-hidden="true"><SourcePetIcon active={false} /><span>Mini</span><MoreHorizontal size={16} /></span>;
}

function PetCustomizeDialog({ size, busy, onClose, onSize }: { size: number; busy: boolean; onClose(): void; onSize(size: number): void }) {
  const { t } = useI18n();
  const focus = useFocusTrap(true);
  return createPortal(<div className="settings-pet-customize-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={focus} className="settings-pet-customize-dialog" role="dialog" aria-modal="true" aria-labelledby="pet-customize-title" tabIndex={-1}
      onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); } }}>
      <header><h3 id="pet-customize-title">{t("自定义虚拟宠物")}</h3><button type="button" className="mc-icon-btn" aria-label={t("关闭")} onClick={onClose}><X size={18} /></button></header>
      <div className="settings-pet-size-copy"><label htmlFor="minicode-pet-size">{t("虚拟宠物大小")}</label><span>{t("调整虚拟宠物大小")}</span></div>
      <input id="minicode-pet-size" className="settings-pet-size" type="range" min={80} max={224} step={1} value={size} aria-label={t("虚拟宠物大小")}
        disabled={busy} onChange={event => onSize(Number(event.currentTarget.value))} />
      <footer><button type="button" className="settings-action-button" disabled={busy || size === 112} onClick={() => onSize(112)}>{t("重置")}</button></footer>
    </section>
  </div>, document.body);
}

export default SourcePetsTab;
