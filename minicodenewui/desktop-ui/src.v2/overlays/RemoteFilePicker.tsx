import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { ArrowUp, Check, File, Folder, X } from "../lib/icons";
import type { RemoteDirectoryEntries } from "../desktop/runtime";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { AppLanguageBridge, useI18n } from "../i18n";
import { parentDirectory } from "./RemoteDirectoryPicker";
import "./RemoteDirectoryPicker.css";

interface PickerOptions {
  initialDirectoryPath: string;
  platform: string;
  listDirectory(path: string | null): Promise<RemoteDirectoryEntries>;
  signal: AbortSignal;
}

export function showRemoteFilePicker({ signal, ...options }: PickerOptions): Promise<string[] | null> {
  if (signal.aborted) return Promise.resolve(null);
  return new Promise(resolve => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const finish = (paths: string[] | null) => {
      signal.removeEventListener("abort", abort);
      root.unmount(); container.remove(); resolve(paths);
    };
    const abort = () => finish(null);
    signal.addEventListener("abort", abort, { once: true });
    root.render(<AppLanguageBridge><RemoteFilePicker {...options} onResult={finish} /></AppLanguageBridge>);
  });
}

export function RemoteFilePicker({ initialDirectoryPath, platform, listDirectory, onResult }: Omit<PickerOptions, "signal"> & { onResult(paths: string[] | null): void }) {
  const { t: tr } = useI18n();
  const focus = useFocusTrap(true);
  const [directory, setDirectory] = useState(initialDirectoryPath);
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);
  const [data, setData] = useState<RemoteDirectoryEntries>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let current = true;
    setLoading(true); setError("");
    void listDirectory(directory || null).then(result => {
      if (current) setData(result);
    }, reason => {
      if (current) { setData(undefined); setError(reason instanceof Error ? reason.message : String(reason)); }
    }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [directory, listDirectory]);
  const currentPath = directory || data?.directoryPath || "";
  const entries = data?.directoryPath === currentPath ? data.entries : [];
  const parent = data?.directoryPath === currentPath ? data.parentPath : parentDirectory(currentPath, platform);
  const toggle = (path: string) => setSelectedPaths(current => current.includes(path) ? current.filter(value => value !== path) : [...current, path]);
  const dismiss = () => onResult(null);
  return <div className="mc-dialog-backdrop remote-directory-backdrop" ref={focus}
    onMouseDown={event => { if (event.target === event.currentTarget) dismiss(); }}>
    <form className="mc-dialog-panel remote-directory-dialog remote-file-dialog" role="dialog" aria-modal="true" aria-label={tr("选择远程文件")}
      onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); dismiss(); } }}
      onSubmit={event => { event.preventDefault(); event.stopPropagation(); if (selectedPaths.length) onResult(selectedPaths); }}>
      <header><h3>{tr("选择远程文件")}</h3><button type="button" className="mc-icon-btn" aria-label={tr("关闭")} onClick={dismiss}><X size={16} /></button></header>
      <div className="remote-directory-body">
        <div className="remote-directory-path">
          <button type="button" className="mc-icon-btn" title={tr("转到上一级文件夹")} aria-label={tr("转到上一级文件夹")}
            disabled={loading || parent === null} onClick={() => setDirectory(parent!)}><ArrowUp size={16} /></button>
          <input aria-label={tr("当前文件夹路径")} value={currentPath} title={currentPath} disabled readOnly spellCheck={false} />
        </div>
        <div className="remote-directory-contents remote-file-contents">
          {loading ? <p role="status">{tr("正在加载文件夹内容…")}</p>
            : error ? <p role="alert" title={error}>{tr("无法加载此文件夹")}</p>
              : entries.length === 0 ? <p>{tr("此目录中没有文件或文件夹。")}</p>
                : <div>{entries.map(entry => entry.type === "directory"
                  ? <button type="button" className="remote-file-entry" key={entry.path} onClick={() => setDirectory(entry.path)}><Folder size={16} aria-hidden="true" /><span>{entry.name}</span></button>
                  : <button type="button" className="remote-file-entry" key={entry.path} aria-pressed={selectedPaths.includes(entry.path)}
                    onClick={() => toggle(entry.path)}><File size={16} aria-hidden="true" /><span>{entry.name}</span>{selectedPaths.includes(entry.path) && <Check size={16} aria-hidden="true" />}</button>)}</div>}
        </div>
      </div>
      <footer><button type="button" className="mc-dialog-btn" onClick={dismiss}>{tr("取消")}</button>
        <button type="submit" className="mc-dialog-btn" data-primary="true" disabled={!selectedPaths.length}>{tr("选择文件")}</button></footer>
    </form>
  </div>;
}
