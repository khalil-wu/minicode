import { pushToast } from "../overlays/ToastContainer";
import type {
  CheckpointCreatedEvent,
  CheckpointListEvent,
  CheckpointRecordPayload,
  CheckpointRewoundEvent,
  ConversationHydrationUpdatedEvent,
  GuidelinesUpdatedEvent,
  PermissionRulePayload,
  PermissionRulesUpdatedEvent,
  RunCheckpointListEvent,
  RunCheckpointRecord,
  RunCheckpointResumeEvent,
  ServerEvent,
  WorkspaceRecentListEvent,
} from "../protocol/events";
import { isReplayedEvent as isReplayed } from "../protocol/events";
import { useAppStore } from "../stores";
import type {
  CheckpointProjectionRecord,
  PermissionRuleProjection,
  RunCheckpointProjectionRecord,
} from "../stores/types";
import { workspaceRootsEqual } from "../lib/workspace-path";
import { addInspectorPayload } from "./inspectorEntries";

const eventTime = (event: ServerEvent): number => {
  const parsed = Date.parse(String(event.timestamp || ""));
  return Number.isFinite(parsed) ? parsed : Date.now();
};

const checkpointProjection = (checkpoint: CheckpointRecordPayload): CheckpointProjectionRecord => ({
  id: checkpoint.id,
  conversationId: checkpoint.conversation_id,
  sessionId: checkpoint.session_id,
  toolCallId: checkpoint.tool_call_id,
  toolName: checkpoint.tool_name,
  workspaceRoot: checkpoint.workspace_root,
  paths: checkpoint.paths.slice(),
  createdAt: checkpoint.created_at,
  metadata: { ...checkpoint.metadata },
});

const runCheckpointProjection = (checkpoint: RunCheckpointRecord): RunCheckpointProjectionRecord => ({
  runId: checkpoint.run_id,
  sessionId: checkpoint.session_id,
  conversationId: checkpoint.conversation_id,
  iteration: checkpoint.iteration,
  iterations: checkpoint.iterations,
  stoppedReason: checkpoint.stopped_reason,
  createdAt: checkpoint.created_at,
  timestamp: checkpoint.timestamp,
});

const permissionRuleProjection = (rule: PermissionRulePayload): PermissionRuleProjection => ({
  pattern: rule.pattern,
  source: rule.source,
  level: rule.level,
  tool: rule.tool,
  ruleContent: rule.rule_content,
  behavior: rule.behavior,
  destination: rule.destination,
});

const eventMatchesKnownWorkspace = (conversationId: string, workspaceRoot: string): boolean => {
  const state = useAppStore.getState();
  const conversation = state.conversations.find((item) => item.id === conversationId);
  const knownRoot = conversation?.worktreePath
    || conversation?.workspaceRoot
    || (conversationId === state.conversationId ? state.workingDirectory : "");
  return !knownRoot || !workspaceRoot || workspaceRootsEqual(knownRoot, workspaceRoot);
};

const isActiveOwner = (conversationId: string): boolean =>
  Boolean(conversationId && conversationId === useAppStore.getState().conversationId);

const projectCheckpoint = (checkpoint: CheckpointProjectionRecord, event: ServerEvent) => {
  useAppStore.getState().recordCheckpointProjection(checkpoint, eventTime(event));
  if (!isActiveOwner(checkpoint.conversationId)) return;
  addInspectorPayload("checkpoint", checkpoint.id, {
    event: event.type,
    checkpoint_id: checkpoint.id,
    conversation_id: checkpoint.conversationId,
    session_id: checkpoint.sessionId,
    tool_call_id: checkpoint.toolCallId,
    tool_name: checkpoint.toolName,
    workspace_root: checkpoint.workspaceRoot,
    paths: checkpoint.paths,
    created_at: checkpoint.createdAt,
    metadata: checkpoint.metadata,
  });
};

export const handleControlPlaneProjectionEvent = (event: ServerEvent): boolean => {
  const state = useAppStore.getState();
  switch (event.type) {
    case "conversation.hydration.updated": {
      const ev = event as ConversationHydrationUpdatedEvent;
      state.setConversationHydration(ev.conversation_id, ev.is_hydrating, eventTime(event));
      if (isActiveOwner(ev.conversation_id)) {
        addInspectorPayload("session", `hydration:${ev.conversation_id}`, {
          event: ev.type,
          conversation_id: ev.conversation_id,
          is_hydrating: ev.is_hydrating,
        });
      }
      return true;
    }
    case "permission.rules.updated": {
      const ev = event as PermissionRulesUpdatedEvent;
      const rules = ev.rules;
      state.setPermissionRulesProjection({
        sessionId: ev.session_id,
        conversationId: ev.conversation_id,
        source: ev.source,
        mode: rules.mode,
        contextSource: rules.context_source,
        systemDeny: rules.system_deny.map(permissionRuleProjection),
        sessionDeny: rules.session_deny.map(permissionRuleProjection),
        sessionOverrides: rules.session_overrides.map(permissionRuleProjection),
        sessionPromptRules: rules.session_prompt_rules.map(permissionRuleProjection),
        updatedAt: eventTime(event),
      });
      if (isActiveOwner(ev.conversation_id)) {
        addInspectorPayload("permission", `permission-rules:${ev.conversation_id}`, {
          event: ev.type,
          session_id: ev.session_id,
          conversation_id: ev.conversation_id,
          source: ev.source,
          rules,
        });
        if (!isReplayed(event) && ev.source !== "frontend.inspector") {
          pushToast(
            `权限规则已更新：会话拒绝 ${rules.session_deny.length} 条，覆盖 ${rules.session_overrides.length} 条，系统拒绝 ${rules.system_deny.length} 条。`,
            "info",
            4200,
          );
        }
      }
      return true;
    }
    case "checkpoint.created": {
      const ev = event as CheckpointCreatedEvent;
      if (!eventMatchesKnownWorkspace(ev.conversation_id, ev.workspace_root)) return true;
      projectCheckpoint(checkpointProjection(ev), event);
      return true;
    }
    case "checkpoint.list": {
      const ev = event as CheckpointListEvent;
      if (!eventMatchesKnownWorkspace(ev.conversation_id, ev.workspace_root)) return true;
      const checkpoints = ev.checkpoints.map(checkpointProjection);
      state.setCheckpointCollectionProjection({
        conversationId: ev.conversation_id,
        workspaceRoot: ev.workspace_root,
        checkpoints,
        updatedAt: eventTime(event),
      });
      if (isActiveOwner(ev.conversation_id)) {
        addInspectorPayload("checkpoint", `checkpoint-list:${ev.conversation_id}`, {
          event: ev.type,
          conversation_id: ev.conversation_id,
          workspace_root: ev.workspace_root,
          checkpoint_count: checkpoints.length,
          checkpoints: ev.checkpoints,
        });
      }
      return true;
    }
    case "checkpoint.rewound": {
      const ev = event as CheckpointRewoundEvent;
      if (!eventMatchesKnownWorkspace(ev.conversation_id, ev.workspace_root)) return true;
      const checkpoint = checkpointProjection(ev.checkpoint);
      projectCheckpoint(checkpoint, event);
      if (isActiveOwner(ev.conversation_id)) {
        state.requestGitChanges();
        if (!isReplayed(event)) {
          const protectedFiles = checkpoint.paths.length === 1
            ? checkpoint.paths[0]
            : `${checkpoint.paths.length} 个文件`;
          pushToast(`已回滚到检查点 ${checkpoint.id.slice(0, 12)}：${protectedFiles}`, "success", 5200);
        }
      }
      return true;
    }
    case "checkpoint.run.list": {
      const ev = event as RunCheckpointListEvent;
      if (!eventMatchesKnownWorkspace(ev.conversation_id, ev.workspace_root)) return true;
      state.setRunCheckpointCollectionProjection({
        sessionId: ev.session_id,
        conversationId: ev.conversation_id,
        workspaceRoot: ev.workspace_root,
        checkpoints: ev.checkpoints.map(runCheckpointProjection),
        runs: ev.runs.slice(),
        subagents: ev.subagents.slice(),
        updatedAt: eventTime(event),
      });
      if (isActiveOwner(ev.conversation_id)) {
        addInspectorPayload("checkpoint", `run-checkpoints:${ev.conversation_id}`, {
          event: ev.type,
          session_id: ev.session_id,
          conversation_id: ev.conversation_id,
          workspace_root: ev.workspace_root,
          checkpoint_count: ev.checkpoints.length,
          run_count: ev.runs.length,
          subagent_count: ev.subagents.length,
          checkpoints: ev.checkpoints,
          runs: ev.runs,
          subagents: ev.subagents,
        });
      }
      return true;
    }
    case "checkpoint.run.resume": {
      const ev = event as RunCheckpointResumeEvent;
      if (!eventMatchesKnownWorkspace(ev.conversation_id, ev.workspace_root)) return true;
      state.setCheckpointResumeProjection({
        resumed: ev.resumed,
        sessionId: ev.session_id,
        conversationId: ev.conversation_id,
        workspaceRoot: ev.workspace_root,
        runId: ev.run_id,
        iteration: ev.iteration,
        stoppedReason: ev.stopped_reason,
        message: ev.message,
        updatedAt: eventTime(event),
      });
      if (isActiveOwner(ev.conversation_id)) {
        addInspectorPayload("checkpoint", `run-resume:${ev.conversation_id}`, {
          event: ev.type,
          resumed: ev.resumed,
          session_id: ev.session_id,
          conversation_id: ev.conversation_id,
          workspace_root: ev.workspace_root,
          run_id: ev.run_id,
          iteration: ev.iteration,
          stopped_reason: ev.stopped_reason,
          message: ev.message,
        });
        if (!isReplayed(event)) {
          pushToast(
            ev.resumed
              ? `已从运行 ${ev.run_id || "未知"} 的第 ${ev.iteration ?? 0} 轮恢复。`
              : ev.message || "没有可恢复的未完成运行。",
            ev.resumed ? "success" : "info",
            5200,
          );
        }
      }
      return true;
    }
    case "workspace.recent.list": {
      const ev = event as WorkspaceRecentListEvent;
      state.setRecentWorkspaces(ev.projects.map((project) => ({
        path: project.path,
        name: project.name,
        projectType: project.project_type,
        lastOpened: project.last_opened,
      })));
      addInspectorPayload("workspace", "recent-workspaces", {
        event: ev.type,
        project_count: ev.projects.length,
        projects: ev.projects,
      });
      return true;
    }
    case "guidelines.updated": {
      const ev = event as GuidelinesUpdatedEvent;
      if (!eventMatchesKnownWorkspace(ev.conversation_id, ev.workspace_root)) return true;
      state.setGuidelinesReloadProjection({
        conversationId: ev.conversation_id,
        workspaceRoot: ev.workspace_root,
        path: ev.path,
        message: ev.message,
        cacheCleared: ev.cache_cleared !== false,
        effectiveFrom: ev.effective_from || "next_turn",
        updatedAt: eventTime(event),
      });
      if (isActiveOwner(ev.conversation_id)) {
        addInspectorPayload("guidelines", `guidelines:${ev.conversation_id}`, {
          event: ev.type,
          conversation_id: ev.conversation_id,
          workspace_root: ev.workspace_root,
          path: ev.path,
          cache_cleared: ev.cache_cleared,
          effective_from: ev.effective_from,
          source_kind: ev.source_kind,
          parent_path: ev.parent_path,
          message: ev.message,
        });
        if (!isReplayed(event)) {
          const source = ev.path ? `“${ev.path}”` : "项目指令";
          pushToast(`已重新加载 ${source}，从下一次 Agent 回合开始生效。`, "info", 5200);
        }
      }
      return true;
    }
    default:
      return false;
  }
};
