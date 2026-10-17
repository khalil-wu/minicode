import { useRef, useState } from "react";
import { ContextMenu } from "../../components/ContextMenu";
import { Tip } from "../../components/Tooltip";
import { useI18n } from "../../i18n";
import { copyText } from "../../lib/clipboard";
import "./ChangesFileActions.css";

type FileActionIcon = "open" | "code" | "more";
type SourcePath = { d: string; fill?: string; fillRule?: "evenodd" | "nonzero"; clipRule?: "evenodd" | "nonzero" };
// BEGIN STATIC SOURCE ASSETS
const sourceAssets: Record<FileActionIcon, { viewBox: string; paths: SourcePath[] }> = {
  "code": {
    "viewBox": "0 0 16 16",
    "paths": [
      {
        "d": "M3.58126 5.02178C3.75323 4.78839 4.08222 4.73855 4.31564 4.91045C4.54904 5.08245 4.59894 5.4114 4.42696 5.64482L2.65353 8.05107L4.42404 10.4144C4.59781 10.6464 4.55063 10.9749 4.31857 11.1487C4.0865 11.3223 3.75792 11.2752 3.58419 11.0433L1.63888 8.44658C1.46519 8.21431 1.46404 7.89502 1.63595 7.66143L3.58126 5.02178Z",
        "fill": "currentColor"
      },
      {
        "d": "M11.6887 4.91045C11.9221 4.73845 12.2511 4.78835 12.4231 5.02178L14.3684 7.66143C14.5405 7.89505 14.5392 8.21423 14.3654 8.44658L12.4201 11.0433C12.2463 11.2752 11.9178 11.3225 11.6858 11.1487C11.4538 10.975 11.4067 10.6464 11.5803 10.4144L13.3508 8.05107L11.5774 5.64482C11.4054 5.41147 11.4555 5.08249 11.6887 4.91045Z",
        "fill": "currentColor"
      },
      {
        "d": "M8.85372 5.10185C8.98163 4.84174 9.2967 4.73483 9.55685 4.8626C9.81654 4.99067 9.92374 5.30479 9.79611 5.56475L7.14669 10.9515C7.0187 11.2115 6.70467 11.3186 6.44454 11.1907C6.18458 11.0627 6.07753 10.7487 6.20529 10.4886L8.85372 5.10185Z",
        "fill": "currentColor"
      }
    ]
  },
  "more": {
    "viewBox": "0 0 16 16",
    "paths": [
      {
        "d": "M3.33362 6.80811C3.99161 6.80828 4.52502 7.34246 4.52502 8.00049C4.52485 8.65837 3.9915 9.19172 3.33362 9.19189C2.67559 9.19189 2.14141 8.65848 2.14124 8.00049C2.14124 7.34235 2.67548 6.80811 3.33362 6.80811Z",
        "fill": "currentColor"
      },
      {
        "d": "M8.00061 6.80811C8.65849 6.80841 9.19202 7.34254 9.19202 8.00049C9.19184 8.65829 8.65838 9.19159 8.00061 9.19189C7.34258 9.19189 6.8084 8.65848 6.80823 8.00049C6.80823 7.34235 7.34247 6.80811 8.00061 6.80811Z",
        "fill": "currentColor"
      },
      {
        "d": "M12.6666 6.80811C13.3246 6.80828 13.858 7.34246 13.858 8.00049C13.8579 8.65837 13.3245 9.19172 12.6666 9.19189C12.0088 9.1917 11.4744 8.65836 11.4742 8.00049C11.4742 7.34247 12.0087 6.8083 12.6666 6.80811Z",
        "fill": "currentColor"
      }
    ]
  },
  "open": {
    "viewBox": "0 0 16 16",
    "paths": [
      {
        "d": "M6.66699 2.47461C6.95676 2.47479 7.19136 2.7102 7.19141 3C7.19141 3.28984 6.95679 3.52521 6.66699 3.52539H5.33301C4.33458 3.52557 3.52562 4.33459 3.52539 5.33301V10.667C3.52557 11.6654 4.33455 12.4744 5.33301 12.4746H10.667C11.6654 12.4744 12.4744 11.6654 12.4746 10.667V9.33301C12.4748 9.04325 12.7102 8.80859 13 8.80859C13.2898 8.80859 13.5252 9.04325 13.5254 9.33301V10.667C13.5252 12.2453 12.2453 13.5252 10.667 13.5254H5.33301C3.75465 13.5252 2.47479 12.2453 2.47461 10.667V5.33301C2.47484 3.75469 3.75468 2.47479 5.33301 2.47461H6.66699Z",
        "fill": "currentColor"
      },
      {
        "d": "M13.4072 1.81738C13.8352 1.81738 14.1816 2.16477 14.1816 2.59277L14.1826 5.87695C14.1825 6.16664 13.9479 6.40209 13.6582 6.40234C13.3683 6.40234 13.1329 6.1668 13.1328 5.87695V3.61621L9.21387 7.53516C9.00885 7.74017 8.67573 7.74015 8.4707 7.53516C8.26568 7.33013 8.26568 6.99702 8.4707 6.79199L12.3955 2.86719H10.123C9.83327 2.86714 9.59885 2.63251 9.59863 2.34277C9.59863 2.05285 9.83313 1.81743 10.123 1.81738H13.4072Z",
        "fill": "currentColor"
      }
    ]
  }
};
// END STATIC SOURCE ASSETS

function FileIcon({ icon }: { icon: FileActionIcon }) {
  const asset = sourceAssets[icon];
  return <svg width="16" height="16" viewBox={asset.viewBox} fill="none" aria-hidden="true">
    {asset.paths.map((path, index) => <path key={index} {...path} />)}
  </svg>;
}

export interface ChangesFileActionsProps {
  /** The caller supplies the visible, workspace-relative path used by Copy path. */
  path: string;
  open: boolean;
  onToggle: () => void;
  onOpen: () => void;
  onOpenExternal?: () => void;
}

/** The per-file custom tab action and preferred external editor keep separate callbacks. */
export function ChangesFileActions({ path, open, onToggle, onOpen, onOpenExternal }: ChangesFileActionsProps) {
  const { t, locale } = useI18n();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const zh = locale === "zh-CN";
  const actionsLabel = zh ? "文件操作" : "File actions";
  const openLabel = zh ? "打开位置" : "Open in";
  const openTooltip = zh ? "在标签页中打开文件" : "Open file in a tab";
  const toggleLabel = open ? (zh ? "折叠文件" : "Collapse file") : (zh ? "展开文件" : "Expand file");

  return <div className="mc-changes-file-actions" onClick={event => event.stopPropagation()}>
    <div className="mc-changes-file-open-actions">
      <Tip content={openTooltip}><button type="button" className="mc-changes-file-action" aria-label={openLabel}
        onClick={event => { event.stopPropagation(); onOpen(); }}><FileIcon icon="open" /></button></Tip>
      {onOpenExternal && <Tip content={t("在编辑器中打开")}><button type="button" className="mc-changes-file-action"
        aria-label={t("在编辑器中打开")} onClick={event => { event.stopPropagation(); onOpenExternal(); }}><FileIcon icon="code" /></button></Tip>}
    </div>
    <Tip content={actionsLabel}><button ref={menuButton} type="button" className="mc-changes-file-action mc-changes-file-more"
      aria-label={actionsLabel} aria-haspopup="menu" aria-expanded={menuOpen}
      onClick={event => { event.stopPropagation(); setMenuOpen(current => !current); }}><FileIcon icon="more" /></button></Tip>
    {menuOpen && <ContextMenu ariaLabel={actionsLabel} anchor={menuButton.current!} returnFocusTo={menuButton.current!}
      position={{ x: 0, y: 0 }} onClose={() => setMenuOpen(false)} items={[
        { label: t("复制路径"), onClick: () => { void copyText(path, t("路径")); } },
        ...onOpenExternal ? [{ label: t("在编辑器中打开"), onClick: onOpenExternal }] : [],
        { label: toggleLabel, onClick: onToggle },
      ]} />}
  </div>;
}
