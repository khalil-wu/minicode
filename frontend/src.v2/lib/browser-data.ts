import { embeddedBrowserClearBrowsingData, type BrowserDataType } from "../desktop/runtime";
import { showConfirm } from "../overlays/DialogService";
import { pushToast } from "../overlays/ToastContainer";

const DATA_LABELS: Record<BrowserDataType, string> = { cookies: "Cookie", siteData: "网站数据", cache: "缓存的图片和文件", downloads: "下载记录", history: "浏览历史" };
export async function clearBrowserData(dataType?: BrowserDataType) {
  const label = dataType ? DATA_LABELS[dataType] : "所有浏览数据";
  if (!await showConfirm({ title: `清除${label}`, message: dataType ? `清除内置浏览器的${label}？` : "清除内置浏览器的历史记录、Cookie、网站数据、缓存和下载记录？", confirmLabel: "清除", danger: true })) return false;
  await embeddedBrowserClearBrowsingData(dataType ? [dataType] : undefined);
  pushToast(`${label}已清除`, "success");
  return true;
}
