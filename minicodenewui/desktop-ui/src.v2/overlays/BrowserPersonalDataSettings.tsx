import { useEffect, useState, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Globe2, MoreHorizontal, Search, X } from "../lib/icons";
import { desktop, type BrowserContact, type BrowserPersonalDataState, type BrowserSavedPassword } from "../desktop/runtime";
import { useI18n } from "../i18n";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { ContextMenu } from "../components/ContextMenu";
import { BrowserSiteSettings } from "./BrowserSiteSettings";

type ManagementSection = "passwords" | "contacts" | "sites";
const EMPTY_CONTACT: BrowserContact = { id: "", fullName: "", organization: "", addressLine1: "", addressLine2: "", city: "", region: "", postalCode: "", country: "", email: "", phone: "" };
const CONTACT_FIELDS = [
  ["fullName", "姓名"], ["organization", "公司"], ["addressLine1", "地址行 1"], ["addressLine2", "地址行 2"],
  ["city", "城市"], ["region", "省/州"], ["postalCode", "邮政编码"], ["country", "国家或地区"], ["email", "电子邮箱"], ["phone", "电话号码"],
] as const;

export function BrowserPersonalDataSettings(props: { section: ManagementSection; active: boolean; breadcrumb: (current: string) => ReactNode }) {
  return props.section === "sites" ? <BrowserSiteSettings active={props.active} breadcrumb={props.breadcrumb} /> : <BrowserPersonalDataManagement section={props.section} active={props.active} breadcrumb={props.breadcrumb} />;
}

function BrowserPersonalDataManagement({ section, active, breadcrumb }: { section: "passwords" | "contacts"; active: boolean; breadcrumb: (current: string) => ReactNode }) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<BrowserPersonalDataState>();
  const [contacts, setContacts] = useState<BrowserContact[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [passwordDraft, setPasswordDraft] = useState<BrowserSavedPassword | null>(null);
  const [contactDraft, setContactDraft] = useState<BrowserContact | null>(null);
  const [menu, setMenu] = useState<{ position: { x: number; y: number }; id: string } | null>(null);
  useEffect(() => {
    if (!active) return;
    let mounted = true;
    setLoading(true); setError("");
    const load = async () => {
      const settings = await desktop()!.browserPersonalData.listPasswords();
      if (!mounted) return;
      setStatus(settings);
      if (section === "contacts" && settings.available) {
        const records = await desktop()!.browserPersonalData.listContacts();
        if (mounted) setContacts(records);
      }
    };
    void load().catch(reason => { if (mounted) setError(String(reason)); }).finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, [section, active, revision]);
  const perform = async (operation: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await operation(); setRevision(current => current + 1); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  const editPassword = async (id: string) => {
    setBusy(true); setError("");
    try { setPasswordDraft(await desktop()!.browserPersonalData.getPassword({ id })); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  const title = section === "passwords" ? t("密码管理器") : t("联系信息");
  const toggleAutofill = () => void perform(async () => {
    const field = section === "passwords" ? "passwordAutofillEnabled" : "contactAutofillEnabled";
    setStatus(await desktop()!.browserPersonalData.setAutofillSettings({ [field]: !status![field] }));
  });
  const search = query.trim().toLocaleLowerCase();
  const passwords = status?.items.filter(item => `${item.origin} ${item.username}`.toLocaleLowerCase().includes(search)) ?? [];
  const matchingContacts = contacts.filter(item => Object.values(item).join(" ").toLocaleLowerCase().includes(search));
  const actions = (event: MouseEvent<HTMLButtonElement>, id: string) => {
    const rect = event.currentTarget.getBoundingClientRect();
    setMenu({ position: { x: rect.right - 180, y: rect.bottom + 6 }, id });
  };
  return <section className="settings-browser-personal-data" aria-label={title}>
    {breadcrumb(title)}
    <header className="settings-page-heading"><div className="settings-page-heading-copy"><h2>{title}</h2></div></header>
    {error && <p role="alert">{error}</p>}
    {loading ? <p role="status">{t("正在加载…")}</p> : !status?.available ? <p>{t("操作系统安全存储不可用，无法管理浏览器个人数据。")}</p> : <>
      <div className="settings-card"><div className="settings-row"><div className="settings-row-copy"><div className="settings-row-title">{section === "passwords" ? t("自动填充密码") : t("保存并填写地址")}</div></div><button type="button" role="switch" className="settings-toggle" aria-label={section === "passwords" ? t("自动填充密码") : t("保存并填写地址")} aria-checked={section === "passwords" ? status.passwordAutofillEnabled : status.contactAutofillEnabled} data-active={section === "passwords" ? status.passwordAutofillEnabled : status.contactAutofillEnabled} disabled={busy} onClick={toggleAutofill}><span /></button></div></div>
      <label className="settings-search settings-browser-history-search"><Search size={16} aria-hidden="true" /><input aria-label={t("搜索 {title}", { title })} placeholder={t("搜索 {title}", { title })} value={query} onChange={event => setQuery(event.target.value)} /></label>
      <div className="settings-browser-history-section-heading"><h3>{section === "passwords" ? t("已保存的密码") : t("已保存的地址")}</h3><button type="button" className="settings-action-button" disabled={busy} onClick={() => section === "passwords" ? setPasswordDraft({ id: "", origin: "", username: "", password: "" }) : setContactDraft({ ...EMPTY_CONTACT })}>{t("添加")}</button></div>
      {section === "passwords" ? passwords.length ? <div className="settings-card">{passwords.map(item => <div className="settings-row settings-browser-personal-row" key={item.id}><Globe2 size={18} aria-hidden="true" /><button type="button" disabled={busy} className="settings-browser-personal-item" aria-label={[new URL(item.origin).hostname, item.username].filter(Boolean).join(" ")} onClick={() => editPassword(item.id)}><span>{new URL(item.origin).hostname}</span><small>{item.username}</small></button><button type="button" className="settings-browser-history-actions" aria-label={t("密码操作：{site}", { site: item.origin })} aria-haspopup="menu" disabled={busy} onClick={event => actions(event, item.id)}><MoreHorizontal size={16} /></button></div>)}</div> : <p>{query ? t("没有匹配的密码") : t("尚未保存密码")}</p>
        : matchingContacts.length ? <div className="settings-card">{matchingContacts.map(item => <div className="settings-row settings-browser-personal-row" key={item.id}><button type="button" disabled={busy} className="settings-browser-personal-item" aria-label={[item.fullName || item.email || item.addressLine1 || item.phone, [item.addressLine1, item.city, item.email, item.phone].filter(Boolean).join(" · ")].filter(Boolean).join(" ")} onClick={() => setContactDraft({ ...item })}><span>{item.fullName || item.email || item.addressLine1 || item.phone}</span><small>{[item.addressLine1, item.city, item.email, item.phone].filter(Boolean).join(" · ")}</small></button><button type="button" className="settings-browser-history-actions" aria-label={t("联系信息操作：{name}", { name: item.fullName || item.email || item.phone })} aria-haspopup="menu" disabled={busy} onClick={event => actions(event, item.id)}><MoreHorizontal size={16} /></button></div>)}</div> : <p>{query ? t("没有匹配的联系信息") : t("尚未保存地址")}</p>}
    </>}
    {menu && <ContextMenu position={menu.position} onClose={() => setMenu(null)} items={[
      { label: t("编辑"), onClick: () => section === "passwords" ? editPassword(menu.id) : setContactDraft({ ...contacts.find(item => item.id === menu.id)! }) },
      { label: t("删除"), onClick: () => void perform(() => section === "passwords" ? desktop()!.browserPersonalData.removePassword({ id: menu.id }) : desktop()!.browserPersonalData.removeContact({ id: menu.id })) },
    ]} />}
    {active && passwordDraft && <PasswordEditor initial={passwordDraft} onClose={() => setPasswordDraft(null)} onSaved={() => { setPasswordDraft(null); setRevision(current => current + 1); }} />}
    {active && contactDraft && <ContactEditor initial={contactDraft} onClose={() => setContactDraft(null)} onSaved={() => { setContactDraft(null); setRevision(current => current + 1); }} />}
  </section>;
}

function PersonalDataDialog({ title, children, busy, error, canSave, onClose, onSave }: { title: string; children: ReactNode; busy: boolean; error: string; canSave: boolean; onClose: () => void; onSave: () => void }) {
  const { t } = useI18n();
  const trap = useFocusTrap(true);
  return createPortal(<div className="settings-browser-import-backdrop" onClick={() => { if (!busy) onClose(); }}><div ref={trap} role="dialog" aria-modal="true" aria-labelledby="browser-personal-editor-title" tabIndex={-1} className="settings-browser-import-dialog settings-browser-personal-dialog" onClick={event => event.stopPropagation()} onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (!busy) onClose(); } }}><form className="settings-browser-personal-form" onSubmit={event => { event.preventDefault(); onSave(); }}><header><h2 id="browser-personal-editor-title">{title}</h2><button type="button" aria-label={t("关闭")} disabled={busy} onClick={onClose}><X size={18} /></button></header><fieldset disabled={busy}>{children}</fieldset>{error && <p role="alert">{error}</p>}<footer><button type="button" disabled={busy} onClick={onClose}>{t("取消")}</button><button type="submit" data-primary="true" disabled={busy || !canSave}>{t("保存")}</button></footer></form></div></div>, document.body);
}

function PasswordEditor({ initial, onClose, onSaved }: { initial: BrowserSavedPassword; onClose: () => void; onSaved: () => void }) {
  const { t } = useI18n();
  const [draft, setDraft] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const save = async () => {
    setBusy(true); setError("");
    try { await desktop()!.browserPersonalData.savePassword(draft); onSaved(); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  return <PersonalDataDialog title={initial.id ? t("编辑密码") : t("添加密码")} busy={busy} error={error} canSave={Boolean(draft.origin.trim() && draft.password) && (!initial.id || JSON.stringify(draft) !== JSON.stringify(initial))} onClose={onClose} onSave={() => void save()}>
    <label>{t("网站")}<input required type="url" autoComplete="off" value={draft.origin} onChange={event => setDraft(current => ({ ...current, origin: event.target.value }))} /></label>
    <label>{t("用户名")}<input autoComplete="off" value={draft.username} onChange={event => setDraft(current => ({ ...current, username: event.target.value }))} /></label>
    <label>{t("密码")}<input required type="password" autoComplete="new-password" value={draft.password} onChange={event => setDraft(current => ({ ...current, password: event.target.value }))} /></label>
  </PersonalDataDialog>;
}

function ContactEditor({ initial, onClose, onSaved }: { initial: BrowserContact; onClose: () => void; onSaved: () => void }) {
  const { t } = useI18n();
  const [draft, setDraft] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const save = async () => {
    setBusy(true); setError("");
    try { await desktop()!.browserPersonalData.saveContact(draft); onSaved(); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  };
  return <PersonalDataDialog title={initial.id ? t("编辑地址") : t("添加地址")} busy={busy} error={error} canSave={CONTACT_FIELDS.some(([field]) => draft[field].trim()) && (!initial.id || JSON.stringify(draft) !== JSON.stringify(initial))} onClose={onClose} onSave={() => void save()}>
    {CONTACT_FIELDS.map(([field, label]) => <label key={field}>{t(label)}<input type={field === "email" ? "email" : field === "phone" ? "tel" : "text"} autoComplete="off" value={draft[field]} onChange={event => setDraft(current => ({ ...current, [field]: event.target.value }))} /></label>)}
  </PersonalDataDialog>;
}
