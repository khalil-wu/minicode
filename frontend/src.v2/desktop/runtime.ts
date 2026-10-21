import { pushToast } from "../overlays/ToastContainer";
import { workspaceHasConversation, workspaceRequestOwner, workspaceRequestPath, workspaceViewRoot } from "../protocol/workspace-owner";
import type { DictationPreferences } from "../lib/workbench-preferences";
import type { UploadResponse } from "../protocol/api";

export interface FsEntry {
  name: string;
  path: string;
  isDirectory: boolean;
  sizeBytes?: number;
  modifiedAt?: string;
}

export interface FsSearchResult {
  path: string;
  name: string;
  score?: number;
  kind?: "file" | "folder";
}

export interface FsListTreeResult {
  workspaceRoot: string;
  requestedPath: string;
  entries: FsEntry[];
}

export interface FsFileResponse {
  path?: string;
  content: string;
  contentHash?: string;
  content_hash?: string;
  sizeBytes?: number;
  size_bytes?: number;
  modifiedAt?: string;
  modified_at?: string;
  readOnly?: boolean;
  read_only?: boolean;
  encoding?: "utf-8" | "utf-8-bom" | "utf-16le" | "utf-16be" | "gb18030" | string;
}

export type FsDeletePathResult =
  | { needsConfirmation: true; path: string; entryCount: number }
  | { deleted: true; path: string; is_dir?: boolean; isDirectory?: boolean };

export interface PtySession {
  sessionId: string;
  conversationId: string;
  pid?: number;
  shell: string;
  cwd: string;
  output?: string;
  outputChars?: number;
  totalOutputChars?: number;
  outputStartCursor?: number;
  outputEndCursor?: number;
  truncated?: boolean;
  isAlive?: boolean;
  exitCode?: number | null;
  exitSignal?: number | string | null;
  exitedAt?: number;
  terminalMode: "pty";
}

export interface DesktopEnvInfo {
  git: boolean;
  python: boolean;
  node: boolean;
  docker: boolean;
  ollama: boolean;
  home: string;
}

export interface DesktopSandboxSetupResult {
  ok: boolean;
  cancelled?: boolean;
  error?: string;
}

export interface DesktopPet {
  id: string;
  displayName: string;
  description: string;
  assetRef?: string;
  spriteVersionNumber: number;
  updatedAt: number;
}

export interface DesktopPetTask {
  id: string;
  title: string;
  status: "running" | "waiting" | "failed" | "review";
  detail?: string;
  turnKey?: string;
  updatedAt?: number;
  sortAt?: number;
  actions?: Array<{ id: string; label: string; openInMainWindow?: boolean }>;
  followUpAllowed?: boolean;
}

export interface DesktopPetActivity {
  running: boolean;
  waiting: boolean;
  failed: boolean;
  review: boolean;
  tasks: DesktopPetTask[];
}

export interface DesktopPetState {
  pets: DesktopPet[];
  selectedPetId: string;
  size: number;
  petVisible: boolean;
  miniOpen: boolean;
  petDirectory: string;
  reducedMotion: boolean;
  mutedConversationIds: string[];
  activity: DesktopPetActivity;
  issues: string[];
}

export type DesktopMenuKey = "file" | "edit" | "view" | "help";

export interface BrowserTargetInfo {
  id: string;
  type: string;
  title: string;
  url: string;
  attached?: boolean;
  faviconUrl?: string;
  devtoolsFrontendUrl?: string;
  webSocketDebuggerUrl?: string;
}

export interface SystemFontFace {
  family: string;
  fullName: string;
  postscriptName: string;
  styleName: string;
  weight: number;
  style: "normal" | "italic" | "oblique";
  isMonospaced: boolean;
}
export interface SystemFontFamily { family: string; faces: SystemFontFace[]; }
export type BrowserSiteCategory = "camera" | "microphone" | "geolocation" | "notifications" | "clipboard-read" | "mediaKeySystem" | "sound" | "images" | "usb" | "javascript";
export type BrowserSiteDecision = "ask" | "allow" | "block";
export interface BrowserSitePolicy {
  categories: Array<{ id: BrowserSiteCategory; allowedSettings: BrowserSiteDecision[]; defaultSettings: BrowserSiteDecision[] }>;
  defaults: Record<BrowserSiteCategory, BrowserSiteDecision>;
  sites: Array<{ origin: string; settings: Partial<Record<BrowserSiteCategory, BrowserSiteDecision>> }>;
  devices: Array<{ id: string; origin: string; name: string }>;
}

export interface BrowserDiscoveryResult {
  status: "connected" | "error";
  endpoint: string;
  browser: string;
  protocolVersion: string;
  userAgent: string;
  webSocketDebuggerUrl: string;
  targets: BrowserTargetInfo[];
  error?: string;
}

export interface BrowserScreenshotResult {
  endpoint: string;
  targetId: string;
  title: string;
  url: string;
  mimeType: string;
  data: string;
  width?: number;
  height?: number;
  capturedAt: number;
}

export interface BrowserActionResult {
  action: "navigate" | "click" | "type";
  endpoint: string;
  targetId: string;
  title: string;
  url: string;
  screenshot: BrowserScreenshotResult;
}

export interface EmbeddedBrowserState {
  id: string;
  conversationId: string;
  conversation_id?: string;
  type: "page" | "loading" | "updated" | "error" | "new-tab-request" | "closed" | "presentation-requested" | "find-result" | "find-request" | "print-request" | "numbered-shortcut" | "external-link-request";
  url: string;
  title: string;
  faviconUrl?: string;
  loading: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
  active?: boolean;
  error?: string;
  requestedUrl?: string;
  findResult?: { requestId: number; matches: number; activeMatchOrdinal: number; finalUpdate: boolean };
  findDirection?: "next" | "previous";
  numberedShortcut?: { target: "tabs" | "sidebar"; index: number };
  zoomFactor?: number;
}

export interface EmbeddedBrowserBounds {
  viewport?: { width: number; height: number; mobile: boolean; scale?: number } | null;
  id: string;
  conversationId: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface EmbeddedBrowserInspectionResult {
  ok: boolean;
  action?: string;
  value?: unknown;
  error?: string;
}

export type BrowserDownload = { id: string; filename: string; url: string; path: string; state: string; receivedBytes: number; totalBytes: number; startedAt: number; canPause: boolean; canResume: boolean; canCancel: boolean; fileExists: boolean };
export type BrowserPageCommand = { id: string; conversationId: string } & (
  | { action: "find"; text: string; forward?: boolean; findNext?: boolean }
  | { action: "zoom"; zoomFactor?: number }
  | { action: "print" | "screenshot" | "site-info" }
);
export type BrowserDataType = "cookies" | "siteData" | "cache" | "downloads" | "history";
export interface ImportableBrowserProfile {
  source: "chrome";
  appName: string;
  profileName: string;
  profileDirectoryName: string;
  profilePath: string;
  rootPath: string;
  hasHistory: boolean;
}
export interface BrowserProfileImportRequest {
  source: ImportableBrowserProfile["source"];
  profilePath: string;
  importHistory: true;
  importCookies: false;
  importPasswords: false;
  importExtensions: false;
}
export interface BrowserProfileImportResult {
  source: ImportableBrowserProfile["source"];
  profilePath: string;
  history: { status: "completed"; discovered: number; imported: number; skippedExisting: number; skippedInvalid: number; failed: number };
}

export interface EmbeddedBrowserSettings {
  downloadDirectory?: string | null;
  browserEnabled?: boolean;
  deviceToolbarAlwaysOnNewTabs?: boolean;
  downloadPolicy: "block" | "ask" | "allow";
  origin: string;
  permissions: string[];
}

export interface BrowserSavedPassword { id: string; origin: string; username: string; password: string }
export type BrowserPasswordSummary = Omit<BrowserSavedPassword, "password">;
export interface BrowserPersonalDataState {
  available: boolean;
  passwordAutofillEnabled: boolean;
  contactAutofillEnabled: boolean;
  items: BrowserPasswordSummary[];
}
export interface BrowserContact {
  id: string;
  fullName: string;
  organization: string;
  addressLine1: string;
  addressLine2: string;
  city: string;
  region: string;
  postalCode: string;
  country: string;
  email: string;
  phone: string;
}
export interface BrowserSitePermissions { origin: string; permissions: string[] }
export type NotificationSound = "default" | "classic" | "none" | { fileName: string; name: string };

export interface EmbeddedBrowserSettingsSnapshot {
  downloadDirectory?: string | null;
  browserEnabled: boolean;
  deviceToolbarAlwaysOnNewTabs: boolean;
  downloadPolicy: EmbeddedBrowserSettings["downloadPolicy"];
  sitePermissions: Record<string, string[]>;
}

export interface UpdateActivitySnapshot {
  runtimeReady: boolean;
  activeTurns: string[];
  sideChatStreams: string[];
  pendingPrompts: string[];
  uploadingAttachments: string[];
  dirtyEditors: string[];
  backgroundTasks: string[];
  pendingDrafts: string[];
  draftStates: string[];
}

export interface UpdatePreflightCheck {
  code: string;
  severity: "pass" | "warning" | "blocking";
  message: string;
  details?: Record<string, unknown>;
}

export interface UpdatePreflightResult {
  allowed: boolean;
  checks: UpdatePreflightCheck[];
  fingerprint: string;
  version: string;
}

export interface UpdateInstallResult {
  installed: boolean;
  reason?: string;
  message?: string;
  preflight?: UpdatePreflightResult;
}

export interface UpdateStatus {
  status: string;
  sequence?: number;
  version?: string;
  previousVersion?: string;
  failedVersion?: string;
  percent?: number;
  message?: string;
}

export type FileOpenTargetId = "minicode" | "system" | "vscode" | "vscodeInsiders" | "visualStudio" | "cursor" | "sublimeText" | "zed" | "antigravity" | "githubDesktop" | "fileManager" | "terminal" | "gitBash" | "cmder" | "wsl" | "androidStudio" | "intellij" | "rider" | "pycharm" | "webstorm" | "phpstorm";
export interface FileOpenTarget {
  id: FileOpenTargetId;
  label: string;
  kind: "editor" | "terminal" | "fileManager" | "systemDefault";
  icon?: string;
  hidden?: boolean;
}
export interface OpenPreferredPathOptions {
  targetId?: FileOpenTargetId;
  workspaceRoot?: string;
  persistPreferredTargetPath?: string;
  column?: number;
}
export type ManagedSettingsTarget = { kind: "config" | "data"; workspaceRoot?: string }
  | { kind: "hook"; hookKey: string; workspaceRoot?: string };

export interface DesktopPreferences extends DictationPreferences {
  autoArchiveAfterDays: 0 | 1 | 7 | 14 | 30 | 90;
  localHistoryCompression: boolean;
  notificationSound: NotificationSound;
  defaultTaskDirectory: string;
  readonly taskDirectoryOverride: string | null;
  defaultPermissionMode: "confirm" | "plan" | "auto" | "bypass";
  showFullAccess: boolean;
  showContextWindowUsage: boolean;
  showBottomPanel: boolean;
  defaultTerminalLocation: "bottom" | "right";
  composerPlainTextMode: boolean;
  primaryNumberShortcutTarget: "tabs" | "sidebar";
  externalBrowserLinkShortcut: "unassigned" | "primary" | "alt" | "primaryShift";
  localeOverride: "zh-CN" | "en-US" | null;
  systemLocale: string;
  fileOpenTarget: FileOpenTargetId;
  fileOpenTargetPerPath: Record<string, FileOpenTargetId>;
  fileOpenTargets: FileOpenTarget[];
  terminalShell: "powershell" | "pwsh" | "cmd" | "gitBash" | "wsl" | "system";
  terminalShellTargets: Array<{ id: DesktopPreferences["terminalShell"]; label: string }>;
  confirmBeforeClose: "never" | "keyboardOnly" | "always";
  turnCompletionNotifications: "never" | "onlyWhenUnfocused" | "always";
  permissionNotifications: boolean;
  questionNotifications: boolean;
  urlOpenTarget: "in-app-browser" | "external-browser";
  localUrlOpenTarget: "in-app-browser" | "external-browser";
  showFullUrl: boolean;
  annotationScreenshotsMode: "always" | "necessary";
}
export type DesktopPreferencesPatch = Partial<Omit<DesktopPreferences,
  "fileOpenTargets" | "fileOpenTargetPerPath" | "terminalShellTargets" | "systemLocale" | "taskDirectoryOverride" | "defaultTaskDirectory">
  & { defaultTaskDirectory: string | null }>;
export interface BrowserHistoryVisit { id: string; url: string; title: string; visitTime: number; source: "agent" | "other" }
export interface BrowserCaptureRequest { id: string; conversationId: string; expectedUrl: string; rect?: { x: number; y: number; width: number; height: number }; viewport?: { width: number; height: number } }
export interface BrowserCaptureImage { data: string; mimeType: "image/png"; width: number; height: number; url: string }

export type RemoteDesktopConnection = { id: string; displayName: string; kind: "ssh" | "wsl"; source?: "manual" | "discovered"; alias?: string; sshHost?: string; sshPort?: number | null; identity?: string | null; distribution?: string; appServerVersion?: string; status?: "connecting" | "connected" | "disconnected" };

export interface RemoteDirectoryEntries {
  directoryPath: string;
  parentPath: string | null;
  entries: Array<{ name: string; path: string; type: "directory" | "file" }>;
}

export interface WindowAppearanceState {
  supportsTranslucency: boolean;
  opaque: boolean;
  theme: "light" | "dark";
}

export interface DictationHotkeyState { available: boolean; holdHotkey: string; toggleHotkey: string; error: string }
export interface NativeDictationEvent { action: "start" | "stop" | "cancel"; token: string; targetKind: "composer" | "external" }

interface MiniCodeDesktop {
  dictation?: {
    getLocalRuntime(): Promise<{ apiBaseUrl: string; runtimeToken: string; revision: number }>;
    openProviderSettings(): Promise<true>;
    consumeProviderSettings(): Promise<boolean>;
    migratePreferences(legacy: DictationPreferences): Promise<DesktopPreferences>;
    get(): Promise<DictationHotkeyState>;
    configure(patch: Partial<Pick<DictationHotkeyState, "holdHotkey" | "toggleHotkey">>): Promise<DictationHotkeyState>;
    suspend(value: boolean): Promise<void>;
    insert(token: string, text: string): Promise<boolean>;
    complete(token: string): Promise<void>;
    onEvent(callback: (event: NativeDictationEvent) => void): () => void;
    onState(callback: (state: DictationHotkeyState) => void): () => void;
  };
  mcpAppSandbox: {
    prepare(frameUrl: string): Promise<{ id: string; src: string; partition: string }>;
    release(id: string): Promise<void>;
    clearData(): Promise<void>;
  };
  browserPersonalData: {
    listPasswords(): Promise<BrowserPersonalDataState>;
    getPassword(request: { id: string }): Promise<BrowserSavedPassword>;
    savePassword(request: Omit<BrowserSavedPassword, "id"> & { id?: string }): Promise<void>;
    removePassword(request: { id: string }): Promise<void>;
    listContacts(): Promise<BrowserContact[]>;
    saveContact(request: Omit<BrowserContact, "id"> & { id?: string }): Promise<void>;
    removeContact(request: { id: string }): Promise<void>;
    setAutofillSettings(patch: { passwordAutofillEnabled?: boolean; contactAutofillEnabled?: boolean }): Promise<BrowserPersonalDataState>;
  };
  pets: {
    getState(): Promise<DesktopPetState>;
    refresh(): Promise<DesktopPetState>;
    update(patch: Partial<Pick<DesktopPetState, "selectedPetId" | "size" | "petVisible" | "reducedMotion">>): Promise<DesktopPetState>;
    toggleMini(): Promise<DesktopPetState>;
    setNotificationMuted(request: { conversationId: string; muted: boolean }): Promise<DesktopPetState>;
    openFolder(): Promise<void>;
    getSprite(id: string): Promise<string>;
    publishActivity(activity: DesktopPetActivity): Promise<void>;
    onActionRequested(callback: (request: { id: string; taskId: string } & ({ kind: "action"; actionId: string } | { kind: "followup"; prompt: string })) => void): () => void;
    completeAction(result: { id: string; error?: string }): Promise<void>;
    onStateChanged(callback: (state: DesktopPetState) => void): () => void;
  };
  connections?: {
    list(): Promise<RemoteDesktopConnection[]>;
    discover(kind: "ssh" | "wsl"): Promise<{ sshAliases: string[]; wslDistributions: string[] }>;
    save(connection: Omit<RemoteDesktopConnection, "status">): Promise<RemoteDesktopConnection | null>;
    saveMany(connections: Array<Omit<RemoteDesktopConnection, "status">>): Promise<RemoteDesktopConnection[]>;
    remove(id: string): Promise<void>;
    connect(id: string): Promise<void>;
    disconnect(id: string): Promise<void>;
    open(id: string): Promise<void>;
    onChanged(callback: (connections: RemoteDesktopConnection[]) => void): () => void;
  };
  preferences: {
    getStartupStatus(): Promise<boolean | null>;
    openStartupSettings(): Promise<void>;
    getOpenSourceLicenses(): Promise<string>;
    pickNotificationSound(): Promise<{ name: string; bytes: number[] } | null>;
    importNotificationSound(payload: { name: string; wav: number[] }): Promise<DesktopPreferences>;
    previewNotificationSound(sound: NotificationSound): Promise<void>;
    chooseTaskDirectory(): Promise<DesktopPreferences | null>;
    get(): Promise<DesktopPreferences>;
    set(patch: DesktopPreferencesPatch): Promise<DesktopPreferences>;
    onChanged(callback: (preferences: DesktopPreferences) => void): () => void;
  };
  openPreferredPath(target: string, line?: number, options?: OpenPreferredPathOptions): Promise<{ openInEditor: boolean; path: string }>;
  saveFile(filename: string, bytes: Uint8Array): Promise<boolean>;
  attachments: {
    importRemoteFile(request: { path: string; sessionId: string; conversationId: string; workspaceRoot: string }): Promise<UploadResponse>;
  };
  platformInfo: { isDesktop: boolean; platform: string; arch: string };
  windowControls: {
    listFontFamilies(): Promise<SystemFontFamily[] | null>;
    listFonts(): Promise<string[]>;
    consumeInitialNavigation(): boolean;
    openConversation(conversationId: string): Promise<{ windowOwnerId: string; windowInstanceId: string }>;
    getState(): Promise<{ isMaximized: boolean; isFocused: boolean }>;
    getAppearance(): Promise<WindowAppearanceState>;
    setAppearance(options: { themeMode: "system" | "light" | "dark"; opaqueWindows: boolean }): Promise<WindowAppearanceState>;
    onAppearanceChanged(callback: (state: WindowAppearanceState) => void): () => void;
    onStateChanged(callback: (state: { isMaximized: boolean; isFocused: boolean }) => void): () => void;
    minimize(): Promise<void>;
    maximize(): Promise<void>;
    close(): Promise<void>;
    captureUnloadIntent(): { id: string; action: "close" | "reload"; ignoreCache: boolean };
    resumeUnload(id: string): Promise<boolean>;
  };
  notify(payload: {
    kind: "completion" | "permission" | "question";
    title: string;
    body: string;
    target?: { kind: "conversation"; conversationId: string };
    requestId?: string;
    conversationVisible?: boolean;
    turnId?: string;
    messageId?: string;
    taskId?: string;
  }): Promise<void>;
  hideNotifications(selector: { conversationId: string; requestId?: string; onlyIfFocused?: boolean; turnId?: string; messageId?: string; taskId?: string }): Promise<number>;
  onDeepLink(callback: (payload: {
    id: string;
    target: { kind: "conversation"; conversationId: string } | { kind: "url"; url: string };
  }) => void): (() => void) | void;
  ackDeepLink(id: string): Promise<boolean>;
  updates: {
    check(): Promise<boolean>;
    download(): Promise<boolean>;
    reportActivity(snapshot: UpdateActivitySnapshot, requestIds?: string[]): Promise<{ accepted: boolean; revision: number }>;
    onActivityRequest(callback: (payload: { requestId?: string }) => void): (() => void) | void;
    preflight(): Promise<UpdatePreflightResult>;
    install(payload: { fingerprint: string }): Promise<UpdateInstallResult>;
    getStatus(): Promise<UpdateStatus>;
    onStatus(callback: (payload: UpdateStatus) => void): (() => void) | void;
  };
  pickDirectory(): Promise<string | null>;
  pickWorkspaceDirectory(): Promise<string | null>;
  workspaceDirectoryEntries(directoryPath: string | null, directoriesOnly: boolean): Promise<RemoteDirectoryEntries>;
  trustWorkspace(path: string): Promise<string | null>;
  openExternal(target: string): Promise<boolean>;
  openPath(target: string): Promise<boolean>;
  revealPath(target: string): Promise<void>;
  settings: { openTarget(request: ManagedSettingsTarget): Promise<true> };
  skills: {
    reveal(request: { path: string; workspaceRoot: string }): Promise<boolean>;
  };
  diagnostics: { export(): Promise<unknown> };
  fs: {
    listTree(path: string): Promise<FsEntry[] | {
      workspace_root?: string;
      workspaceRoot?: string;
      requested_path?: string;
      requestedPath?: string;
      entries?: unknown[];
    }>;
    searchFiles(rootPath: string, query: string, limit?: number): Promise<FsSearchResult[]>;
    searchFilesByKind?(rootPath: string, query: string, limit?: number, kind?: "file" | "folder" | "all"): Promise<FsSearchResult[]>;
    readFile(path: string): Promise<FsFileResponse>;
    deletePath(path: string, recursive?: boolean, confirm?: boolean): Promise<FsDeletePathResult>;
  };
  pty: {
    spawn(cwd: string | undefined, conversationId?: string): Promise<{ sessionId?: string; session_id?: string; conversationId?: string; conversation_id?: string; pid?: number; shell?: string; cwd?: string }>;
    write(sessionId: string, data: string, conversationId: string): Promise<boolean>;
    resize(sessionId: string, cols: number, rows: number, conversationId: string): Promise<void>;
    kill(sessionId: string, conversationId: string): Promise<boolean>;
    restart(sessionId: string, conversationId: string): Promise<Record<string, unknown> | null>;
    killConversation(conversationId: string, preserveHistory?: boolean): Promise<number>;
    list(conversationId?: string): Promise<Record<string, unknown>[]>;
    snapshot(sessionId: string, maxChars: number | undefined, conversationId: string): Promise<Record<string, unknown> | null>;
    clear(sessionId: string, conversationId: string): Promise<{ cleared?: boolean; outputCursor?: number }>;
    ackExit(sessionId: string, conversationId: string): Promise<boolean>;
    onData(cb: (data: { sessionId: string; conversationId: string; data: string; startCursor?: number; endCursor?: number }) => void): (() => void) | void;
    onExit(cb: (data: { sessionId: string; conversationId: string; exitCode: number | null; exitSignal?: number | string | null; exitedAt?: number }) => void): (() => void) | void;
  };
  env: {
    detect(): Promise<Partial<DesktopEnvInfo>>;
  };
  sandbox: {
    setup(): Promise<DesktopSandboxSetupResult>;
  };
  menu: {
    popup(key: DesktopMenuKey): Promise<void>;
    execute(action: string): Promise<boolean>;
    setState(state: { terminalEnabled: boolean; bottomPanelToggleEnabled: boolean; newProjectEnabled: boolean; editorFocused: boolean;
      accelerators: Record<string, string | null>; acceleratorBindings: Record<string, string[]>; shortcutOverrides: string[]; shortcutRecording: boolean }): Promise<void>;
  };
  browserProfileImport: {
    listImportableBrowserProfiles(): Promise<ImportableBrowserProfile[]>;
    importBrowserProfile(request: BrowserProfileImportRequest): Promise<BrowserProfileImportResult>;
  };
  browser: {
    history(query: { text?: string; source?: "all" | "agent" | "other" }): Promise<BrowserHistoryVisit[]>;
    removeHistory(ids: string[]): Promise<boolean>;
    discover(endpoint?: string): Promise<BrowserDiscoveryResult>;
    captureScreenshot(endpoint: string | undefined, targetId: string): Promise<BrowserScreenshotResult>;
    navigate(endpoint: string | undefined, targetId: string, url: string): Promise<BrowserActionResult>;
    click(endpoint: string | undefined, targetId: string, selector: string): Promise<BrowserActionResult>;
    type(endpoint: string | undefined, targetId: string, selector: string, text: string): Promise<BrowserActionResult>;
  };
  embeddedBrowser: {
    getSitePolicy(): Promise<BrowserSitePolicy>;
    setSitePolicy(request: { origin?: string; category: BrowserSiteCategory; setting: BrowserSiteDecision | "default" }): Promise<BrowserSitePolicy>;
    removeSiteDevice(request: { id: string }): Promise<BrowserSitePolicy>;
    listSitePermissions(): Promise<BrowserSitePermissions[]>;
    removeSitePermissions(request: { origin: string }): Promise<void>;
    pageCommand(payload: BrowserPageCommand): Promise<{ requestId?: number; matches?: number; activeMatchOrdinal?: number; zoomFactor?: number; saved?: boolean; success?: boolean; origin?: string; hostname?: string; secure?: boolean; cookieCount?: number }>;
    downloads(): Promise<BrowserDownload[]>;
    downloadAction(payload: { action: "open-folder" | "open" | "reveal" | "remove" | "pause" | "resume" | "cancel"; id?: string }): Promise<string>;
    onDownloadsChanged(callback: (records: BrowserDownload[]) => void): () => void;
    capture(payload: BrowserCaptureRequest): Promise<BrowserCaptureImage>;
    clearBrowsingData(dataTypes?: BrowserDataType[]): Promise<boolean>;
    create(payload: { id: string; url: string; conversationId: string }): Promise<EmbeddedBrowserState>;
    list(payload: { conversationId: string }): Promise<EmbeddedBrowserState[]>;
    activate(payload: { id: string; conversationId: string }): Promise<boolean>;
    setBounds(payload: EmbeddedBrowserBounds): Promise<boolean>;
    navigate(payload: { id: string; url: string; conversationId: string }): Promise<EmbeddedBrowserState>;
    runAction(payload: { id: string; conversationId: string; action: "back" | "forward" | "reload" | "stop" | "focus" }): Promise<boolean>;
    inspect(payload: { id: string; conversationId: string; kind: "console" | "network" | "element" | "region" }): Promise<EmbeddedBrowserInspectionResult>;
    getSettings(payload: { url: string }): Promise<EmbeddedBrowserSettings>;
    setSettings(payload: { browserEnabled?: boolean; deviceToolbarAlwaysOnNewTabs?: boolean; downloadDirectory?: string | null; downloadPolicy?: "block" | "ask" | "allow"; origin?: string; permission?: string; allowed?: boolean }): Promise<EmbeddedBrowserSettings>;
    onSettingsChanged(callback: (settings: EmbeddedBrowserSettingsSnapshot) => void): () => void;
    clearSiteData(payload: { id: string; conversationId: string }): Promise<boolean>;
    close(payload: { id: string; conversationId: string }): Promise<boolean>;
    closeConversation(conversationId: string): Promise<number>;
    onEvent(callback: (payload: EmbeddedBrowserState) => void): (() => void) | void;
  };
}

declare global {
  interface Window {
    __MINICODE_RUNTIME__?: {
      windowOwnerId?: string;
      windowInstanceId?: string;
      windowBindingToken?: string;
      initialNavigation?: { conversationId: string | null } | null;
      apiBaseUrl?: string;
      wsBaseUrl?: string;
      runtimeToken?: string;
      remoteHost?: { id: string; displayName: string; kind: "ssh" | "wsl"; platform: string; home: string; generation: string };
      desktop?: MiniCodeDesktop;
    };
  }
}

export const runtime = () =>
  typeof window !== "undefined" ? window.__MINICODE_RUNTIME__ : undefined;

export const desktop = (): MiniCodeDesktop | undefined => runtime()?.desktop;
export const getDesktopPreferences = () => desktop()!.preferences.get();
export const setDesktopPreferences = (patch: DesktopPreferencesPatch) => desktop()!.preferences.set(patch);

export function desktopNotificationConversationVisible(conversationId: string, activeConversationId: string | null, settingsOpen: boolean, switchingConversationId: string | null): boolean {
  if (document.visibilityState !== "visible" || settingsOpen || switchingConversationId || conversationId !== activeConversationId) return false;
  const pane = document.querySelector<HTMLElement>(".chat-pane");
  for (let node = pane; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    if (node.hidden || style.display === "none" || style.visibility === "hidden") return false;
  }
  return pane !== null;
}
export const onDesktopPreferencesChanged = (callback: (preferences: DesktopPreferences) => void) => desktop()!.preferences.onChanged(callback);
export const browserHistorySearch = (query: { text?: string; source?: "all" | "agent" | "other" }) => desktop()!.browser.history(query);
export const browserHistoryRemove = (ids: string[]) => desktop()!.browser.removeHistory(ids);
export const embeddedBrowserClearBrowsingData = (dataTypes?: BrowserDataType[]) => desktop()!.embeddedBrowser.clearBrowsingData(dataTypes);
export const embeddedBrowserCapture = (payload: BrowserCaptureRequest) => desktop()!.embeddedBrowser.capture(payload);

export const isDesktop = (): boolean =>
  Boolean(desktop()?.platformInfo?.isDesktop);

// --- Directory picker ---

export const pickDirectory = async (): Promise<string | null> => {
  if (runtime()?.remoteHost) {
    const { showRemoteDirectoryPicker } = await import("../overlays/RemoteDirectoryPicker");
    return showRemoteDirectoryPicker({ initialDirectoryPath: runtime()!.remoteHost!.home,
      platform: runtime()!.remoteHost!.platform, listDirectory: remoteDirectoryEntries });
  }
  return (await desktop()?.pickDirectory()) ?? null;
};

export const remoteDirectoryEntries = async (directoryPath: string | null, directoriesOnly = true): Promise<RemoteDirectoryEntries> => {
  const result = await desktop()!.workspaceDirectoryEntries(directoryPath, directoriesOnly);
  result.entries.sort((left, right) => left.type !== right.type ? (left.type === "directory" ? -1 : 1)
    : left.name.startsWith(".") !== right.name.startsWith(".") ? (left.name.startsWith(".") ? 1 : -1)
    : left.name.localeCompare(right.name));
  return result;
};

export const pickWorkspaceDirectory = async (): Promise<string | null> => {
  if (runtime()?.remoteHost) {
    const path = await pickDirectory();
    return path ? await desktop()!.trustWorkspace(path) : null;
  }
  return (await desktop()?.pickWorkspaceDirectory()) ?? null;
};

export const trustWorkspace = async (path: string): Promise<string | null> =>
  (await desktop()?.trustWorkspace(path)) ?? null;

// --- Filesystem ---

export const fsReadFileInfo = async (path: string): Promise<FsFileResponse | null> => {
  const owner = workspaceRequestOwner();
  const root = workspaceViewRoot();
  if (owner || workspaceHasConversation()) {
    const { readWorkspaceFile } = await import("../protocol/workspace");
    return await readWorkspaceFile(workspaceRequestPath(path, root), root, undefined, owner);
  }
  const fs = desktop()?.fs;
  if (!fs) return null;
  return await fs.readFile(path);
};

const normalizeFsEntry = (entry: unknown): FsEntry | null => {
  if (!entry || typeof entry !== "object") return null;
  const value = entry as Record<string, unknown>;
  const path = typeof value.path === "string" ? value.path : "";
  const name = typeof value.name === "string"
    ? value.name
    : path.split(/[/\\]/).filter(Boolean).pop() ?? path;
  if (!path && !name) return null;
  return {
    name,
    path,
    isDirectory: Boolean(value.isDirectory ?? value.is_dir),
    sizeBytes: typeof value.sizeBytes === "number"
      ? value.sizeBytes
      : typeof value.size_bytes === "number"
        ? value.size_bytes
        : typeof value.size === "number"
          ? value.size
          : undefined,
    modifiedAt: typeof value.modifiedAt === "string"
      ? value.modifiedAt
      : typeof value.modified_at === "string"
        ? value.modified_at
        : typeof value.modified === "number"
          ? new Date(value.modified).toISOString()
          : undefined,
  };
};

const normalizeFsListTreeResult = (path: string, payload: unknown): FsListTreeResult => {
  if (Array.isArray(payload)) {
    return {
      workspaceRoot: path,
      requestedPath: path,
      entries: payload.map(normalizeFsEntry).filter((entry): entry is FsEntry => entry != null),
    };
  }
  const value = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
  const rawEntries = Array.isArray(value.entries) ? value.entries : [];
  return {
    workspaceRoot: typeof value.workspaceRoot === "string"
      ? value.workspaceRoot
      : typeof value.workspace_root === "string"
        ? value.workspace_root
        : path,
    requestedPath: typeof value.requestedPath === "string"
      ? value.requestedPath
      : typeof value.requested_path === "string"
        ? value.requested_path
        : path,
    entries: rawEntries.map(normalizeFsEntry).filter((entry): entry is FsEntry => entry != null),
  };
};

export const fsListTreeResult = async (path: string): Promise<FsListTreeResult> => {
  // A filesystem permission/missing-root failure must reach FileTree's error
  // state. Returning an empty tree turns "access denied" into a false empty
  // workspace and prevents the user from retrying the real operation.
  const owner = workspaceRequestOwner(), root = workspaceViewRoot();
  if (owner || workspaceHasConversation()) {
    const { listWorkspaceTree } = await import("../protocol/workspace");
    const tree = await listWorkspaceTree(root, workspaceRequestPath(path, root), owner);
    return { workspaceRoot: root, requestedPath: path, entries: (tree?.children ?? []).map(entry => ({
      name: entry.name, path: root.replace(/[/\\]+$/, "") + "/" + entry.path,
      isDirectory: entry.is_dir, sizeBytes: entry.size_bytes ?? undefined, modifiedAt: entry.modified_at,
    })) };
  }
  return normalizeFsListTreeResult(path, await desktop()?.fs.listTree(path));
};

export const fsListTree = async (path: string): Promise<FsEntry[]> =>
  (await fsListTreeResult(path)).entries;

export const fsSearchFiles = async (
  rootPath: string,
  query: string,
  limit = 20,
  kind: "file" | "folder" | "all" = "file",
): Promise<FsSearchResult[]> => {
  const owner = workspaceRequestOwner();
  if (owner || workspaceHasConversation()) {
    const { searchWorkspaceFiles } = await import("../protocol/workspace");
    return await searchWorkspaceFiles(rootPath, query, limit, kind, owner);
  }
  const fs = desktop()?.fs;
  const result = kind !== "all" && fs?.searchFilesByKind
    ? await fs.searchFilesByKind(rootPath, query, limit, kind)
    : await fs?.searchFiles(rootPath, query, limit);
  return normalizeFsSearchResult(result);
};

const normalizeFsSearchResult = (payload: unknown): FsSearchResult[] => {
  const raw = Array.isArray(payload)
    ? payload
    : payload && typeof payload === "object" && Array.isArray((payload as Record<string, unknown>).results)
      ? (payload as Record<string, unknown>).results as unknown[]
      : [];
  return raw
    .map((item): FsSearchResult | null => {
      if (!item || typeof item !== "object") return null;
      const value = item as Record<string, unknown>;
      const path = typeof value.path === "string" ? value.path : "";
      if (!path) return null;
      const name = typeof value.name === "string"
        ? value.name
        : path.split(/[/\\]/).filter(Boolean).pop() ?? path;
      const result: FsSearchResult = {
        path,
        name,
        kind: value.kind === "folder" || value.isDirectory === true || value.is_dir === true ? "folder" as const : "file" as const,
      };
      if (typeof value.score === "number") result.score = value.score;
      return result;
    })
    .filter((item): item is FsSearchResult => item != null);
};

// --- PTY ---

const normalizePtySession = (session: unknown, fallbackCwd = ""): PtySession | null => {
  if (!session || typeof session !== "object") return null;
  const value = session as Record<string, unknown>;
  const sessionId = typeof value.sessionId === "string"
    ? value.sessionId
    : typeof value.session_id === "string"
      ? value.session_id
      : "";
  if (!sessionId) return null;
  const output = typeof value.output === "string" ? value.output : undefined;
  const outputChars = typeof value.outputChars === "number"
    ? value.outputChars
    : typeof value.output_chars === "number"
      ? value.output_chars
      : undefined;
  const totalOutputChars = typeof value.totalOutputChars === "number"
    ? value.totalOutputChars
    : typeof value.total_output_chars === "number"
      ? value.total_output_chars
      : undefined;
  const outputStartCursor = typeof value.outputStartCursor === "number"
    ? value.outputStartCursor
    : typeof value.output_start_cursor === "number"
      ? value.output_start_cursor
      : undefined;
  const outputEndCursor = typeof value.outputEndCursor === "number"
    ? value.outputEndCursor
    : typeof value.output_end_cursor === "number"
      ? value.output_end_cursor
      : undefined;
  const isAlive = typeof value.isAlive === "boolean"
    ? value.isAlive
    : typeof value.is_alive === "boolean"
      ? value.is_alive
      : undefined;
  const exitCode = value.exitCode === null || typeof value.exitCode === "number"
    ? value.exitCode
    : value.exit_code === null || typeof value.exit_code === "number"
      ? value.exit_code
      : undefined;
  const exitSignal = value.exitSignal !== undefined ? value.exitSignal : value.exit_signal;
  const exitedAt = typeof value.exitedAt === "number"
    ? value.exitedAt
    : typeof value.exited_at === "number"
      ? value.exited_at
      : undefined;
  const conversationId = typeof value.conversationId === "string"
    ? value.conversationId.trim()
    : typeof value.conversation_id === "string"
      ? value.conversation_id.trim()
      : "";
  if (!conversationId) return null;
  return {
    sessionId,
    conversationId,
    pid: typeof value.pid === "number" ? value.pid : undefined,
    shell: typeof value.shell === "string" ? value.shell : "shell",
    cwd: typeof value.cwd === "string" ? value.cwd : fallbackCwd,
    ...(output !== undefined ? { output } : {}),
    ...(outputChars !== undefined ? { outputChars } : {}),
    ...(totalOutputChars !== undefined ? { totalOutputChars } : {}),
    ...(outputStartCursor !== undefined ? { outputStartCursor } : {}),
    ...(outputEndCursor !== undefined ? { outputEndCursor } : {}),
    ...(typeof value.truncated === "boolean" ? { truncated: value.truncated } : {}),
    ...(isAlive !== undefined ? { isAlive } : {}),
    ...(exitCode !== undefined ? { exitCode } : {}),
    ...(typeof exitSignal === "number" || typeof exitSignal === "string" || exitSignal === null ? { exitSignal } : {}),
    ...(exitedAt !== undefined ? { exitedAt } : {}),
    terminalMode: "pty",
  };
};

export const ptySpawn = async (cwd: string | undefined, conversationId: string): Promise<PtySession | null> => {
  const owner = conversationId.trim();
  if (!owner) return null;
  const pty = desktop()?.pty;
  if (!pty) return null;
  const raw = await pty.spawn(cwd, owner);
  const session = normalizePtySession(raw, cwd ?? "");
  return session?.conversationId === owner ? session : null;
};
export const ptyWrite = (sessionId: string, data: string, conversationId: string) => desktop()?.pty.write(sessionId, data, conversationId);
export const ptyResize = (sessionId: string, cols: number, rows: number, conversationId: string) => desktop()?.pty.resize(sessionId, cols, rows, conversationId);
export const ptyKill = (sessionId: string, conversationId: string) => desktop()?.pty.kill(sessionId, conversationId);
export const ptyRestart = async (sessionId: string, conversationId: string): Promise<PtySession | null> => {
  const owner = conversationId.trim();
  if (!owner) return null;
  const session = normalizePtySession(await desktop()?.pty.restart(sessionId, owner));
  return session?.conversationId === owner ? session : null;
};
export const ptyKillConversation = (conversationId: string, preserveHistory = false) => desktop()?.pty.killConversation(conversationId, preserveHistory);
export const ptyAckExit = (sessionId: string, conversationId: string) => desktop()?.pty.ackExit(sessionId, conversationId);
export const ptyList = async (conversationId: string): Promise<PtySession[]> => {
  const owner = conversationId.trim();
  if (!owner) return [];
  const pty = desktop()?.pty;
  if (!pty) return [];
  const sessions = await pty.list(owner);
  return (sessions ?? [])
    .map((session) => normalizePtySession(session))
    .filter((session): session is PtySession => session?.conversationId === owner);
};
export const ptySnapshot = async (sessionId: string, conversationId: string, maxChars = 80_000): Promise<PtySession | null> => {
  const owner = conversationId.trim();
  if (!owner) return null;
  const session = normalizePtySession(await desktop()?.pty.snapshot(sessionId, maxChars, owner));
  return session?.conversationId === owner ? session : null;
};
export const ptyClear = async (
  sessionId: string,
  conversationId: string,
): Promise<{ cleared: boolean; outputCursor: number }> => {
  const owner = conversationId.trim();
  if (!owner) return { cleared: false, outputCursor: 0 };
  const result = await desktop()?.pty.clear(sessionId, owner);
  return {
    cleared: result?.cleared === true,
    outputCursor: Number.isFinite(result?.outputCursor) ? Number(result?.outputCursor) : 0,
  };
};

// --- Shell / OS ---

/**
 * Browser mode is a supported deployment (`connectionPresentation` renders
 * 浏览器预览模式), and there is no OS shell to hand a path to. Returning
 * `undefined` made every "open with the default app" / "reveal in the file
 * manager" action a dead click: nothing happened and nothing said why. Report it
 * once here so no call site — present or future — can swallow it.
 */
const requireDesktopShell = (action: string): boolean => {
  if (isDesktop()) return true;
  pushToast(`${action}需要桌面版 MiniCode，浏览器预览模式下不可用。`, "warning", 4000);
  return false;
};

export const openExternal = (target: string) => desktop()?.openExternal(target);
export const openPath = (target: string) => {
  if (!requireDesktopShell("使用默认应用打开文件")) return undefined;
  return desktop()?.openPath(target);
};
export const revealPath = (target: string) => {
  if (!requireDesktopShell("在文件管理器中显示")) return undefined;
  return desktop()?.revealPath(target);
};

export const revealRegisteredSkill = (request: { path: string; workspaceRoot: string }) => {
  if (!requireDesktopShell("在文件管理器中显示")) return undefined;
  return desktop()!.skills.reveal(request);
};
export const openManagedSettingsTarget = (request: ManagedSettingsTarget) => {
  if (!requireDesktopShell("打开配置和应用数据")) return undefined;
  return desktop()!.settings.openTarget(request);
};
export const exportDiagnostics = () => desktop()?.diagnostics.export();
export const browserDiscover = async (endpoint?: string): Promise<BrowserDiscoveryResult | null> => {
  try {
    return (await desktop()?.browser.discover(endpoint)) ?? null;
  } catch {
    return null;
  }
};
export const browserCaptureScreenshot = async (
  endpoint: string | undefined,
  targetId: string,
): Promise<BrowserScreenshotResult | null> => {
  try {
    return (await desktop()?.browser.captureScreenshot(endpoint, targetId)) ?? null;
  } catch {
    return null;
  }
};

export const browserNavigate = async (
  endpoint: string | undefined,
  targetId: string,
  url: string,
): Promise<BrowserActionResult | null> => {
  try {
    return (await desktop()?.browser.navigate(endpoint, targetId, url)) ?? null;
  } catch {
    return null;
  }
};

export const browserClick = async (
  endpoint: string | undefined,
  targetId: string,
  selector: string,
): Promise<BrowserActionResult | null> => {
  try {
    return (await desktop()?.browser.click(endpoint, targetId, selector)) ?? null;
  } catch {
    return null;
  }
};

export const browserType = async (
  endpoint: string | undefined,
  targetId: string,
  selector: string,
  text: string,
): Promise<BrowserActionResult | null> => {
  try {
    return (await desktop()?.browser.type(endpoint, targetId, selector, text)) ?? null;
  } catch {
    return null;
  }
};

export const embeddedBrowserCreate = (conversationId: string, id: string, url: string) =>
  desktop()?.embeddedBrowser.create({ id, url, conversationId });
export const embeddedBrowserList = (conversationId: string) =>
  desktop()?.embeddedBrowser.list({ conversationId });
export const embeddedBrowserActivate = (conversationId: string, id: string) =>
  desktop()?.embeddedBrowser.activate({ id, conversationId });
export const embeddedBrowserSetBounds = (bounds: EmbeddedBrowserBounds) =>
  desktop()?.embeddedBrowser.setBounds(bounds);
export const embeddedBrowserNavigate = (conversationId: string, id: string, url: string) =>
  desktop()?.embeddedBrowser.navigate({ id, url, conversationId });
export const embeddedBrowserRunAction = (
  conversationId: string,
  id: string,
  action: "back" | "forward" | "reload" | "stop" | "focus",
) => desktop()?.embeddedBrowser.runAction({ id, action, conversationId });
export const embeddedBrowserInspect = (conversationId: string, id: string, kind: "console" | "network" | "element" | "region") =>
  desktop()?.embeddedBrowser.inspect({ id, kind, conversationId });
export const embeddedBrowserGetSettings = (url: string) => desktop()?.embeddedBrowser.getSettings({ url });
export const onEmbeddedBrowserSettingsChanged = (callback: (settings: EmbeddedBrowserSettingsSnapshot) => void) =>
  desktop()?.embeddedBrowser.onSettingsChanged(callback);
export const embeddedBrowserSetSettings = (payload: {
  downloadDirectory?: string | null;
  browserEnabled?: boolean;
  deviceToolbarAlwaysOnNewTabs?: boolean;
  downloadPolicy?: "block" | "ask" | "allow";
  origin?: string;
  permission?: string;
  allowed?: boolean;
}) => desktop()?.embeddedBrowser.setSettings(payload);
export const embeddedBrowserClearSiteData = (conversationId: string, id: string) =>
  desktop()?.embeddedBrowser.clearSiteData({ id, conversationId });
export const embeddedBrowserClose = (conversationId: string, id: string) =>
  desktop()?.embeddedBrowser.close({ id, conversationId });
export const embeddedBrowserCloseConversation = (conversationId: string) =>
  desktop()?.embeddedBrowser.closeConversation(conversationId);
export const onEmbeddedBrowserEvent = (callback: (payload: EmbeddedBrowserState) => void) =>
  desktop()?.embeddedBrowser.onEvent(callback);

// --- Environment ---

export const envDetect = async (): Promise<DesktopEnvInfo | null> => {
  try {
    const info = await desktop()?.env.detect();
    if (!info) return null;
    return {
      git: Boolean(info.git),
      python: Boolean(info.python),
      node: Boolean(info.node),
      docker: Boolean(info.docker),
      ollama: Boolean(info.ollama),
      home: typeof info.home === "string" ? info.home : "",
    };
  } catch {
    return null;
  }
};
