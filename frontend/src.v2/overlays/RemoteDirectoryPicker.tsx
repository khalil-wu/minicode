import { useEffect, useId, useState } from "react";
import { createRoot } from "react-dom/client";
import { ArrowUp, Folder, LoaderCircle, X } from "lucide-react";
import type { RemoteDirectoryEntries } from "../desktop/runtime";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { AppLanguageBridge, useI18n } from "../i18n";
import "./RemoteDirectoryPicker.css";

interface PickerOptions {
  initialDirectoryPath: string;
  platform: string;
  listDirectory(path: string | null): Promise<RemoteDirectoryEntries>;
}

function isAbsolutePath(path: string, platform: string): boolean {
  return platform === "win32" ? /^(?:[a-z]:[\\/]|[\\/]{2}[^\\/]+[\\/][^\\/]+)/i.test(path) : path.startsWith("/");
}

function parentDirectory(path: string, platform: string): string | null {
  const windows = platform === "win32";
  const normalized = (windows ? path.replace(/\\/g, "/") : path).replace(/\/+$/, "");
  if (!normalized || windows && (/^[a-z]:$/i.test(normalized) || /^\/\/[^/]+\/[^/]+$/.test(normalized))) return null;
  const slash = normalized.lastIndexOf("/");
  const parent = slash === 0 ? "/" : normalized.slice(0, slash);
  const result = windows && /^[a-z]:$/i.test(parent) ? `${parent}/` : parent;
  return windows && path.includes("\\") ? result.replace(/\//g, "\\") : result;
}

function browsingPath(path: string, platform: string): { directoryPath: string; prefix: string | null } {
  const windows = platform === "win32";
  if ((windows ? /[\\/]$/ : /\/$/).test(path)) return {
    directoryPath: /^(?:\/|[a-z]:[\\/])$/i.test(path) ? path : path.replace(windows ? /[\\/]+$/ : /\/+$/, ""), prefix: null,
  };
  const parent = parentDirectory(path, platform);
  return parent === null ? { directoryPath: path, prefix: null }
    : { directoryPath: parent, prefix: (windows ? path.replace(/\\/g, "/") : path).split("/").at(-1)! };
}

export function showRemoteDirectoryPicker(options: PickerOptions): Promise<string | null> {
  return new Promise(resolve => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    root.render(<AppLanguageBridge><RemoteDirectoryPicker {...options} onResult={path => {
      root.unmount(); container.remove(); resolve(path);
    }} /></AppLanguageBridge>);
  });
}

export function RemoteDirectoryPicker({ initialDirectoryPath, platform, listDirectory, onResult }: PickerOptions & { onResult(path: string | null): void }) {
  const { t: tr } = useI18n();
  const focus = useFocusTrap(true);
  const listId = useId();
  const [directory, setDirectory] = useState(initialDirectoryPath);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [data, setData] = useState<RemoteDirectoryEntries>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [activePath, setActivePath] = useState<string | null>(null);
  const validInput = !editing || isAbsolutePath(text.trim(), platform);
  const requested = editing && validInput ? browsingPath(text.trim(), platform) : { directoryPath: directory, prefix: null };
  const queryPath = requested.directoryPath;
  useEffect(() => {
    if (!validInput) { setLoading(false); return; }
    let current = true;
    setLoading(true); setError("");
    void listDirectory(queryPath || null).then(result => {
      if (current) setData(result);
    }, (reason: Error) => { if (current) { setData(undefined); setError(reason.message); } })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [listDirectory, queryPath, validInput]);
  const currentPath = data?.directoryPath ?? directory;
  const inputValue = editing ? text : selectedPath ?? currentPath;
  const entries = validInput && data?.directoryPath === queryPath ? data.entries.filter(entry => entry.type === "directory"
    && (requested.prefix === null || entry.name.toLocaleLowerCase().startsWith(requested.prefix.toLocaleLowerCase()))) : [];
  const activeEntry = entries.find(entry => entry.path === activePath);
  const parent = validInput && data?.directoryPath === queryPath && (!editing || inputValue === queryPath)
    ? selectedPath !== null && entries.some(entry => entry.path === selectedPath) ? queryPath : data.parentPath
    : parentDirectory(inputValue, platform);
  const candidate = selectedPath ?? currentPath;
  const open = (path: string, selection = path) => {
    setDirectory(path); setSelectedPath(selection); setEditing(false); setText(""); setActivePath(null);
  };
  const commitInput = () => { if (isAbsolutePath(text.trim(), platform)) open(text.trim()); };
  const dismiss = () => { if (!saving) onResult(null); };
  return <div className="mc-dialog-backdrop remote-directory-backdrop" ref={focus}
    onMouseDown={event => { if (event.target === event.currentTarget) dismiss(); }}>
    <form className="mc-dialog-panel remote-directory-dialog" role="dialog" aria-modal="true" aria-label={tr("选择源文件夹")}
      onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); dismiss(); } }}
      onSubmit={event => {
        event.preventDefault();
        if (saving || !isAbsolutePath(candidate.trim(), platform)) return;
        setSaving(true); setError("");
        void listDirectory(candidate.trim()).then(result => onResult(result.directoryPath), (reason: Error) => {
          setError(reason.message); setSaving(false);
        });
      }}>
      <header><h3>{tr("选择源文件夹")}</h3><button type="button" className="mc-icon-btn" aria-label={tr("关闭")} disabled={saving} onClick={dismiss}><X size={16} /></button></header>
      <div className="remote-directory-body" onBlur={event => {
        if (editing && !event.currentTarget.contains(event.relatedTarget as Node)) commitInput();
      }}>
        <div className="remote-directory-path">
          <button type="button" className="mc-icon-btn" title={tr("转到上一级文件夹")} aria-label={tr("转到上一级文件夹")}
            disabled={saving || loading || !validInput || parent === null} onClick={() => open(parent!)}><ArrowUp size={16} /></button>
          <input aria-label={tr("已选文件夹路径")} role="combobox" aria-autocomplete="list" aria-controls={listId}
            aria-expanded={entries.length > 0} aria-activedescendant={activeEntry ? `${listId}-${encodeURIComponent(activeEntry.path)}` : undefined}
            value={inputValue} disabled={saving} autoFocus onChange={event => {
              setText(event.target.value); setSelectedPath(event.target.value); setEditing(true); setActivePath(null);
            }} onKeyDown={event => {
              if (event.nativeEvent.isComposing) return;
              if ((event.key === "ArrowDown" || event.key === "ArrowUp") && entries.length && !loading) {
                event.preventDefault();
                const index = entries.findIndex(entry => entry.path === activePath);
                const next = index === -1 ? (event.key === "ArrowDown" ? 0 : entries.length - 1)
                  : (index + (event.key === "ArrowDown" ? 1 : -1) + entries.length) % entries.length;
                setActivePath(entries[next].path);
              } else if (event.key === "Enter" && activeEntry && !loading) {
                event.preventDefault(); event.stopPropagation();
                open(queryPath, activeEntry.path);
              }
            }} />
        </div>
        {selectedPath === null && <button type="button" className="remote-directory-current" disabled={saving || loading || !validInput || Boolean(error) || !data}
          onClick={() => setSelectedPath(currentPath)}>{tr("选择当前文件夹")}</button>}
        <div className="remote-directory-contents">
          {!validInput ? <p>{tr("输入完整的文件夹路径以浏览")}</p> : loading ? <p role="status">{tr("正在加载文件夹内容…")}</p>
            : error ? <p role="alert" title={error}>{tr("无法加载此文件夹")}</p>
              : entries.length === 0 ? <p>{tr("此目录中没有文件夹。")}</p>
                : <div role="listbox" id={listId} aria-label={tr("选择源文件夹")}>{entries.map(entry => <button type="button" role="option"
                  id={`${listId}-${encodeURIComponent(entry.path)}`} key={entry.path} aria-selected={selectedPath === entry.path}
                  data-active={activePath === entry.path || selectedPath === entry.path || undefined} disabled={saving || loading}
                  onClick={() => open(entry.path)}><Folder size={16} aria-hidden="true" /><span>{entry.name}</span></button>)}</div>}
        </div>
      </div>
      <footer><button type="button" className="mc-dialog-btn" disabled={saving} onClick={dismiss}>{tr("取消")}</button>
        <button type="submit" className="mc-dialog-btn" data-primary="true" disabled={saving || !isAbsolutePath(candidate.trim(), platform)}>
          {saving && <LoaderCircle size={14} className="remote-directory-spinner" aria-hidden="true" />}{tr("使用文件夹")}</button></footer>
    </form>
  </div>;
}
