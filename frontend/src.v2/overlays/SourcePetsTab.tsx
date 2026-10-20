import { useEffect, useState } from "react";
import { ArrowUpRight, RefreshCw } from "../lib/icons";
import { desktop, type DesktopPet, type DesktopPetState } from "../desktop/runtime";
import { PetSprite } from "../pets/PetSprite";
import { useI18n } from "../i18n";
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
  const row = (pet: DesktopPet) => {
    const selected = state!.petVisible && state!.selectedPetId === pet.id;
    return <div className="settings-row settings-pet-row" key={pet.id}>
      <PetSprite pet={pet} active={active} reducedMotion={state!.reducedMotion} />
      <div className="settings-row-copy"><div className="settings-row-title">{pet.displayName}</div><div className="settings-row-description">{pet.description}</div></div>
      <button type="button" className="settings-action-button" aria-label={t(selected ? "已选择 {v0}" : "选择 {v0}", { v0: pet.displayName })}
        disabled={selected || busy} onClick={() => void run(() => api!.update({ selectedPetId: pet.id, petVisible: true }))}>{t(selected ? "已选择" : "选择")}</button>
    </div>;
  };
  if (!api) return <p className="settings-page-note">{t("Mini 仅可在桌面端使用。")}</p>;
  return <>
    <section className="settings-group settings-pets-group">
      <div className="settings-pets-heading"><div><h3 className="settings-group-title">{t("选择宠物")}</h3><p className="settings-page-note">{t("Mini 管理聊天并显示需要关注的事项")}</p></div>
        <div className="settings-pets-actions"><button type="button" className="settings-icon-button" aria-label={t("刷新宠物")} title={t("刷新")} disabled={busy || loading} onClick={() => void run(() => api.refresh())}><RefreshCw size={16} className={loading ? "animate-spin" : undefined} /></button>
          <button type="button" className="settings-action-button" aria-label={t("创建宠物")} disabled={creating || isCreatingPet || busy || loading} onClick={() => void create()}>{t(creating || isCreatingPet ? "正在创建…" : "创建")}</button>
          <button type="button" className="settings-action-button" disabled={busy || loading || !state} onClick={() => void run(() => api.toggleMini())}>{t(state?.miniOpen ? "收起 Mini" : "唤醒 Mini")}</button>
        </div>
      </div>
      {loading && !state && <p className="settings-page-note" role="status">{t("正在加载宠物…")}</p>}
      {error && <p className="settings-pets-error" role="alert">{t("无法更新宠物状态：{v0}", { v0: error })}</p>}
      {state && <div className="settings-card settings-pets-card">
        {state.pets.filter(pet => pet.assetRef).map(row)}
        <div className="settings-row settings-pets-folder"><div className="settings-row-copy"><div className="settings-row-title">{t("自定义宠物")}</div><div className="settings-row-description settings-path-description">{state.petDirectory}</div></div>
          <button type="button" className="settings-action-button settings-pets-open-folder" disabled={busy} onClick={() => void run(() => api.openFolder())}>{t("打开文件夹")}<ArrowUpRight size={14} /></button></div>
        {state.pets.filter(pet => !pet.assetRef).map(row)}
        {state.issues.map(issue => <p className="settings-pets-issue" key={issue} role="alert">{issue}</p>)}
      </div>}
    </section>
    {state && <section className="settings-group"><h3 className="settings-group-title">{t("外观")}</h3><div className="settings-card"><div className="settings-row"><div className="settings-row-copy"><label htmlFor="minicode-pet-size" className="settings-row-title">{t("宠物大小")}</label><div className="settings-row-description">{t("调整宠物的大小")}</div></div>
      <input id="minicode-pet-size" className="settings-pet-size" type="range" min={80} max={224} step={1} value={state.size} aria-label={t("宠物大小")} onChange={event => { const size = Number(event.currentTarget.value); void run(() => api.update({ size })); }} />
    </div></div></section>}
  </>;
}

export default SourcePetsTab;
