import { useLayoutEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { useI18n } from "../../i18n";
import { ContextMenu, type ContextMenuItem } from "../../components/ContextMenu";
import { Tip } from "../../components/Tooltip";
import { Copy, LoaderCircle } from "../../lib/icons";
import { DiffSourceOptionIcon } from "../../lib/diff-source-icons";
import { useDiffViewPreference } from "./diff-view-preferences";
import { useWordDiffsPreference } from "./word-diff-preference";
import "./ChangesToolbarActions.css";

type SourceAsset = "ellipsis" | "jump" | "files" | "wrap" | "unified" | "split" | "refresh";
type SourcePath = { d: string; fill?: string; fillOpacity?: number; fillRule?: "evenodd" | "nonzero"; clipRule?: "evenodd" | "nonzero" };
// BEGIN STATIC SOURCE ASSETS
const sourceAssets: Record<SourceAsset, { viewBox: string; paths: SourcePath[] }> = {
  "ellipsis": {
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
  "files": {
    "viewBox": "0 0 16 16",
    "paths": [
      {
        "fillRule": "evenodd",
        "clipRule": "evenodd",
        "d": "M7.60791 1.47461C8.06198 1.47464 8.50506 1.61631 8.87549 1.87891L9.53662 2.34766C9.72953 2.48444 9.96128 2.55852 10.1978 2.55859H12.9995C14.2099 2.55859 15.1919 3.53958 15.1919 4.75V9C15.1919 10.2104 14.2099 11.1914 12.9995 11.1914H11.8589V11.667C11.8587 12.8773 10.8768 13.8584 9.6665 13.8584H3.00049C1.79017 13.8584 0.808271 12.8773 0.808105 11.667V6.33398C0.808105 5.12356 1.79006 4.1416 3.00049 4.1416H4.14111V3.66699C4.14111 2.45669 5.12324 1.47481 6.3335 1.47461H7.60791ZM3.00049 5.19238C2.36996 5.19238 1.85889 5.70346 1.85889 6.33398V11.667C1.85905 12.2974 2.37006 12.8086 3.00049 12.8086H9.6665C10.2969 12.8086 10.8079 12.2974 10.8081 11.667V7.41699C10.8081 6.78647 10.297 6.27539 9.6665 6.27539H6.86475C6.41064 6.27539 5.96763 6.1337 5.59717 5.87109L4.93506 5.40234C4.7421 5.26554 4.51144 5.19241 4.2749 5.19238H3.00049ZM6.3335 2.52539C5.70314 2.52559 5.19189 3.03659 5.19189 3.66699V4.34375C5.31402 4.4001 5.43168 4.46733 5.54248 4.5459L6.20361 5.01465C6.39659 5.15144 6.6282 5.22559 6.86475 5.22559H9.6665C10.8769 5.22559 11.8589 6.20657 11.8589 7.41699V10.1416H12.9995C13.63 10.1416 14.1411 9.63052 14.1411 9V4.75C14.1411 4.11947 13.63 3.6084 12.9995 3.6084H10.1978C9.74372 3.60832 9.30057 3.46671 8.93018 3.2041L8.26807 2.73535C8.07516 2.59866 7.84434 2.52542 7.60791 2.52539H6.3335Z",
        "fill": "currentColor"
      }
    ]
  },
  "wrap": {
    "viewBox": "0 0 16 16",
    "paths": [
      {
        "d": "M12.6665 2.14111C12.9565 2.14111 13.1919 2.37655 13.1919 2.6665V13.3335C13.1917 13.6233 12.9563 13.8579 12.6665 13.8579C12.3767 13.8579 12.1413 13.6233 12.1411 13.3335V2.6665C12.1411 2.37655 12.3766 2.14111 12.6665 2.14111Z",
        "fill": "currentColor"
      },
      {
        "d": "M7.1665 4.80811C8.10467 4.80811 8.90555 5.09138 9.47803 5.61182C10.0534 6.13492 10.3578 6.86284 10.3579 7.6665C10.3579 8.60869 10.0708 9.36291 9.45752 9.86475C8.93889 10.2891 8.25432 10.4769 7.49561 10.5171L7.1665 10.5249H4.43506L5.37158 11.4624C5.57632 11.6674 5.5763 11.9996 5.37158 12.2046C5.16663 12.4095 4.83444 12.4094 4.62939 12.2046L2.92529 10.5015C2.64875 10.2249 2.64891 9.77615 2.92529 9.49951L4.62939 7.79541C4.8344 7.59085 5.16668 7.59068 5.37158 7.79541C5.57627 8.00032 5.57613 8.33261 5.37158 8.5376L4.43506 9.4751H7.1665L7.44678 9.46826C8.07224 9.43437 8.50902 9.28417 8.79248 9.05225C9.09578 8.80408 9.30811 8.39088 9.30811 7.6665C9.30803 7.13714 9.11307 6.69828 8.77197 6.38818C8.42778 6.07532 7.89496 5.85889 7.1665 5.85889H3.1665C2.87655 5.85889 2.64111 5.62345 2.64111 5.3335C2.64111 5.04355 2.87655 4.80811 3.1665 4.80811H7.1665Z",
        "fill": "currentColor"
      }
    ]
  },
  "jump": {
    "viewBox": "0 0 16 16",
    "paths": [
      {
        "fillRule": "evenodd",
        "clipRule": "evenodd",
        "d": "M10.8711 8.80615C12.3848 8.80615 13.6123 10.0336 13.6123 11.5474C13.6123 12.1172 13.4374 12.646 13.1396 13.0845L14.374 14.3179C14.579 14.5228 14.5789 14.856 14.374 15.061C14.169 15.266 13.8359 15.266 13.6309 15.061L12.3945 13.8237C11.9586 14.1161 11.4353 14.2886 10.8711 14.2886C9.35746 14.2886 8.13001 13.061 8.12988 11.5474C8.12989 10.0337 9.35738 8.80616 10.8711 8.80615ZM10.8711 9.85693C9.93728 9.85694 9.18067 10.6136 9.18066 11.5474C9.1808 12.4811 9.93736 13.2378 10.8711 13.2378C11.8048 13.2378 12.5614 12.4811 12.5615 11.5474C12.5615 10.6135 11.8049 9.85693 10.8711 9.85693Z",
        "fill": "currentColor"
      },
      {
        "fillRule": "evenodd",
        "clipRule": "evenodd",
        "d": "M9.36816 1.80811C10.0378 1.80811 10.6798 2.07482 11.1533 2.54834L12.6484 4.04346C13.122 4.51699 13.3887 5.15895 13.3887 5.82861V7.16357C13.3886 7.45346 13.1532 7.68799 12.8633 7.68799C12.5734 7.68799 12.338 7.45346 12.3379 7.16357V6.85889H10.833C9.80694 6.85871 8.97479 6.02656 8.97461 5.00049V2.85889H4.86328C4.04866 2.85889 3.38867 3.51888 3.38867 4.3335V11.6665C3.38867 12.4811 4.04866 13.1421 4.86328 13.1421H6.36328C6.65312 13.1421 6.8885 13.3767 6.88867 13.6665C6.88867 13.9565 6.65323 14.1919 6.36328 14.1919H4.86328C3.46876 14.1919 2.33789 13.061 2.33789 11.6665V4.3335C2.33789 2.93898 3.46876 1.80811 4.86328 1.80811H9.36816ZM10.0254 5.00049C10.0256 5.44666 10.3868 5.80793 10.833 5.80811H12.3369C12.3316 5.42434 12.178 5.05742 11.9062 4.78564L10.4111 3.29053C10.2975 3.17693 10.1665 3.08541 10.0254 3.01514V5.00049Z",
        "fill": "currentColor"
      }
    ]
  },
  "split": {
    "viewBox": "0 0 16 16",
    "paths": [
      {
        "d": "M13.4746 4.53296C13.4744 3.79234 12.8744 3.19238 12.1338 3.19214H3.86621C3.12559 3.19238 2.52564 3.79234 2.52539 4.53296V11.4666C2.52539 12.2074 3.12544 12.8081 3.86621 12.8083H12.1338C12.8746 12.8081 13.4746 12.2074 13.4746 11.4666V4.53296ZM14.5254 11.4666C14.5254 12.7873 13.4545 13.8579 12.1338 13.8582H3.86621C2.54554 13.8579 1.47461 12.7873 1.47461 11.4666V4.53296C1.47486 3.21244 2.54569 2.1416 3.86621 2.14136H12.1338C13.4543 2.1416 14.5251 3.21244 14.5254 4.53296V11.4666Z",
        "fill": "currentColor"
      },
      {
        "d": "M3.63916 5.13882C3.63916 4.80745 3.90779 4.53882 4.23916 4.53882H6.84237C7.17374 4.53882 7.44237 4.80745 7.44237 5.13882V10.8606C7.44237 11.192 7.17374 11.4606 6.84237 11.4606H4.23916C3.90779 11.4606 3.63916 11.192 3.63916 10.8606V5.13882Z",
        "fill": "#F84E63",
        "fillOpacity": 0.5
      },
      {
        "d": "M8.54834 5.13882C8.54834 4.80745 8.81697 4.53882 9.14834 4.53882H11.7608C12.0922 4.53882 12.3608 4.80745 12.3608 5.13882V10.8606C12.3608 11.192 12.0922 11.4606 11.7608 11.4606H9.14834C8.81697 11.4606 8.54834 11.192 8.54834 10.8606V5.13882Z",
        "fill": "#36D958",
        "fillOpacity": 0.5
      }
    ]
  },
  "unified": {
    "viewBox": "0 0 16 16",
    "paths": [
      {
        "d": "M13.4746 4.53296C13.4744 3.79234 12.8744 3.19238 12.1338 3.19214H3.86621C3.12559 3.19238 2.52564 3.79234 2.52539 4.53296V11.4666C2.52539 12.2074 3.12544 12.8081 3.86621 12.8083H12.1338C12.8746 12.8081 13.4746 12.2074 13.4746 11.4666V4.53296ZM14.5254 11.4666C14.5254 12.7873 13.4545 13.8579 12.1338 13.8582H3.86621C2.54554 13.8579 1.47461 12.7873 1.47461 11.4666V4.53296C1.47486 3.21244 2.54569 2.1416 3.86621 2.14136H12.1338C13.4543 2.1416 14.5251 3.21244 14.5254 4.53296V11.4666Z",
        "fill": "currentColor"
      },
      {
        "d": "M11.7613 4.53882C12.0927 4.53882 12.3613 4.80745 12.3613 5.13882L12.3613 6.95716C12.3613 7.28853 12.0927 7.55716 11.7613 7.55716L4.23965 7.55716C3.90828 7.55716 3.63965 7.28853 3.63965 6.95716L3.63965 5.13882C3.63965 4.80745 3.90828 4.53882 4.23965 4.53882L11.7613 4.53882Z",
        "fill": "#F84E63",
        "fillOpacity": 0.5
      },
      {
        "d": "M11.7613 8.43481C12.0927 8.43481 12.3613 8.70344 12.3613 9.03481L12.3613 10.8605C12.3613 11.1919 12.0927 11.4605 11.7613 11.4605L4.23965 11.4605C3.90828 11.4605 3.63965 11.1919 3.63965 10.8605L3.63965 9.03481C3.63965 8.70344 3.90828 8.43481 4.23965 8.43481L11.7613 8.43481Z",
        "fill": "#36D958",
        "fillOpacity": 0.5
      }
    ]
  },
  "refresh": {
    "viewBox": "0 0 20 20",
    "paths": [
      {
        "d": "M3.50205 16.6664V13.3333C3.50205 12.9661 3.79982 12.6683 4.16709 12.6683H7.5001L7.63389 12.682C7.93696 12.7439 8.16514 13.0119 8.16514 13.3333C8.16514 13.6547 7.93696 13.9227 7.63389 13.9847L7.5001 13.9984H5.47471C6.58687 15.2249 8.21848 16.0013 10.0001 16.0013C13.06 16.0013 15.586 13.711 15.9552 10.7513L15.9854 10.6195C16.0846 10.3266 16.3786 10.1335 16.6974 10.1732C17.0617 10.2186 17.3198 10.551 17.2745 10.9154L17.2247 11.2523C16.6301 14.7051 13.6225 17.3313 10.0001 17.3314C8.01108 17.3314 6.17193 16.5383 4.83213 15.2474V16.6664C4.83213 17.0335 4.53416 17.3312 4.16709 17.3314C3.79982 17.3314 3.50205 17.0336 3.50205 16.6664ZM4.04502 9.24936C3.99941 9.61354 3.66706 9.87179 3.30283 9.82651C2.93839 9.78106 2.67926 9.44877 2.72471 9.08432L4.04502 9.24936ZM10.0001 2.6683C11.994 2.66834 13.8372 3.46552 15.1778 4.76205V3.33334C15.1778 2.96617 15.4757 2.66846 15.8429 2.6683C16.2101 2.6683 16.5079 2.96607 16.5079 3.33334V6.66635C16.5079 7.03362 16.2101 7.33139 15.8429 7.33139H12.5099C12.1426 7.33139 11.8448 7.03362 11.8448 6.66635C11.845 6.29923 12.1427 6.00131 12.5099 6.00131H14.5255C13.4134 4.77489 11.7816 3.99842 10.0001 3.99838C6.94004 3.99838 4.41411 6.28948 4.04502 9.24936L3.38486 9.16635L2.72471 9.08432C3.1758 5.46703 6.26081 2.6683 10.0001 2.6683Z",
        "fill": "currentColor"
      }
    ]
  }
};
// END STATIC SOURCE ASSETS

function SourceIcon({ asset }: { asset: SourceAsset }) {
  const icon = sourceAssets[asset];
  return <svg width="16" height="16" viewBox={icon.viewBox} fill="none" aria-hidden="true">
    {icon.paths.map((path, index) => <path key={index} {...path} />)}
  </svg>;
}

function AutoIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
    <path d="M13.4746 4.53296C13.4744 3.79234 12.8744 3.19238 12.1338 3.19214H3.86621C3.12559 3.19214 2.52564 3.79234 2.52539 4.53296V11.4666C2.52539 12.2074 3.12544 12.8081 3.86621 12.8083H12.1338C12.8746 12.8083 13.4746 12.2074 13.4746 11.4666V4.53296ZM14.5254 11.4666C14.5254 12.7873 13.4545 13.8579 12.1338 13.8582H3.86621C2.54554 13.8582 1.47461 12.7873 1.47461 11.4666V4.53296C1.47486 3.21244 2.54569 2.1416 3.86621 2.14136H12.1338C13.4543 2.1416 14.5251 3.21244 14.5254 4.53296V11.4666Z" fill="currentColor" />
    <path d="M4.24 4.54H11.66L3.64 10.76V5.14C3.64 4.80863 3.90863 4.54 4.24 4.54Z" fill="#F84E63" fillOpacity="0.5" />
    <path d="M12.36 5.24V10.86C12.36 11.1914 12.0914 11.46 11.76 11.46H4.34L12.36 5.24Z" fill="#36D958" fillOpacity="0.5" />
  </svg>;
}

function ExpandIcon({ expanded }: { expanded: boolean }) {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    {expanded ? <>
      <path d="M13 9.50004L20 9.5M13 14.5L17 14.5M3.74009 16.5L6.24009 14L8.74009 16.5M8.74009 7.5L6.24009 10L3.74009 7.5" />
      <path d="M6.23999 4V9M6.23999 15V20" strokeWidth="1.8" />
    </> : <>
      <path d="M12.8 8L19.8 8M12.8 12L19.8 12.002M12.8 16L16.8 16M3.74022 7.50012L6.24022 5.00012L8.74022 7.50012M8.74022 16.5L6.24022 19L3.74022 16.5" />
      <path d="M6.24013 6V18" strokeWidth="1.8" />
    </>}
  </svg>;
}

function WordDiffIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.1" aria-hidden="true">
    <rect x="3" y="1.5" width="10" height="13" rx="2" /><path d="M5.5 5h5M8 2.5v5M5.5 11h5" />
  </svg>;
}

export type ChangesDiffMode = "unified" | "split" | "auto";
const nextMode: Record<ChangesDiffMode, ChangesDiffMode> = { unified: "split", split: "auto", auto: "unified" };

export interface ChangesToolbarActionsProps {
  width: number;
  mode: ChangesDiffMode;
  onModeChange: (mode: ChangesDiffMode) => void;
  wrap: boolean;
  onWrapChange: (enabled: boolean) => void;
  allExpanded: boolean;
  onToggleAll: () => void;
  onRefresh?: () => void;
  refreshDisabled?: boolean;
  refreshing?: boolean;
  filesVisible: boolean;
  onToggleFiles: () => void;
  onJumpToFile: (anchor: HTMLButtonElement) => void;
  jumpOpen?: boolean;
  onCopyGitApplyCommand?: () => void;
  copyGitApplyDisabled?: boolean;
}

function ToolbarButton({ label, onClick, selected, disabled, children }: {
  label: string; onClick: (event: MouseEvent<HTMLButtonElement>) => void; selected?: boolean; disabled?: boolean; children: ReactNode;
}) {
  return <Tip content={label}><button type="button" className="mc-changes-toolbar-action" aria-label={label}
    aria-pressed={selected} data-selected={selected || undefined} disabled={disabled} onClick={onClick}>{children}</button></Tip>;
}

/** Source Er: 625px changes controls; compact actions live inside its options menu. */
export function ChangesToolbarActions({ width, mode, onModeChange, wrap, onWrapChange, allExpanded, onToggleAll,
  onRefresh, refreshDisabled = false, refreshing = false, filesVisible, onToggleFiles, onJumpToFile, jumpOpen = false,
  onCopyGitApplyCommand, copyGitApplyDisabled = false }: ChangesToolbarActionsProps) {
  const { t, locale } = useI18n();
  const [loadFullFiles, setLoadFullFiles] = useDiffViewPreference("loadFullFiles");
  const [richPreview, setRichPreview] = useDiffViewPreference("richPreview");
  const [hideWhitespace, setHideWhitespace] = useDiffViewPreference("hideWhitespace");
  const [hideImports, setHideImports] = useDiffViewPreference("hideImports");
  const [wordDiffs, setWordDiffs] = useWordDiffsPreference();
  const [menuOpen, setMenuOpen] = useState(false);
  const optionsButton = useRef<HTMLButtonElement>(null);
  const wideGroup = useRef<HTMLDivElement>(null);
  const wide = width >= 625;
  const previousWide = useRef(wide);
  const wideFocus = useRef(false);
  const zh = locale === "zh-CN";
  const modeLabel = mode === "unified" ? (zh ? "切换到拆分差异视图" : "Switch to split diff")
    : mode === "split" ? (zh ? "将差异视图切换为自动布局" : "Switch to Auto diff")
      : (zh ? "自动差异视图：改用统一差异视图" : "Auto diff: switch to unified diff");
  const expandLabel = allExpanded ? (zh ? "折叠全部差异" : "Collapse all diffs") : (zh ? "展开全部差异" : "Expand all diffs");
  const filesLabel = filesVisible ? (zh ? "隐藏文件" : "Hide files") : (zh ? "显示文件" : "Show files");
  const jumpLabel = zh ? "跳转到文件" : "Jump to file";
  const modeIcon = mode === "auto" ? <AutoIcon /> : <SourceIcon asset={mode} />;
  const refreshIcon = refreshing ? <LoaderCircle size={16} className="animate-spin" /> : <SourceIcon asset="refresh" />;

  useLayoutEffect(() => {
    if (wide && !previousWide.current) setMenuOpen(false);
    if (!wide && previousWide.current && wideFocus.current) {
      const owner = wideGroup.current!.ownerDocument;
      if (owner.hasFocus() && (wideGroup.current!.contains(owner.activeElement) || owner.activeElement === owner.body)
        && optionsButton.current!.getClientRects().length > 0) optionsButton.current!.focus();
      wideFocus.current = false;
    }
    previousWide.current = wide;
  }, [wide]);

  const items: ContextMenuItem[] = [
    ...wide ? [] : [
      ...onRefresh ? [{ label: t("刷新"), icon: refreshIcon, disabled: refreshDisabled, onClick: onRefresh }] : [],
      { label: t("自动换行"), icon: <SourceIcon asset="wrap" />, checked: wrap, checkType: "checkbox" as const, onClick: () => onWrapChange(!wrap) },
      { label: modeLabel, icon: modeIcon, onClick: () => onModeChange(nextMode[mode]) },
      { label: expandLabel, icon: <ExpandIcon expanded={allExpanded} />, onClick: onToggleAll },
      { label: "", separator: true },
    ],
    { label: t("加载完整文件"), icon: <DiffSourceOptionIcon kind="document" />, checked: loadFullFiles, checkType: "checkbox", onClick: () => setLoadFullFiles(!loadFullFiles) },
    { label: t("渲染预览"), icon: <DiffSourceOptionIcon kind="photo" />, checked: richPreview, checkType: "checkbox", onClick: () => setRichPreview(!richPreview) },
    { label: t("词级差异"), icon: <WordDiffIcon />, checked: wordDiffs, checkType: "checkbox", onClick: () => setWordDiffs(!wordDiffs) },
    { label: t("隐藏空白字符"), icon: <DiffSourceOptionIcon kind="eye" />, checked: hideWhitespace, checkType: "checkbox", onClick: () => setHideWhitespace(!hideWhitespace) },
    { label: t("隐藏导入内容"), icon: <DiffSourceOptionIcon kind="cube" />, checked: hideImports, checkType: "checkbox", onClick: () => setHideImports(!hideImports) },
    ...onCopyGitApplyCommand ? [{ label: "", separator: true },
      { label: t("复制 git apply 命令"), icon: <Copy size={16} />, disabled: copyGitApplyDisabled, onClick: onCopyGitApplyCommand }] : [],
  ];

  return <div className="mc-changes-toolbar-actions" data-layout={wide ? "wide" : "compact"}>
    <Tip content={t("“变更”选项")}><button ref={optionsButton} type="button" className="mc-changes-toolbar-action"
      aria-label={t("“变更”选项")} aria-haspopup="menu" aria-expanded={menuOpen} aria-busy={refreshing}
      onClick={() => setMenuOpen(open => !open)}>{refreshing && !wide ? refreshIcon : <SourceIcon asset="ellipsis" />}</button></Tip>
    {menuOpen && <ContextMenu ariaLabel={t("“变更”选项")} anchor={optionsButton.current!} returnFocusTo={optionsButton.current!}
      position={{ x: 0, y: 0 }} onClose={() => setMenuOpen(false)} items={items} />}
    <Tip content={jumpLabel}><button type="button" className="mc-changes-toolbar-action" aria-label={jumpLabel}
      aria-haspopup="dialog" aria-expanded={jumpOpen} onClick={event => onJumpToFile(event.currentTarget)}><SourceIcon asset="jump" /></button></Tip>
    <div ref={wideGroup} className="mc-changes-toolbar-wide" hidden={!wide}
      onFocusCapture={event => { wideFocus.current = event.target.matches(":focus-visible"); }}
      onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget)) wideFocus.current = false; }}>
      {onRefresh && <ToolbarButton label={t("刷新")} disabled={refreshDisabled} onClick={onRefresh}>{refreshIcon}</ToolbarButton>}
      <ToolbarButton label={t("自动换行")} selected={wrap} onClick={() => onWrapChange(!wrap)}><SourceIcon asset="wrap" /></ToolbarButton>
      <ToolbarButton label={expandLabel} onClick={onToggleAll}><ExpandIcon expanded={allExpanded} /></ToolbarButton>
      <ToolbarButton label={modeLabel} onClick={() => onModeChange(nextMode[mode])}>{modeIcon}</ToolbarButton>
    </div>
    <ToolbarButton label={filesLabel} selected={filesVisible} onClick={onToggleFiles}><SourceIcon asset="files" /></ToolbarButton>
  </div>;
}
