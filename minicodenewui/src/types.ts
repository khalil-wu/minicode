export type StepKind = "think" | "read" | "search" | "edit" | "run" | "web";
export type StepStatus = "running" | "done" | "failed";

export interface Step {
  id: string;
  kind: StepKind;
  /** 主体：文件名、命令或搜索词 */
  target: string;
  detail?: string;
  add?: number;
  del?: number;
  output?: string;
  status: StepStatus;
  /** 编辑类步骤对应的文件路径，点击可在右侧打开差异 */
  path?: string;
}

export interface FileChange {
  path: string;
  add: number;
  del: number;
  diff: string;
  reverted?: boolean;
}

export type ApprovalDecision = "approve" | "always" | "deny";

export interface Approval {
  id: string;
  kind: "command" | "write";
  command: string;
  reason: string;
  decision?: ApprovalDecision;
}

export interface QuestionOption {
  id: string;
  label: string;
  desc: string;
}

export interface Question {
  id: string;
  prompt: string;
  options: QuestionOption[];
  answer?: string;
}

export type TurnStatus = "running" | "done" | "stopped" | "failed";
export type TurnPhase = "thinking" | "working" | "waiting" | "writing" | "idle";

export interface AssistantTurn {
  id: string;
  role: "assistant";
  status: TurnStatus;
  phase: TurnPhase;
  startedAt: number;
  /** 结束后固定的耗时（秒） */
  seconds: number;
  steps: Step[];
  text: string;
  files?: FileChange[];
  approval?: Approval;
  question?: Question;
  plan?: { markdown: string; state: "proposed" | "accepted" };
  model: string;
}

export interface Attachment {
  id: string;
  name: string;
  kind: "image" | "file";
  size: string;
}

export interface UserMessage {
  id: string;
  role: "user";
  text: string;
  attachments?: Attachment[];
  at: number;
}

export type Message = UserMessage | AssistantTurn;

export interface Chat {
  id: string;
  title: string;
  projectId: string | null;
  updatedAt: number;
  pinned?: boolean;
  unread?: boolean;
  archived?: boolean;
  messages: Message[];
  queue: string[];
}

export interface Project {
  id: string;
  name: string;
  path: string;
  branch: string;
  expanded: boolean;
}

export type PermissionMode = "readonly" | "ask" | "full";
export type Effort = "low" | "medium" | "high" | "xhigh";
export type ThemePref = "light" | "dark" | "system";
export type Accent = "clay" | "blue" | "violet" | "green" | "graphite";
export type PanelTab = "changes" | "files" | "preview";

export type SettingsSection =
  | "general"
  | "appearance"
  | "models"
  | "personalization"
  | "mcp"
  | "git"
  | "shortcuts"
  | "archived"
  | "about";

export type Route =
  | { name: "home" }
  | { name: "chat"; id: string }
  | { name: "settings"; section: SettingsSection }
  | { name: "plugins" }
  | { name: "automations" };

export interface Toast {
  id: string;
  text: string;
  tone?: "default" | "success" | "danger";
  action?: { label: string; run: () => void };
}
