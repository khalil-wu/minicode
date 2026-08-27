import { useCallback, useEffect, useState } from "react";
import { Download, Globe2, PanelRightOpen, RefreshCw, ShieldCheck } from "lucide-react";
import { embeddedBrowserGetSettings, embeddedBrowserList, embeddedBrowserSetSettings, isDesktop, type EmbeddedBrowserSettings, type EmbeddedBrowserState } from "../desktop/runtime";
import { openRightPanelFromSettings } from "../lib/settings-navigation";
import { Section } from "./settingsShared";
import { pushToast } from "./ToastContainer";
import { useAppStore } from "../stores";
import { SelectMenu } from "../components/SelectMenu";

export const BrowserIntegrationTab = () => {
  const conversationId = useAppStore((state) => state.conversationId) || "";
  const [tabs, setTabs] = useState<EmbeddedBrowserState[]>([]);
  const [downloadPolicy, setDownloadPolicy] = useState<EmbeddedBrowserSettings["downloadPolicy"]>("block");
  const [savingDownloadPolicy, setSavingDownloadPolicy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  const loadBrowserState = useCallback(async (showFeedback = false) => {
    setLoading(true);
    setLoadError("");
    try {
      const [items, settings] = await Promise.all([
        conversationId ? Promise.resolve(embeddedBrowserList(conversationId)) : Promise.resolve([]),
        isDesktop() ? Promise.resolve(embeddedBrowserGetSettings("")) : Promise.resolve(null),
      ]);
      setTabs(Array.isArray(items) ? items : []);
      if (settings?.downloadPolicy) setDownloadPolicy(settings.downloadPolicy);
      if (showFeedback) pushToast("浏览器状态已刷新", "success");
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      setTabs([]);
      setLoadError(message);
      if (showFeedback) pushToast(`浏览器状态刷新失败：${message}`, "error");
    } finally {
      setLoading(false);
    }
  }, [conversationId]);

  useEffect(() => {
    void loadBrowserState();
  }, [loadBrowserState]);

  const openBrowser = () => {
    openRightPanelFromSettings("browser");
  };

  const updateDownloadPolicy = async (policy: EmbeddedBrowserSettings["downloadPolicy"]) => {
    if (savingDownloadPolicy || policy === downloadPolicy) return;
    const previous = downloadPolicy;
    setDownloadPolicy(policy);
    setSavingDownloadPolicy(true);
    try {
      const settings = await embeddedBrowserSetSettings({ downloadPolicy: policy });
      if (!settings) throw new Error("Browser settings are unavailable");
      setDownloadPolicy(settings.downloadPolicy);
      pushToast("浏览器下载策略已保存", "success");
    } catch (error) {
      setDownloadPolicy(previous);
      const message = error instanceof Error ? error.message : String(error || "未知错误");
      pushToast(`浏览器下载设置保存失败：${message}`, "error");
    } finally {
      setSavingDownloadPolicy(false);
    }
  };

  return (
    <>
      <Section title="内置浏览器" description="页面、控制台、网络和权限共用浏览器面板。">
        <div className="settings-browser-summary">
          <span className="settings-browser-summary-icon" aria-hidden="true"><Globe2 /></span>
          <div>
            <strong>{loading ? "正在读取浏览器状态…" : tabs.length > 0 ? `${tabs.length} 个打开的页面` : "浏览器工作区"}</strong>
            <span>{loadError ? `状态读取失败：${loadError}` : tabs.find((item) => item.active)?.title || tabs[0]?.title || "当前没有打开的页面"}</span>
          </div>
          {loadError && (
            <button type="button" className="settings-action-button" disabled={loading} onClick={() => void loadBrowserState(true)}>
              <RefreshCw className={loading ? "settings-spin" : undefined} />重试
            </button>
          )}
          <button type="button" className="settings-action-button" data-primary="true" onClick={openBrowser}>
            <PanelRightOpen />打开浏览器
          </button>
        </div>
      </Section>

      <Section title="浏览器设置" description="下载为全局策略；站点权限按网站保存。">
        <div className="settings-card">
          <div className="settings-row settings-browser-permission-row">
            <span className="settings-browser-row-icon" aria-hidden="true"><Download /></span>
            <div className="settings-row-copy">
              <div className="settings-row-title">下载</div>
              <div className="settings-row-description">控制内置浏览器下载文件。</div>
            </div>
            <div className="settings-row-control">
              <SelectMenu
                ariaLabel="浏览器下载策略"
                value={downloadPolicy}
                disabled={!isDesktop() || savingDownloadPolicy}
                onValueChange={(value) => {
                  const policy = value as EmbeddedBrowserSettings["downloadPolicy"];
                  void updateDownloadPolicy(policy);
                }}
              >
                <option value="block">阻止</option>
                <option value="ask">每次询问</option>
                <option value="allow">保存到下载目录</option>
              </SelectMenu>
            </div>
          </div>
          <div className="settings-row settings-browser-permission-row">
            <span className="settings-browser-row-icon" aria-hidden="true"><ShieldCheck /></span>
            <div className="settings-row-copy">
              <div className="settings-row-title">站点权限与数据</div>
              <div className="settings-row-description">在浏览器面板管理当前网站权限和数据。</div>
            </div>
            <div className="settings-row-control">
              <button type="button" className="settings-action-button" onClick={openBrowser}>管理</button>
            </div>
          </div>
        </div>
      </Section>
    </>
  );
};
