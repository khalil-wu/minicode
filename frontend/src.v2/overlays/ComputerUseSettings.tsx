import { Button, IconButton } from "../components/Button";
import { BrandIcon } from "../components/BrandIcon";
import { Trash2 } from "../lib/icons";
import { useI18n } from "../i18n";
import { useAppStore } from "../stores";
import { pluginAssetResourceUrlWithToken } from "../protocol/api";
import { showConfirm } from "./DialogService";
import { pushToast } from "./ToastContainer";
import type { useComputerUseSettings } from "../hooks/useComputerUseSettings";
import type { ComputerUseApprovedApp, ComputerUsePluginControl } from "../protocol/computer-use-settings";
import "./ComputerUseSettings.css";

export function ComputerUseSettings({ model }: { model: ReturnType<typeof useComputerUseSettings> }) {
  const { t } = useI18n();
  const resolvedTheme = useAppStore(state => state.resolvedTheme);
  const data = model.data;
  const busy = model.loading || model.removing !== null || model.pluginPending !== null;
  const pluginIcon = (control: ComputerUsePluginControl) => resolvedTheme === "dark" && control.plugin_path && control.icon_variants.includes("logo-dark")
    ? pluginAssetResourceUrlWithToken(control.plugin_path, "logo-dark")
    : control.icon_url || (control.plugin_path && control.icon_variant ? pluginAssetResourceUrlWithToken(control.plugin_path, control.icon_variant) : undefined);
  const remove = async (app: ComputerUseApprovedApp) => {
    const removeOriginal = model.remove;
    if (!await showConfirm({
      title: t("要从始终允许的应用中移除“{displayName}”吗？", { displayName: app.display_name }),
      message: t("MiniCode 将在下次电脑操作会话中询问是否使用“{displayName}”。", { displayName: app.display_name }),
      confirmLabel: t("移除"), cancelLabel: t("取消"), danger: true,
    })) return;
    if (await removeOriginal(app)) pushToast(t("已移除允许的应用"), "success");
  };
  if (!data) return <p className="settings-note" role={model.error ? "alert" : "status"}>
    {model.error || (model.loading ? t("正在加载允许的应用…") : t("尚未连接"))}
  </p>;
  return <div className="settings-computer-use">
    {model.error && <p className="settings-note" role="alert">{model.error}</p>}
    {!data.available && data.reason && <p className="settings-note">{data.reason}</p>}
    {data.controls.length > 0 && <section className="settings-group" aria-labelledby="computer-use-control">
      <h3 className="settings-group-title" id="computer-use-control">{t("控制")}</h3>
      <div className="settings-card" aria-busy={busy}>
        {data.controls.map(control => <div className="settings-row settings-computer-use-control" key={control.id}>
          <button type="button" className="settings-computer-use-plugin-details" disabled={busy}
            aria-label={t("查看插件详情 {pluginName}", { pluginName: control.display_name })} onClick={() => model.openPluginDetails(control)}>
            <BrandIcon className="settings-computer-use-plugin-icon" value={control.display_name} inferBrand={false} fallback="plugin" size={40} iconUrl={pluginIcon(control)} />
            <span className="settings-row-copy"><span className="settings-row-title">{t("任意应用")}</span>
              <span className="settings-row-description">{t("让 MiniCode 控制电脑上的应用。")}</span>
              {control.load_errors.length > 0 && <span className="settings-note" role="alert">{control.load_errors.join("；")}</span>}
            </span>
          </button>
          {control.installed ? <button type="button" className="settings-toggle" role="switch" aria-checked={control.enabled}
            aria-label={t("切换 {pluginName}", { pluginName: control.display_name })} data-active={control.enabled}
            title={t(control.enabled ? "停用 {pluginName}" : "启用 {pluginName}", { pluginName: control.display_name })}
            disabled={busy || !control.can_toggle} onClick={() => void model.updatePlugin(control, { action: "set_enabled", enabled: !control.enabled })}><span /></button>
            : <Button size="sm" className="settings-action-button" disabled={busy || !control.can_install} loading={model.pluginPending === control.id}
              aria-label={t("安装 {pluginName}", { pluginName: control.display_name })}
              onClick={() => void model.updatePlugin(control, { action: "install" })}>{t("安装")}</Button>}
        </div>)}
      </div>
    </section>}
    {data.capabilities.persistent_approvals && <section className="settings-group" aria-labelledby="computer-use-allowed-apps">
      <h3 className="settings-group-title" id="computer-use-allowed-apps">{t("始终允许的应用")}</h3>
      <div className="settings-card" aria-busy={busy}>
        {data.approved_apps!.length === 0 ? <div className="settings-row"><span className="settings-note">{t("暂无")}</span></div>
          : data.approved_apps!.map(app => <div className="settings-row" key={app.app_id}>
            <div className="settings-computer-use-app">
              {app.icon_data_url ? <img className="settings-computer-use-app-icon" alt="" draggable={false} src={app.icon_data_url} />
                : <span className="settings-computer-use-app-icon settings-computer-use-app-initial" aria-hidden="true">{app.display_name.slice(0, 1).toUpperCase()}</span>}
              <span className="settings-row-title">{app.display_name}</span>
            </div>
            {data.capabilities.revoke_app_approval && <IconButton variant="danger" label={t("移除 {displayName}", { displayName: app.display_name })}
              disabled={busy} onClick={() => void remove(app)}><Trash2 size={16} /></IconButton>}
          </div>)}
      </div>
    </section>}
  </div>;
}
