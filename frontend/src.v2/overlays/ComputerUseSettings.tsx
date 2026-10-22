import { IconButton } from "../components/Button";
import { Trash2 } from "../lib/icons";
import { useI18n } from "../i18n";
import { showConfirm } from "./DialogService";
import { pushToast } from "./ToastContainer";
import type { useComputerUseSettings } from "../hooks/useComputerUseSettings";
import type { ComputerUseApprovedApp } from "../protocol/computer-use-settings";
import "./ComputerUseSettings.css";

export function ComputerUseSettings({ model }: { model: ReturnType<typeof useComputerUseSettings> }) {
  const { t } = useI18n();
  const data = model.data;
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
    {data.capabilities.persistent_approvals && <section className="settings-group" aria-labelledby="computer-use-allowed-apps">
      <h3 className="settings-group-title" id="computer-use-allowed-apps">{t("始终允许的应用")}</h3>
      <div className="settings-card" aria-busy={model.loading || model.removing !== null}>
        {data.approved_apps!.length === 0 ? <div className="settings-row"><span className="settings-note">{t("暂无")}</span></div>
          : data.approved_apps!.map(app => <div className="settings-row" key={app.app_id}>
            <div className="settings-computer-use-app">
              {app.icon_data_url ? <img className="settings-computer-use-app-icon" alt="" draggable={false} src={app.icon_data_url} />
                : <span className="settings-computer-use-app-icon settings-computer-use-app-initial" aria-hidden="true">{app.display_name.slice(0, 1).toUpperCase()}</span>}
              <span className="settings-row-title">{app.display_name}</span>
            </div>
            {data.capabilities.revoke_app_approval && <IconButton variant="danger" label={t("移除 {displayName}", { displayName: app.display_name })}
              disabled={model.loading || model.removing !== null} onClick={() => void remove(app)}><Trash2 size={16} /></IconButton>}
          </div>)}
      </div>
    </section>}
  </div>;
}
