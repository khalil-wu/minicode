import { useI18n, type Translate } from "../i18n";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useAppStore } from "../stores";
import { pendingPlanImplementation, PlanImplementationPrompt } from "../chat/PlanImplementationPrompt";
import { promptDraftKey } from "../stores/prompt-drafts";
import { deriveSendState } from "../lib/send-state";
import { sendChatMessage } from "../chat/sendChatMessage";
import { captureSteerTurnId } from "../lib/steer-command";
import type { AppStore, ComposerQuote, FollowUpBehavior, MessageAttachmentRef } from "../stores/types";
import { ContextChipRegion } from "./ActionChipRegion";
import { AttachmentStrip } from "./AttachmentStrip";
import { ComposerTextarea } from "./ComposerTextarea";
import { appendComposerTokenAnchor, composerTokenAtSelection, type ComposerSelection } from "./inputSelection";
import { MenuOverlay } from "./MenuOverlay";
import { FooterRow } from "./FooterRow";
import { ProjectScopePicker } from "./ProjectScopePicker";
import { PromptHistoryOverlay } from "./PromptHistoryOverlay";
import { QueuedMessageList } from "./QueuedMessageList";
import { cancelQueuedMessageEdit, installQueuedEditRecovery } from "./queued-message-edit";
import { cancelRetainedInputEdit, completeRetainedInputEdit, discardRetainedComposerInput, installRetainedInputEditRecovery, retainFailedComposerInput, retainPendingComposerInput } from "./retainedComposerInputs";
import { appendPromptHistory, clearPromptHistory, readPromptHistory } from "./prompt-history";
import { acceptAttachmentConversationOwner, uploadComposerFiles } from "./uploads";
import { InlineAgentPrompt } from "../chat/InlineAgentPrompt";
import { TurnPlanProgress } from "../chat/components/TurnPlanProgress";
import { MessageQuote } from "../chat/components/MessageQuote";
import { commandResultSucceeded, sendClientCommand, sendClientCommandAwaitResult } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { getWebSocket } from "../hooks/useWebSocket";
import { buildContextNativeAttachments, buildContextPayload } from "./contextPayload";
import {
  executeRuntimeSlashCommand,
  getActiveRuntimeSlashCommand,
  parseRuntimeSlashInput,
  resolveRuntimeSlashMenuSelection,
  syncRuntimeSlashPanelForDraft,
} from "../lib/runtime-commands";
import { buildInterruptCommand, hasInterruptFence } from "../lib/interrupt-command";
import { workspaceFilePathsEqual, workspaceRootsEqual } from "../lib/workspace-path";
import { hasLocalPendingPromptForConversation } from "../lib/pending-prompts";
import { uniqueMessageId } from "../stores/shared-helpers";
import { composerDraftOf, composerWorkspaceKey, isComposerConversationDeleted } from "../stores/composer-drafts";
import type { CommandResultEvent } from "../protocol/events";
import { capabilityFeatureEnabled } from "../protocol/capabilities";
import type { CodeReviewOwner } from "./CodeReviewMenu";
import { createCodeReview } from "../protocol/code-review";
import type { CodeReviewOptions } from "../protocol/code-review-types";
import { publishCodeReview } from "../chat/codeReview";
import { GuardianApprovalMenu } from "./GuardianApprovalMenu";
import { recentGuardianDenials } from "../chat/guardianApprovalState";
import { cloneDiffComments, formatDiffCommentsPrompt, type DiffComment } from "../lib/diff-comments";
import { clearSubmittedDiffComments, getDiffCommentScope, readDiffCommentState, removeDiffComment, restoreSubmittedDiffComments,
  subscribeDiffComments } from "../lib/diff-comment-storage";
import { diffCommentKey } from "../lib/diff-comments";
import { DiffCommentAttachments } from "./DiffCommentAttachments";
import { claimSubmittedDraft } from "./submittedDraftClaim";
import { emptyConversationWorkbenchState } from "../stores/ui-slice";
import { emptyComposerDraft } from "../stores/composer-drafts";
import { GoalBar } from "./GoalBar";
import { enterComposerGoalMode, submitComposerGoal } from "./goal-state";
import { hasRichComposerContent, inlineComposerReferences, type RichComposerDocument } from "./rich-composer-document";
import { composerSkillReference, type ComposerReferenceInsertion, type InlineComposerReference } from "./composer-references";

let initialCatalogRequested = false;
const CodeReviewMenu = lazy(() => import("./CodeReviewMenu").then(module => ({ default: module.CodeReviewMenu })));
const selectedContextReferences = (state: Pick<AppStore, "richDraft" | "selectedMentions" | "selectedSkills">) => state.richDraft
  ? [...inlineComposerReferences(state.richDraft), ...state.selectedMentions.filter(reference => reference.kind === "browser_annotation")]
  : [...state.selectedMentions, ...state.selectedSkills];

// Agent prompts and template slash commands share the context pipeline:
// resolves the target conversation, uploads @mention/skill context attachments,
// and guards against conversation switches mid-upload. Returns null when the
// send must abort (a toast has already been shown).
const buildOutgoingContext = async (requestedConversationId: string, tr: Translate, stateAtSend = useAppStore.getState(),
  submittedComments?: readonly DiffComment[], onFailure?: (reason: string) => void) => {
  const targetConversationId = String(
    requestedConversationId || stateAtSend.conversationId || "",
  ).trim();
  const contextRefs = selectedContextReferences(stateAtSend);
  const comments = cloneDiffComments(submittedComments ?? readDiffCommentState(getDiffCommentScope(
    targetConversationId || stateAtSend.activeConversationDraftId, stateAtSend.workingDirectory)).comments);
  const skillInvocations = stateAtSend.richDraft ? "" : buildSkillInvocationLine(stateAtSend.selectedSkills);
  const contextPayload = await buildContextPayload(contextRefs);
  let nativeContext: Awaited<ReturnType<typeof buildContextNativeAttachments>> = {
    attachments: [] as Record<string, unknown>[],
    attachmentRefs: [] as MessageAttachmentRef[],
    notes: "",
    conversationId: targetConversationId || undefined,
  };
  try {
    nativeContext = await buildContextNativeAttachments(
      contextRefs,
      getWebSocket()?.sessionId,
      targetConversationId,
      stateAtSend.workingDirectory,
    );
  } catch (error) {
    onFailure?.(error instanceof Error ? error.message : String(error));
    console.warn("Failed to build native context attachments:", error);
    pushToast(
      error instanceof Error ? error.message : tr("上下文附件上传失败，请重试。"),
      "error",
      4500,
    );
    return null;
  }
  const currentState = useAppStore.getState();
  if (
    !targetConversationId
    && (currentState.conversationId !== stateAtSend.conversationId
      || !workspaceRootsEqual(currentState.workingDirectory, stateAtSend.workingDirectory))
  ) {
    pushToast(tr("会话或工作区已切换，本次发送已取消；请回到原输入后重试。"), "warning", 4500);
    return null;
  }
  if (nativeContext.notes) {
    pushToast(tr("部分上下文附件无法读取，已在消息中标明。"), "warning", 4200);
  }
  const resolvedConversationId = String(
    nativeContext.conversationId || targetConversationId,
  ).trim();
  if (
    targetConversationId
    && resolvedConversationId
    && resolvedConversationId !== targetConversationId
  ) {
    pushToast(tr("附件所属会话已变化，请切回原会话后重试。"), "error", 4500);
    return null;
  }
  if (
    !targetConversationId
    && resolvedConversationId
    && !await acceptAttachmentConversationOwner(resolvedConversationId)
  ) {
    pushToast(tr("附件已绑定到另一个会话，请切回该会话后发送。"), "warning", 4500);
    return null;
  }
  const prefix = [skillInvocations, contextPayload, nativeContext.notes].filter(Boolean).join("\n\n");
  return {
    stateAtSend,
    contextRefs: [...contextRefs, ...comments.map(comment => ({ kind: "diff_comment" as const,
      name: comment.position.path, path: comment.position.path, comment }))],
    comments,
    prefix,
    attachments: nativeContext.attachments,
    attachmentRefs: nativeContext.attachmentRefs,
    conversationId: resolvedConversationId || targetConversationId || undefined,
  };
};

export const Composer = ({ minimal = false }: { minimal?: boolean } = {}) => {
  useEffect(installQueuedEditRecovery, []);
  useEffect(installRetainedInputEditRecovery, []);
  const { t: tr } = useI18n();
  const draft = useAppStore((s) => s.draft);
  const richDraft = useAppStore(s => s.richDraft);
  const setDraft = useAppStore((s) => s.setDraft);
  const quotedMessage = useAppStore((s) => s.quotedMessage);
  const clearQuotedMessage = useAppStore((s) => s.clearQuotedMessage);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const emptyConversation = useAppStore((s) => s.messages.length === 0);
  const isConnected = useAppStore((s) => s.isConnected);
  const slashPanelOpen = useAppStore((s) => s.slashPanelOpen);
  const mentionPanelOpen = useAppStore((s) => s.mentionPanelOpen);
  const openSlashPanel = useAppStore((s) => s.openSlashPanel);
  const closeSlashPanel = useAppStore((s) => s.closeSlashPanel);
  const openMentionPanel = useAppStore((s) => s.openMentionPanel);
  const closeMentionPanel = useAppStore((s) => s.closeMentionPanel);
  const clearAttachments = useAppStore((s) => s.clearAttachments);
  const addSelectedMention = useAppStore((s) => s.addSelectedMention);
  const clearSelectedMentions = useAppStore((s) => s.clearSelectedMentions);
  const clearSelectedSkills = useAppStore((s) => s.clearSelectedSkills);
  const removeSelectedSkill = useAppStore((s) => s.removeSelectedSkill);
  const setMentionResults = useAppStore((s) => s.setMentionResults);
  const selectedSkills = useAppStore((s) => s.selectedSkills);
  const selectedMentions = useAppStore((s) => s.selectedMentions);
  const pendingConversationSwitchId = useAppStore((s) => s.pendingConversationSwitchId);
  const hasPendingPrompt = useAppStore((s) => hasLocalPendingPromptForConversation([
    s.pendingApproval, ...s.approvalQueue, s.pendingAskUser, ...s.askUserQueue,
    s.pendingDiffReview, ...s.diffReviewQueue,
  ], s.conversationId));
  const hasPendingApproval = useAppStore((s) => !s.pendingConversationSwitchId && hasLocalPendingPromptForConversation([
    s.pendingApproval, ...s.approvalQueue, s.pendingDiffReview, ...s.diffReviewQueue,
    ...[s.pendingAskUser, ...s.askUserQueue].filter(request => request?.requestUserInput || request?.elicitation || request?.planReview),
  ], s.conversationId));
  const skillCatalog = useAppStore((s) => s.availableSkills);
  const activeGoal = useAppStore((s) => capabilityFeatureEnabled(s.runtimeCapabilities, "goals") ? s.activeGoal : null);
  const goalMode = useAppStore((s) => s.goalMode && capabilityFeatureEnabled(s.runtimeCapabilities, "goals"));
  const currentModel = useAppStore((s) => s.currentModel);
  const conversationId = useAppStore(state => state.conversationId);
  const activeDraftId = useAppStore(state => state.activeConversationDraftId);
  const planMessages = useAppStore(state => state.agentMode === "plan" && !state.isStreaming ? state.messages : null);
  const agentMode = useAppStore(state => state.agentMode);
  const promptDrafts = useAppStore(state => state.promptDrafts);
  const planImplementation = useMemo(() => {
    if (!conversationId || pendingConversationSwitchId || !planMessages) return null;
    const plan = pendingPlanImplementation(planMessages, agentMode, isStreaming);
    return plan && !promptDrafts[promptDraftKey({ conversationId, requestId: `implement-plan:${plan.turnId || plan.messageId}` })]?.planImplementationSettled ? plan : null;
  }, [conversationId, pendingConversationSwitchId, planMessages, agentMode, isStreaming, promptDrafts]);
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const editorOwner = useRef({ conversationId, draftId: activeDraftId, workingDirectory, document: richDraft,
    key: conversationId || activeDraftId || composerWorkspaceKey(workingDirectory) });
  let editorKey = editorOwner.current.key;
  if (editorOwner.current.conversationId !== conversationId || editorOwner.current.draftId !== activeDraftId || editorOwner.current.workingDirectory !== workingDirectory) {
    const boundDraft = editorOwner.current.draftId && conversationId
      && useAppStore.getState().conversationDrafts[editorOwner.current.draftId]?.conversationId === conversationId;
    const adopted = !editorOwner.current.conversationId && conversationId && (boundDraft
      || (editorOwner.current.workingDirectory === workingDirectory && richDraft && richDraft === editorOwner.current.document));
    if (!adopted) editorKey = conversationId || activeDraftId || composerWorkspaceKey(workingDirectory);
  }
  editorOwner.current = { conversationId, draftId: activeDraftId, workingDirectory, document: richDraft, key: editorKey };
  const commentScope = getDiffCommentScope(conversationId || activeDraftId, workingDirectory);
  const commentState = useSyncExternalStore(subscribeDiffComments, () => readDiffCommentState(commentScope));

  const containerRef = useRef<HTMLDivElement>(null);
  const inputSelectionRef = useRef<ComposerSelection>({ start: draft.length, end: draft.length });
  const [selectionRequest, setSelectionRequest] = useState<ComposerSelection>();
  const [referenceInsertion, setReferenceInsertion] = useState<{ owner: string; request: ComposerReferenceInsertion }>();
  const inlineReferences = useMemo(() => [...selectedMentions.filter((reference): reference is Exclude<typeof reference, { kind: "browser_annotation" }> => reference.kind !== "browser_annotation"),
    ...selectedSkills.map(skill => {
      const metadata = skillCatalog.find(item => skill.path ? workspaceFilePathsEqual(item.path, skill.path, workingDirectory) : item.name === skill.name);
      return metadata ? composerSkillReference(metadata) : skill;
    })], [selectedMentions, selectedSkills, skillCatalog, workingDirectory]);
  const submittingRef = useRef(new Map<string, object>());
  const [submittingOwners, setSubmittingOwners] = useState<ReadonlySet<string>>(() => new Set());
  const submitting = submittingOwners.has(editorKey);
  const stoppingRef = useRef(new Set<string>());
  const [stoppingOwners, setStoppingOwners] = useState<ReadonlySet<string>>(() => new Set());
  const isStopping = isStreaming && stoppingOwners.has(conversationId || "");
  const [menuFilter, setMenuFilter] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [selectedSlashCommand, setSelectedSlashCommand] = useState<string | null>(null);
  const selectedSlashCommandRef = useRef(selectedSlashCommand);
  selectedSlashCommandRef.current = selectedSlashCommand;
  const submittedDraftClaims = useRef(new Set<ReturnType<typeof claimSubmittedDraft>>());
  useEffect(() => { for (const claim of submittedDraftClaims.current) claim.observe(); }, [selectedSlashCommand]);
  useEffect(() => () => { for (const claim of submittedDraftClaims.current) claim.release(); submittedDraftClaims.current.clear(); }, []);
  const [skillPanelOpen, setSkillPanelOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [reviewOwner, setReviewOwner] = useState<CodeReviewOwner>();
  const [guardianApprovalOwner, setGuardianApprovalOwner] = useState<{ conversationId: string; input: string; selectedCommand: string | null }>();
  const [historyItems, setHistoryItems] = useState<string[]>([]);
  // ArrowUp/ArrowDown recall cursor (cc useArrowKeyHistory): -1 = live draft,
  // 0..n-1 = position in most-recent-first history. savedDraft preserves the
  // in-progress text so ArrowDown past the newest entry restores it.
  const historyCursorRef = useRef(-1);
  const historySavedDraftRef = useRef("");
  const historySavedDocumentRef = useRef<RichComposerDocument>();
  useEffect(() => {
    historyCursorRef.current = -1;
    historySavedDraftRef.current = "";
    historySavedDocumentRef.current = undefined;
    setHistoryOpen(false);
    setSelectionRequest(undefined);
    setReferenceInsertion(undefined);
  }, [editorKey]);
  const hasReadyAttachment = useAppStore((s) => s.attachments.some((a) => a.status === "ready"));
  const hasComposerAttachments = useAppStore((s) => s.attachments.length > 0);
  const reviewPermissionPending = useAppStore(s => Boolean(s.conversationId && s.pendingPermissionModeChanges[s.conversationId]));
  useEffect(() => { setReviewOwner(undefined); }, [conversationId, activeDraftId, workingDirectory, pendingConversationSwitchId]);
  useEffect(() => { setGuardianApprovalOwner(undefined); }, [conversationId, workingDirectory, pendingConversationSwitchId]);

  const openGuardianApproval = () => {
    const state = useAppStore.getState();
    if (!state.conversationId || recentGuardianDenials(state.conversationId).length === 0) return;
    setGuardianApprovalOwner({ conversationId: state.conversationId, input: state.draft, selectedCommand: selectedSlashCommandRef.current });
    closeSlashPanel(); closeMentionPanel(); setSkillPanelOpen(false); setHistoryOpen(false); setMenuFilter("");
  };
  const finishGuardianApproval = () => {
    const owner = guardianApprovalOwner!;
    const state = useAppStore.getState();
    if (state.conversationId === owner.conversationId && state.draft === owner.input
      && selectedSlashCommandRef.current === owner.selectedCommand) {
      if (parseRuntimeSlashInput(owner.input)?.commandLine === "/approve") setDraft("");
      if (owner.selectedCommand === "/approve") setSelectedSlashCommand(null);
    }
    setGuardianApprovalOwner(undefined);
  };

  const openCodeReview = () => {
    const captured = useAppStore.getState();
    const selectedCommand = selectedSlashCommandRef.current;
    closeSlashPanel(); closeMentionPanel(); setSkillPanelOpen(false); setHistoryOpen(false); setMenuFilter("");
    void (async () => {
      const owner = captured.conversationId || await captured.ensureComposerConversation();
      if (!owner) return;
      const state = useAppStore.getState();
      if (state.conversationId !== owner || state.pendingConversationSwitchId) return;
      setReviewOwner({ conversationId: owner, draftId: captured.activeConversationDraftId,
        cwd: state.workingDirectory, input: captured.draft, selectedCommand });
    })().catch(error => pushToast(error instanceof Error ? error.message : String(error), "error"));
  };

  const dismissCodeReview = () => {
    setReviewOwner(owner => owner ? { ...owner, dismissed: true } : undefined);
    closeSlashPanel(); closeMentionPanel(); setSkillPanelOpen(false); setMenuFilter("");
  };

  const startCodeReview = (mode: "uncommitted" | "base-branch", options: CodeReviewOptions, branch?: string) => {
    const owner = reviewOwner!;
    return runSubmission(async () => {
      const requestMessage = mode === "uncommitted" ? tr("请审阅未提交的更改")
        : tr("请审阅 {from} 相对 {to} 的更改", { from: options.currentBranch, to: branch! });
      const prepared = await createCodeReview(owner.conversationId, owner.cwd, mode, branch, undefined, requestMessage);
      const state = useAppStore.getState();
      if ((state.conversationId || "") !== (owner.conversationId || "") || !workspaceRootsEqual(state.workingDirectory, owner.cwd)) {
        throw new Error(tr("工作区已变化，请重新选择审阅目标。"));
      }
      const userMessageId = uniqueMessageId("u");
      let admittedOwner = prepared.reviewThreadId;
      const submittedOperation = submittingRef.current.get(editorKey);
      let published = false;
      const completeReviewAdmission = () => {
        if (published || isComposerConversationDeleted(admittedOwner)) return;
        published = true;
        if (prepared.delivery === "inline") clearSubmittedInput(owner.input, owner.conversationId || "", owner.cwd, owner.selectedCommand, admittedOwner, owner.draftId);
        publishCodeReview(prepared, admittedOwner, userMessageId);
        setReviewOwner(current => current === owner ? undefined : current);
        completeSubmission(editorKey, submittedOperation);
        if (prepared.delivery === "detached" && useAppStore.getState().conversationId === owner.conversationId) {
          useAppStore.getState().requestConversationSwitch(admittedOwner);
        }
      };
      const sent = await sendChatMessage({ displayContent: prepared.requestMessage, backendContent: prepared.prompt,
        conversationId: prepared.reviewThreadId, codeReview: prepared.scope, userMessageId, awaitAdmission: true,
        onAdmission: event => { if (typeof event.data?.conversation_id === "string") admittedOwner = event.data.conversation_id; },
        onModelAccepted: completeReviewAdmission,
      });
      if (sent) completeReviewAdmission();
    });
  };

  useEffect(() => {
    if (!dragOver) return;
    const clearDragOver = () => setDragOver(false);
    const cancelDrag = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      clearDragOver();
    };
    window.addEventListener("drop", clearDragOver, true);
    window.addEventListener("dragend", clearDragOver);
    window.addEventListener("blur", clearDragOver);
    window.addEventListener("keydown", cancelDrag, true);
    return () => {
      window.removeEventListener("drop", clearDragOver, true);
      window.removeEventListener("dragend", clearDragOver);
      window.removeEventListener("blur", clearDragOver);
      window.removeEventListener("keydown", cancelDrag, true);
    };
  }, [dragOver]);

  const activeSlashCommand = selectedSlashCommand ?? getActiveRuntimeSlashCommand(draft);
  const sendState = submitting ? "sending" : deriveSendState({
    hasContent: goalMode ? hasRichComposerContent(richDraft, draft) : hasRichComposerContent(richDraft, draft)
      || selectedSkills.length > 0 || selectedMentions.length > 0 || hasReadyAttachment || Boolean(selectedSlashCommand) || commentState.comments.length > 0,
    isStreaming,
    isConnected,
    hasModel: activeSlashCommand === "/compact" || currentModel.trim().length > 0,
  });
  // Both work modes use the code-mode chat presentation. Mode-specific tools
  // remain separate; transcript and composer geometry do not change.
  const codeLayout = !minimal;
  const wideMode = false;
  const commandModeActive = Boolean(activeSlashCommand && !slashPanelOpen);

  const openPromptHistory = useCallback(() => {
    setReviewOwner(owner => owner ? { ...owner, dismissed: true } : undefined);
    closeSlashPanel();
    closeMentionPanel();
    setSkillPanelOpen(false);
    setMenuFilter("");
    setHistoryItems(readPromptHistory(workingDirectory));
    setHistoryOpen(true);
  }, [closeMentionPanel, closeSlashPanel, workingDirectory]);

  useEffect(() => {
    window.addEventListener("composer:history-search", openPromptHistory);
    return () => window.removeEventListener("composer:history-search", openPromptHistory);
  }, [openPromptHistory]);

  const recallHistory = useCallback(
    (direction: "up" | "down"): string | { value: string; document?: RichComposerDocument } | null => {
      const items = readPromptHistory(workingDirectory);
      if (items.length === 0) return null;
      const cursor = historyCursorRef.current;
      if (direction === "up") {
        // Entering history from the live draft: save it so ArrowDown can return.
        if (cursor === -1) {
          historySavedDraftRef.current = useAppStore.getState().draft;
          historySavedDocumentRef.current = useAppStore.getState().richDraft;
        }
        const next = Math.min(cursor + 1, items.length - 1);
        historyCursorRef.current = next;
        return items[next];
      }
      // direction === "down"
      if (cursor <= -1) return null; // already at live draft; let arrow move caret
      const next = cursor - 1;
      historyCursorRef.current = next;
      return next === -1 ? { value: historySavedDraftRef.current, document: historySavedDocumentRef.current } : items[next];
    },
    [workingDirectory],
  );

  const escapeInterrupt = useCallback((): boolean => {
    if (useAppStore.getState().retainedInputEdit) { cancelRetainedInputEdit(); return true; }
    if (useAppStore.getState().queuedEdit) { void cancelQueuedMessageEdit(); return true; }
    if (!useAppStore.getState().isStreaming) return false;
    void stopRun();
    return true;
  }, []);

  const sendUserMessage = async (
    content: string,
    readyAttachments: Record<string, unknown>[] = [],
    options?: {
      backendContent?: string;
      displayContent?: string;
      attachmentRefs?: MessageAttachmentRef[];
      conversationId?: string;
      allowWhileStreaming?: boolean;
      busyBehavior?: "queue" | "steer";
      expectedTurnId?: string | null;
      quotedMessage?: ComposerQuote | null;
      skipLocalAppend?: boolean;
      userMessageId?: string;
      awaitAdmission?: boolean;
      onPreparing?: () => void;
      onAdmission?: (event: CommandResultEvent) => void;
      onModelAccepted?: () => void;
      onSubmitted?: () => void;
      onReceived?: () => void;
      onRejected?: (reason?: string) => void;
      onConversationCreated?: (conversationId: string) => void;
    },
  ) => {
    let capturedState = useAppStore.getState();
    if (!options?.conversationId && !capturedState.conversationId && !capturedState.activeConversationDraftId) {
      capturedState.startConversationDraft({ bindWorkspace: Boolean(capturedState.workingDirectory), appMode: capturedState.appMode });
      capturedState = useAppStore.getState();
    }
    let owner = options?.conversationId || capturedState.conversationId || "";
    const comments = cloneDiffComments(readDiffCommentState(getDiffCommentScope(
      owner || capturedState.activeConversationDraftId, capturedState.workingDirectory)).comments);
    let materializing: Promise<string | null> | undefined;
    if (!owner && capturedState.activeConversationDraftId) {
      // Materialization captures the submitted body synchronously. The editor
      // can now accept the next draft while resource creation runs separately.
      materializing = capturedState.materializeConversationDraft(capturedState.activeConversationDraftId);
    }
    // Context and attachment preparation can take time for existing chats too.
    // Capture their owner and context above, then release the submitted draft.
    options?.onPreparing?.();
    if (materializing) {
      owner = await materializing || "";
      if (!owner) return false;
      options?.onConversationCreated?.(owner);
    }
    const expectedTurnId = options && Object.hasOwn(options, "expectedTurnId") ? options.expectedTurnId
      : options?.allowWhileStreaming && options.busyBehavior === "steer"
        ? captureSteerTurnId(useAppStore.getState(), options.conversationId) : undefined;
    const outgoing = await buildOutgoingContext(owner, tr, capturedState, comments, options?.onRejected);
    if (owner && isComposerConversationDeleted(owner)) return false;
    if (!outgoing) return false;
    const mergedAttachments = [...readyAttachments, ...outgoing.attachments];
    const mergedAttachmentRefs = [...(options?.attachmentRefs ?? []), ...outgoing.attachmentRefs];
    const effectiveContent = [outgoing.prefix, content].filter(Boolean).join("\n\n").trim();
    if (!effectiveContent && mergedAttachments.length === 0 && outgoing.comments.length === 0) return false;
    return sendChatMessage({
      queuedEdit: outgoing.stateAtSend.queuedEdit,
      displayContent: options?.displayContent ?? content,
      backendContent: formatDiffCommentsPrompt([outgoing.prefix, options?.backendContent ?? content].filter(Boolean).join("\n\n").trim(), outgoing.comments),
      attachments: mergedAttachments,
      attachmentRefs: mergedAttachmentRefs,
      conversationId: outgoing.conversationId,
      agentMode: outgoing.stateAtSend.agentMode,
      primaryFile: outgoing.stateAtSend.workingDirectory
        ? outgoing.stateAtSend.activeTabPath ?? undefined
        : undefined,
      contextRefs: outgoing.contextRefs,
      quotedMessage: options?.quotedMessage ?? null,
      allowWhileStreaming: options?.allowWhileStreaming,
      busyBehavior: options?.busyBehavior,
      expectedTurnId,
      skipLocalAppend: options?.skipLocalAppend,
      userMessageId: options?.userMessageId,
      awaitAdmission: options?.awaitAdmission,
      onAdmission: options?.onAdmission,
      onModelAccepted: options?.onModelAccepted,
      onSubmitted: options?.onSubmitted,
      onReceived: options?.onReceived,
      onRejected: options?.onRejected,
      onConversationCreated: options?.onConversationCreated,
    });
  };

  const sendRuntimeSlashMessage = async (options: {
    displayContent: string;
    backendContent: string;
    skipLocalAppend: boolean;
  }, prepared?: { attachments: Record<string, unknown>[]; attachmentRefs: MessageAttachmentRef[]; conversationId?: string; quotedMessage?: ComposerQuote | null },
    admission?: { userMessageId: string; onAdmission: (event: CommandResultEvent) => void; onModelAccepted?: () => void; onReceived?: () => void;
      onConversationCreated?: (conversationId: string) => void },
    busyBehavior: FollowUpBehavior = useAppStore.getState().followUpBehavior) => {
    const state = useAppStore.getState();
    // Compaction acts on its conversation. Draft attachments, mentions and
    // quoted text belong to the next agent prompt, not to this operation.
    if (options.skipLocalAppend && parseRuntimeSlashInput(options.backendContent)?.command === "/compact") return sendChatMessage({
      ...options,
      conversationId: prepared?.conversationId || state.conversationId || undefined,
      agentMode: state.agentMode,
      allowWhileStreaming: state.isStreaming,
      busyBehavior,
      awaitAdmission: true,
      ...admission,
    });
    return sendUserMessage(options.displayContent, prepared?.attachments, {
      ...prepared,
      ...options,
      backendContent: [prepared?.quotedMessage ? formatQuotedMessageForBackend(prepared.quotedMessage) : "", options.backendContent].filter(Boolean).join("\n\n"),
      allowWhileStreaming: state.isStreaming,
      busyBehavior,
      awaitAdmission: true,
      ...admission,
    });
  };

  const resetComposer = () => {
    setDraft("");
    historyCursorRef.current = -1;
    historySavedDraftRef.current = "";
    historySavedDocumentRef.current = undefined;
    clearAttachments();
    clearSelectedMentions();
    clearSelectedSkills();
    clearQuotedMessage();
    setMentionResults([]);
    closeSlashPanel();
    closeMentionPanel();
    setSkillPanelOpen(false);
    setMenuFilter("");
    setSelectedSlashCommand(null);
  };

  const stopRun = async () => {
    const state = useAppStore.getState();
    const command = buildInterruptCommand(state);
    const owner = command.conversation_id || "";
    if (stoppingRef.current.has(owner)) return;
    stoppingRef.current.add(owner);
    setStoppingOwners(new Set(stoppingRef.current));
    // Before agent.run.started binds a turn/message fence, the backend must
    // reject an unfenced interrupt rather than guessing which run to stop.
    // Finish the local optimistic state in that narrow window so Stop never
    // appears to do nothing; a fenced command still waits for the server's
    // authoritative terminal event.
    if (!hasInterruptFence(command)) state.interrupt();
    try {
      const result = await sendClientCommandAwaitResult(command, "interrupt", { silent: true });
      if (!commandResultSucceeded(result)) pushToast(result.message, "error", 4500);
    } catch (error) {
      pushToast(error instanceof Error ? error.message : String(error), "error", 4500);
    } finally {
      stoppingRef.current.delete(owner);
      setStoppingOwners(new Set(stoppingRef.current));
    }
  };

  const composerFingerprint = (state = useAppStore.getState(), comments = readDiffCommentState(
    getDiffCommentScope(state.conversationId || state.activeConversationDraftId, state.workingDirectory)).comments,
    command = selectedSlashCommandRef.current) => JSON.stringify({
      conversationId: String(state.conversationId || ""), draftId: state.activeConversationDraftId || "",
      workspaceRoot: state.workingDirectory, draft: state.draft, richDraft: state.richDraft,
      attachments: state.attachments.map(item => ({ id: item.id, status: item.status,
        artifactId: item.artifactId || item.attachment?.artifact_id || item.attachment?.id || "" })),
      mentions: state.selectedMentions, skills: state.selectedSkills, quotedMessage: state.quotedMessage,
      selectedSlashCommand: command, comments,
    });

  const clearSubmittedComposer = (fingerprint: string, originalOwner: string, workspace: string, userId: string,
    admittedOwner: string, historyContent?: string, submittedComments: readonly DiffComment[] = [],
    captured?: { draftId: string | null; command: string | null }) => {
    const state = useAppStore.getState(), draftId = captured?.draftId ?? null;
    const command = captured ? captured.command : selectedSlashCommandRef.current;
    const boundOwner = (draftId ? state.conversationDrafts[draftId]?.conversationId : null) || admittedOwner;
    const currentOwner = String(state.conversationId || "").trim();
    const sameOwner = draftId ? state.activeConversationDraftId === draftId || Boolean(boundOwner && currentOwner === boundOwner)
      : currentOwner === originalOwner || (!originalOwner && currentOwner === admittedOwner
        && state.messages.some(message => message.id === userId));
    const originalView = { ...state, activeConversationDraftId: draftId, conversationId: originalOwner, workingDirectory: workspace };
    if (sameOwner && (draftId || workspaceRootsEqual(state.workingDirectory, workspace))
      && composerFingerprint(originalView, readDiffCommentState(getDiffCommentScope(currentOwner || draftId, workspace)).comments, command) === fingerprint) {
      if (historyContent?.trim()) appendPromptHistory(workspace, historyContent);
      resetComposer();
      // Materializing a blank conversation adopts the submitted composer into
      // its workbench before the user_message is sent. Once the transport has
      // accepted that message, the active draft is cleared above, but the
      // adopted workbench copy must be cleared as well or the conversation
      // switch projection restores the submitted text into the composer.
      if (boundOwner && boundOwner !== originalOwner) {
        const saved = useAppStore.getState().conversationWorkbenchStates[boundOwner];
        if (saved) {
          for (const attachment of saved.attachments) if (attachment.dataUrl?.startsWith("blob:")) URL.revokeObjectURL?.(attachment.dataUrl);
          useAppStore.setState(current => ({ conversationWorkbenchStates: { ...current.conversationWorkbenchStates,
            [boundOwner]: { ...saved, draft: "", richDraft: undefined, attachments: [], selectedMentions: [], selectedSkills: [], quotedMessage: null },
          } }));
        }
      }
    } else if (boundOwner) {
      // Conversation switching can reset the visible rich draft before this
      // callback runs; the adopted hidden snapshot still owns the submission.
      const saved = state.conversationWorkbenchStates[boundOwner];
      if (saved && composerFingerprint({ ...originalView, ...saved, workingDirectory: workspace },
        readDiffCommentState(getDiffCommentScope(boundOwner, workspace)).comments, command) === fingerprint) {
        for (const attachment of saved.attachments) if (attachment.dataUrl?.startsWith("blob:")) URL.revokeObjectURL?.(attachment.dataUrl);
        useAppStore.setState(current => ({ conversationWorkbenchStates: { ...current.conversationWorkbenchStates,
          [boundOwner]: { ...saved, draft: "", richDraft: undefined, attachments: [], selectedMentions: [], selectedSkills: [], quotedMessage: null },
        } }));
      }
    }
    if (!originalOwner) {
      const key = draftId || composerWorkspaceKey(workspace);
      const saved = useAppStore.getState().workspaceComposerDrafts[key];
      if (saved && composerFingerprint({ ...originalView, ...saved }, undefined, command) === fingerprint) {
        useAppStore.setState(current => {
          const drafts = { ...current.workspaceComposerDrafts }; delete drafts[key];
          return { workspaceComposerDrafts: drafts };
        });
      }
    }
    clearSubmittedDiffComments(getDiffCommentScope(originalOwner || draftId, workspace), submittedComments);
    if (boundOwner && boundOwner !== originalOwner) clearSubmittedDiffComments(getDiffCommentScope(boundOwner, workspace), submittedComments);
  };

  const clearSubmittedInput = (input: string, originalOwner: string, workspace: string, selectedCommand: string | null,
    acceptedOwner: string, draftId: string | null = null) => {
    const state = useAppStore.getState();
    const boundOwner = (draftId ? state.conversationDrafts[draftId]?.conversationId : null) || acceptedOwner;
    const currentOwner = String(state.conversationId || "").trim();
    const sameOwner = draftId ? state.activeConversationDraftId === draftId || Boolean(boundOwner && currentOwner === boundOwner)
      : currentOwner === originalOwner || (!originalOwner && currentOwner === acceptedOwner);
    if (sameOwner && (draftId || workspaceRootsEqual(state.workingDirectory, workspace)) && state.draft === input
      && selectedSlashCommandRef.current === selectedCommand) {
      setDraft(""); setSelectedSlashCommand(null); closeSlashPanel(); setMenuFilter("");
    } else if (originalOwner || boundOwner) {
      const owner = originalOwner || boundOwner;
      const saved = state.conversationWorkbenchStates[owner];
      if (saved?.draft === input) useAppStore.setState(current => ({ conversationWorkbenchStates: {
        ...current.conversationWorkbenchStates, [owner]: { ...saved, draft: "", richDraft: undefined },
      } }));
    }
    if (!originalOwner) {
      const key = draftId || composerWorkspaceKey(workspace), saved = useAppStore.getState().workspaceComposerDrafts[key];
      if (saved?.draft === input) useAppStore.setState(current => ({ workspaceComposerDrafts: {
        ...current.workspaceComposerDrafts, [key]: { ...saved, draft: "", richDraft: undefined },
      } }));
    }
  };

  const completeSubmission = (owner: string, submission: object | undefined) => {
    if (submission && submittingRef.current.get(owner) === submission) {
      submittingRef.current.delete(owner);
      setSubmittingOwners(new Set(submittingRef.current.keys()));
    }
  };

  const runSubmission = async (action: () => Promise<void>) => {
    if (submittingRef.current.has(editorKey)) return;
    const submission = {};
    submittingRef.current.set(editorKey, submission);
    setSubmittingOwners(new Set(submittingRef.current.keys()));
    try {
      await action();
    } catch (error) {
      pushToast(error instanceof Error ? error.message : tr("消息发送失败，请重试。"), "error", 4500);
    } finally {
      completeSubmission(editorKey, submission);
    }
  };

  const submit = () => {
    if (hasPendingApproval) return;
    return runSubmission(() => submitCurrentDraft(useAppStore.getState().followUpBehavior));
  };

  const submitAlternateFollowUp = () => {
    if (hasPendingApproval) return;
    const behavior = useAppStore.getState().followUpBehavior === "queue" ? "steer" : "queue";
    return runSubmission(() => submitCurrentDraft(behavior));
  };

  const submitCurrentDraft = async (busyBehavior: FollowUpBehavior) => {
    if (useAppStore.getState().pendingConversationSwitchId) return;
    if (sendState === "stop" && !draft.trim()) return;
    if (sendState !== "idle" && sendState !== "queue" && sendState !== "offline-queue") return;
    if (useAppStore.getState().goalMode && capabilityFeatureEnabled(useAppStore.getState().runtimeCapabilities, "goals")) {
      await submitComposerGoal(tr);
      return;
    }
    const text = hasRichComposerContent(richDraft, draft) ? draft.trim() : "";
    const content = selectedSlashCommand
      ? [selectedSlashCommand, text].filter(Boolean).join(" ")
      : text;

    const slashInput = parseRuntimeSlashInput(content);
    if (slashInput?.commandLine.trim() === "/goal" && useAppStore.getState().slashCommands.some(command => command.name === "goal" && command.source === "builtin")) {
      if (!enterComposerGoalMode()) return;
      setDraft(""); setSelectedSlashCommand(null); closeSlashPanel();
      return;
    }
    if (slashInput?.commandLine === "/approve" && useAppStore.getState().slashCommands.find(command => command.name === "approve")?.source === "builtin") {
      openGuardianApproval();
      return;
    }
    if (slashInput?.commandLine === "/review" && useAppStore.getState().slashCommands.some(command => command.name === "review" && command.source === "builtin")) {
      openCodeReview();
      return;
    }
    if (slashInput?.command === "/compact") {
      await executeSlashCommand(slashInput.commandLine, undefined, busyBehavior);
      return;
    }
    const composerAttachments = useAppStore.getState().attachments;
    const blockingAttachment = composerAttachments.find((a) => a.status !== "ready" || !a.attachment);
    if (blockingAttachment) {
      const message = blockingAttachment.status === "uploading"
        ? tr("“{name}”仍在上传，请等待完成后发送。", { name: blockingAttachment.name })
        : blockingAttachment.error
          ? `“${blockingAttachment.name}”${blockingAttachment.error}`
          : tr("“{name}”上传失败，请移除或重试后发送。", { name: blockingAttachment.name });
      pushToast(message, "warning", 3500);
      return;
    }

    const readyComposerAttachments = composerAttachments
      .filter((a) => a.status === "ready" && a.attachment);
    const missingOwner = readyComposerAttachments.find((attachment) => (
      !String(attachment.conversationId || "").trim()
    ));
    if (missingOwner) {
      pushToast(tr("“{name}”没有绑定会话，请重新上传。", { name: missingOwner.name }), "error", 4200);
      return;
    }
    const attachmentOwners = new Set(
      readyComposerAttachments
        .map((attachment) => String(attachment.conversationId || "").trim())
        .filter(Boolean),
    );
    if (attachmentOwners.size > 1) {
      pushToast(tr("这些附件来自不同会话，请移除后在同一会话重新上传。"), "error", 4500);
      return;
    }
    const attachmentConversationId = [...attachmentOwners][0] || "";
    const currentConversationId = String(useAppStore.getState().conversationId || "").trim();
    if (
      attachmentConversationId
      && currentConversationId
      && attachmentConversationId !== currentConversationId
    ) {
      pushToast(tr("附件属于另一个会话，请切回附件所在会话后发送。"), "warning", 4500);
      return;
    }
    if (
      attachmentConversationId
      && !currentConversationId
      && !await acceptAttachmentConversationOwner(attachmentConversationId)
    ) {
      pushToast(tr("附件已绑定到另一个会话，请切回该会话后发送。"), "warning", 4500);
      return;
    }
    const sendConversationId = attachmentConversationId || currentConversationId;
    const readyAttachments = readyComposerAttachments.map((a) => a.attachment as Record<string, unknown>);
    const attachmentRefs = readyComposerAttachments.map((a) => {
      const payload = a.attachment as Record<string, unknown>;
      const kind = String(payload.kind || (a.type.startsWith("image/") ? "image" : "document"));
      return {
        id: String(payload.id || a.id),
        name: String(payload.file_name || a.name),
        kind: kind === "image" ? "image" as const : kind === "document" ? "document" as const : "file" as const,
        mediaType: String(payload.media_type || a.type),
        sizeBytes: Number(payload.size_bytes || a.size || 0),
        artifactId: String(payload.artifact_id || a.artifactId || ""),
        docId: String(payload.doc_id || a.docId || ""),
        dataUrl: a.type.startsWith("image/") ? a.dataUrl : undefined,
        inputSource: payload.input_source === "pasted_text" || a.inputSource === "pasted_text"
          ? "pasted_text" as const
          : "upload" as const,
        sourceCharCount: Number(payload.source_char_count ?? a.sourceCharCount ?? 0) || undefined,
      };
    });

    const finalContent = content;
    // Establish the draft owner before capturing the submission fingerprint.
    // sendUserMessage also materializes a draft for a blank workspace, but
    // doing it there is too late for clearSubmittedComposer: the owner changes
    // from the blank surface to the new conversation while the original
    // submission still needs to be claimed and cleared.
    const stateBeforeSubmission = useAppStore.getState();
    if (!stateBeforeSubmission.conversationId && !stateBeforeSubmission.activeConversationDraftId) {
      stateBeforeSubmission.startConversationDraft({ bindWorkspace: Boolean(stateBeforeSubmission.workingDirectory), appMode: stateBeforeSubmission.appMode });
    }
    const submissionState = useAppStore.getState();
    const composerStateAtSend = composerFingerprint(submissionState);
    const capturedComposer = composerDraftOf(submissionState);
    const capturedDraft = { draftId: submissionState.activeConversationDraftId, command: selectedSlashCommandRef.current };
    const submittedComments = cloneDiffComments(readDiffCommentState(getDiffCommentScope(
      submissionState.conversationId || submissionState.activeConversationDraftId, submissionState.workingDirectory)).comments);
    const conversationAtSend = (submissionState.conversationId || "").trim();
    const workspaceAtSend = submissionState.workingDirectory;
    const submittedUserMessageId = uniqueMessageId("u");
    const submittedOperation = submittingRef.current.get(editorKey);
    let admittedConversationId = conversationAtSend;
    const accepted: { result?: CommandResultEvent } = {};
    let draftClaim: ReturnType<typeof claimSubmittedDraft> | undefined;
    let locallySubmitted = false;
    let explicitlyRejected = false;
    let failureReason: string | undefined;
    let draftRestored = false;
    let draftRetained = false;
    const quoteContext = quotedMessage ? formatQuotedMessageForBackend(quotedMessage) : "";
    const displayContent = content;

    if (slashInput) {
      for (const mention of slashInput.mentions) addSelectedMention(mention);
      await executeSlashCommand(slashInput.commandLine, { attachments: readyAttachments, attachmentRefs, conversationId: sendConversationId || undefined, quotedMessage }, busyBehavior);
      return;
    }

    const submittedContextRefs = [...selectedContextReferences(capturedComposer),
      ...submittedComments.map(comment => ({ kind: "diff_comment" as const, name: comment.position.path,
        path: comment.position.path, comment }))];
    const submittedUserMessage = {
      id: submittedUserMessageId, role: "user" as const, content: displayContent, timestamp: Date.now(), artifacts: [],
      attachmentRefs, quotedMessage, contextRefs: submittedContextRefs, deliveryStatus: "sending" as const,
    };
    if (capturedDraft.draftId) useAppStore.setState(state => ({ conversationDrafts: { ...state.conversationDrafts,
      [capturedDraft.draftId!]: { ...state.conversationDrafts[capturedDraft.draftId!], pendingUserMessage: submittedUserMessage } } }));
    else if (conversationAtSend && !capturedComposer.queuedEdit) useAppStore.setState(state => ({
      preparingComposerMessages: { ...state.preparingComposerMessages, [conversationAtSend]: submittedUserMessage },
    }));
    const clearPreparingMessage = () => useAppStore.setState(state => {
      if (state.preparingComposerMessages[conversationAtSend]?.id !== submittedUserMessageId) return state;
      const preparingComposerMessages = { ...state.preparingComposerMessages };
      delete preparingComposerMessages[conversationAtSend];
      return { preparingComposerMessages };
    });
    const consumeDraft = () => {
      if (!capturedComposer.queuedEdit) retainPendingComposerInput({
        owner: conversationAtSend ? { kind: "conversation", id: conversationAtSend, workspaceRoot: workspaceAtSend }
          : { kind: "draft", id: capturedDraft.draftId!, workspaceRoot: workspaceAtSend },
        message: submittedUserMessage, draft: capturedComposer, comments: submittedComments,
        sourceInputId: !capturedComposer.retainedInputEdit?.completed ? capturedComposer.retainedInputEdit?.inputId : undefined,
      });
      clearSubmittedComposer(composerStateAtSend, conversationAtSend, workspaceAtSend, submittedUserMessageId,
        admittedConversationId, finalContent, submittedComments, capturedDraft);
      draftClaim = claimSubmittedDraft({ conversationId: conversationAtSend, draftId: capturedDraft.draftId,
        workspaceRoot: workspaceAtSend, userMessageId: submittedUserMessageId,
        selectedCommand: () => selectedSlashCommandRef.current });
      submittedDraftClaims.current.add(draftClaim);
    };
    const restoreDraft = () => {
      if (draftRestored) return true;
      if (!draftClaim?.canRestore()) return false;
      const { owner, active, scope } = draftClaim.location();
      const state = useAppStore.getState();
      if (locallySubmitted && owner && state.getVisibleMessages(owner).filter(message => message.role === "user").at(-1)?.id !== submittedUserMessageId) return false;
      const restored = { draft: capturedComposer.draft, richDraft: capturedComposer.richDraft, attachments: capturedComposer.attachments.map(attachment => ({ ...attachment,
        ...(attachment.localFile && attachment.dataUrl?.startsWith("blob:") ? { dataUrl: URL.createObjectURL(attachment.localFile) } : {}),
      })), selectedMentions: capturedComposer.selectedMentions, selectedSkills: capturedComposer.selectedSkills, quotedMessage: capturedComposer.quotedMessage };
      if (active) useAppStore.setState(restored);
      else if (owner && !(capturedDraft.draftId && !state.conversationDrafts[capturedDraft.draftId]?.adopted)) useAppStore.setState(current => ({ conversationWorkbenchStates: { ...current.conversationWorkbenchStates,
        [owner]: { ...emptyConversationWorkbenchState(), ...current.conversationWorkbenchStates[owner], ...restored } } }));
      else useAppStore.setState(current => ({ workspaceComposerDrafts: { ...current.workspaceComposerDrafts,
        [capturedDraft.draftId || composerWorkspaceKey(workspaceAtSend)]: { ...emptyComposerDraft(), ...current.workspaceComposerDrafts[capturedDraft.draftId || composerWorkspaceKey(workspaceAtSend)], ...restored } } }));
      restoreSubmittedDiffComments(scope, submittedComments);
      draftRestored = true;
      return true;
    };
    const preserveRejectedDraft = () => {
      if (!draftClaim || draftRetained) return;
      if (restoreDraft()) { discardRetainedComposerInput(submittedUserMessageId); return; }
      if (capturedComposer.queuedEdit) return;
      const state = useAppStore.getState();
      const context = capturedDraft.draftId ? state.conversationDrafts[capturedDraft.draftId] : undefined;
      const owner = context && !context.adopted ? { kind: "draft" as const, id: context.id, workspaceRoot: context.workspaceRoot }
        : { kind: "conversation" as const, id: admittedConversationId || conversationAtSend, workspaceRoot: context?.executionCwd ?? workspaceAtSend };
      if (owner.id && (!capturedDraft.draftId || context)) {
        retainFailedComposerInput({ owner,
          message: { ...submittedUserMessage, failureMessage: failureReason || context?.selectionError || undefined },
          draft: composerDraftOf(capturedComposer), comments: submittedComments,
          sourceInputId: !capturedComposer.retainedInputEdit?.completed ? capturedComposer.retainedInputEdit?.inputId : undefined });
        draftRetained = true;
      }
    };
    let sent = false;
    try { sent = await sendUserMessage(finalContent, readyAttachments, {
      attachmentRefs,
      conversationId: sendConversationId || undefined,
      displayContent,
      quotedMessage,
      backendContent: [quoteContext, finalContent].filter(Boolean).join("\n\n"),
      allowWhileStreaming: true,
      busyBehavior,
      awaitAdmission: true,
      userMessageId: submittedUserMessageId,
      onPreparing: capturedComposer.queuedEdit ? undefined : consumeDraft,
      onAdmission: event => {
        accepted.result = event;
        if (typeof event.data?.conversation_id === "string") admittedConversationId = event.data.conversation_id;
        if (event.data?.native_completion === "completed") completeRetainedInputEdit(submittedUserMessageId);
      },
      onModelAccepted: () => {
        completeRetainedInputEdit(submittedUserMessageId);
      },
      onConversationCreated: owner => { admittedConversationId = owner; },
      onSubmitted: () => {
        locallySubmitted = true;
        if (!draftClaim) consumeDraft();
        clearPreparingMessage();
        completeSubmission(editorKey, submittedOperation);
      },
      onRejected: reason => { explicitlyRejected = true; failureReason = reason; preserveRejectedDraft(); },
    }); } catch (error) {
      failureReason = error instanceof Error ? error.message : String(error);
      throw error;
    } finally {
      if (!sent && (!locallySubmitted || explicitlyRejected)) preserveRejectedDraft();
      clearPreparingMessage();
      if (draftClaim) { draftClaim.release(); submittedDraftClaims.current.delete(draftClaim); }
      if (capturedDraft.draftId) useAppStore.setState(state => {
        const context = state.conversationDrafts[capturedDraft.draftId!];
        return context?.pendingUserMessage?.id === submittedUserMessageId ? { conversationDrafts: { ...state.conversationDrafts,
          [context.id]: { ...context, pendingUserMessage: undefined } } } : state;
      });
    }
    if (!sent) return;
    completeRetainedInputEdit(submittedUserMessageId);
    if (accepted.result?.data?.native_completion === "completed") {
      clearSubmittedInput(capturedComposer.draft, conversationAtSend, workspaceAtSend, capturedDraft.command, admittedConversationId, capturedDraft.draftId);
      return;
    }
    if (!locallySubmitted && !draftClaim) clearSubmittedComposer(composerStateAtSend, conversationAtSend, workspaceAtSend, submittedUserMessageId, admittedConversationId, finalContent, submittedComments, capturedDraft);
  };

  const executeSlashCommand = async (commandLine: string, prepared?: { attachments: Record<string, unknown>[]; attachmentRefs: MessageAttachmentRef[]; conversationId?: string; quotedMessage?: ComposerQuote | null },
    busyBehavior: FollowUpBehavior = useAppStore.getState().followUpBehavior) => {
    const composerStateAtSend = composerFingerprint();
    const originalState = useAppStore.getState();
    const originalInput = originalState.draft;
    const originalSelectedCommand = selectedSlashCommandRef.current;
    const originalOwner = (originalState.conversationId || "").trim();
    const capturedDraft = { draftId: originalState.activeConversationDraftId, command: originalSelectedCommand };
    const workspace = originalState.workingDirectory;
    const submittedComments = cloneDiffComments(readDiffCommentState(getDiffCommentScope(originalOwner || capturedDraft.draftId, workspace)).comments);
    const userMessageId = uniqueMessageId("u");
    const submittedOperation = submittingRef.current.get(editorKey);
    let admittedOwner = originalOwner;
    let compactReceived = false;
    let inputConsumed = false;
    const consumeSlashInput = (wholeComposer: boolean) => {
      if (inputConsumed || isComposerConversationDeleted(admittedOwner)) return;
      inputConsumed = true;
      if (wholeComposer) clearSubmittedComposer(composerStateAtSend, originalOwner, workspace, userMessageId, admittedOwner, undefined, submittedComments, capturedDraft);
      else clearSubmittedInput(originalInput, originalOwner, workspace, originalSelectedCommand, admittedOwner, capturedDraft.draftId);
      completeSubmission(editorKey, submittedOperation);
    };
    const result = await executeRuntimeSlashCommand(commandLine, {
      getState: useAppStore.getState,
      setState: useAppStore.setState,
      sendClientCommand,
      sendChatMessage: (options) => sendRuntimeSlashMessage(options, prepared, { userMessageId, onReceived: () => {
        if (!options.skipLocalAppend || parseRuntimeSlashInput(commandLine)?.command !== "/compact") return;
        compactReceived = true;
        consumeSlashInput(false);
      }, onAdmission: event => {
        if (typeof event.data?.conversation_id === "string") admittedOwner = event.data.conversation_id;
        if (event.data?.native_completion === "completed") consumeSlashInput(false);
      }, onModelAccepted: () => {
        consumeSlashInput(true);
      }, onConversationCreated: owner => { admittedOwner = owner; } }, busyBehavior),
      sendUserMessage,
      confirmClear: async () => {
        const { showConfirm } = await import("../overlays/DialogService");
        return showConfirm({
          title: tr("清空会话"),
          message: tr("清空当前会话视图中的全部消息？此操作无法撤销。"),
          confirmLabel: tr("清空"),
          danger: true,
        });
      },
    });
    if (compactReceived || inputConsumed) return;
    if (result.reset === "composer") {
      consumeSlashInput(true);
      return;
    }
    if (result.reset === "input") {
      consumeSlashInput(false);
    }
  };

  const handleChange = (v: string, selection: ComposerSelection = { start: v.length, end: v.length }, rich?: RichComposerDocument) => {
    setReferenceInsertion(undefined);
    if (reviewOwner && v !== reviewOwner.input) setReviewOwner(undefined);
    if (guardianApprovalOwner && v !== guardianApprovalOwner.input) setGuardianApprovalOwner(undefined);
    if (historyOpen) setHistoryOpen(false);
    // Reset the history-recall cursor whenever the draft changes for a reason
    // other than a recall fill, so the next ArrowUp starts from the live draft.
    if (historyCursorRef.current !== -1 && v !== historySavedDraftRef.current) {
      const items = readPromptHistory(workingDirectory);
      if (v !== items[historyCursorRef.current]) historyCursorRef.current = -1;
    }
    if (rich) {
      const references = inlineComposerReferences(rich);
      useAppStore.setState(state => ({ draft: v, richDraft: v ? rich : undefined,
        selectedMentions: [...references.filter((reference): reference is Exclude<InlineComposerReference, { kind: "skill" }> => reference.kind !== "skill"),
          ...state.selectedMentions.filter(reference => reference.kind === "browser_annotation")],
        selectedSkills: references.filter((reference): reference is Extract<InlineComposerReference, { kind: "skill" }> => reference.kind === "skill"),
      }));
    } else setDraft(v);
    refreshInputMenus(v, selection);
  };

  const refreshInputMenus = (v: string, selection: ComposerSelection) => {
    inputSelectionRef.current = selection;
    const document = useAppStore.getState().richDraft;
    if (document && (document.selection?.start !== selection.start || document.selection?.end !== selection.end)) {
      useAppStore.setState({ richDraft: { ...document, selection } });
    }
    if (reviewOwner && v === reviewOwner.input) return;
    if (selection.composing || selection.start !== selection.end) {
      closeMentionPanel(); closeSlashPanel(); setSkillPanelOpen(false); setMenuFilter(""); return;
    }
    syncRuntimeSlashPanelForDraft(v, {
      slashPanelOpen,
      openSlashPanel,
      closeSlashPanel,
      setMenuFilter,
      sendClientCommand,
    });

    const token = composerTokenAtSelection(v, selection);
    if (token?.kind === "skill" && !slashPanelOpen) {
      closeMentionPanel();
      closeSlashPanel();
      setSkillPanelOpen(true);
      setMenuFilter(token.value);
      return;
    }

    if (skillPanelOpen) {
      setSkillPanelOpen(false);
      setMenuFilter("");
    }

    if (token?.kind === "mention") {
      setSkillPanelOpen(false);
      if (!mentionPanelOpen) openMentionPanel();
      setMenuFilter(token.value);
    } else if (mentionPanelOpen) {
      closeMentionPanel();
      setMenuFilter("");
    }
  };

  const handleMenuSelect = (value: string, reference?: InlineComposerReference) => {
    if (!value) {
      closeSlashPanel();
      closeMentionPanel();
      setSkillPanelOpen(false);
      setMenuFilter("");
      return;
    }

    const selectedToken = composerTokenAtSelection(draft, inputSelectionRef.current);
    const insertReference = (reference: InlineComposerReference, selection = selectedToken
      ? { start: selectedToken.start, end: selectedToken.end } : inputSelectionRef.current) => {
      setReferenceInsertion({ owner: editorKey, request: { reference, selection } });
      setMentionResults([]); closeMentionPanel(); closeSlashPanel(); setSkillPanelOpen(false); setMenuFilter("");
    };
    if (skillPanelOpen) {
      const encodedPath = value.match(/^skill-path:(.+)$/)?.[1];
      const encodedName = value.match(/^skill-name:(.+)$/)?.[1];
      const skillPath = encodedPath ? decodeURIComponent(encodedPath) : "";
      const skillName = encodedName ? decodeURIComponent(encodedName) : value.replace(/^\$/, "");
      const skill = useAppStore.getState().availableSkills.find((item) => (
        skillPath
          ? workspaceFilePathsEqual(item.path, skillPath, workingDirectory)
          : item.name === skillName
      ));
      if (skill) {
        insertReference(composerSkillReference(skill));
      }
      setSkillPanelOpen(false);
      setMenuFilter("");
      return;
    }

    if (slashPanelOpen) {
      if (value === "/goal" && useAppStore.getState().slashCommands.some(command => command.name === "goal" && command.source === "builtin")) {
        if (!enterComposerGoalMode()) return;
        setSelectedSlashCommand(null);
        setDraft(draft.replace(/^\s*\/[\w-]+\s*/, ""));
        closeSlashPanel(); setMenuFilter("");
        return;
      }
      if (value === "/approve" && useAppStore.getState().slashCommands.find(command => command.name === "approve")?.source === "builtin") {
        openGuardianApproval();
        return;
      }
      if (value === "/review" && useAppStore.getState().slashCommands.some(command => command.name === "review" && command.source === "builtin")) {
        openCodeReview();
        return;
      }
      const selection = resolveRuntimeSlashMenuSelection(value, useAppStore.getState());
      if (selection.kind === "skill_picker") {
        setSelectedSlashCommand(null);
        setDraft("/skill ");
        setMenuFilter("/skill ");
        sendClientCommand({ type: "skills.list" });
        return;
      }
      if (selection.kind === "skill") {
        const metadata = skillCatalog.find(item => selection.skill.path ? workspaceFilePathsEqual(item.path, selection.skill.path, workingDirectory) : item.name === selection.skill.name);
        insertReference(metadata ? composerSkillReference(metadata) : { ...selection.skill, kind: "skill" }, { start: 0, end: draft.length });
        setSelectedSlashCommand(null);
        closeSlashPanel();
        setMenuFilter("");
        return;
      }
      if (selection.kind === "tokenize") {
        setSelectedSlashCommand(selection.command);
        setDraft("");
      } else if (selection.kind === "execute") {
        setSelectedSlashCommand(null);
        void runSubmission(() => executeSlashCommand(selection.commandLine));
      }
      closeSlashPanel();
      setMenuFilter("");
    } else if (mentionPanelOpen) {
      if (reference?.kind === "url") { insertReference(reference); return; }
      const encodedPlugin = value.match(/^plugin:(.+)$/)?.[1];
      if (encodedPlugin) {
        const configName = decodeURIComponent(encodedPlugin);
        insertReference(reference ?? {
          kind: "plugin",
          name: configName,
          configName,
          path: `plugin://${configName}`,
        });
        setMentionResults([]);
        closeMentionPanel();
        setMenuFilter("");
        return;
      }
      const typed = value.match(/^(file|folder):(.*)$/);
      const rawPath = appendComposerTokenAnchor(typed ? typed[2] : value, selectedToken?.value ?? "");
      const kind = typed?.[1] === "folder" || rawPath.endsWith("/") || rawPath.endsWith("\\") ? "folder" : "file";
      const name = rawPath.split(/[/\\]/).filter(Boolean).pop() || rawPath;

      insertReference({ path: rawPath, name, kind: kind as "file" | "folder", workspaceRoot: workingDirectory });
      setMentionResults([]);
      closeMentionPanel();
      setMenuFilter("");
    }
  };

  useEffect(() => {
    const onClickOutside = (e: MouseEvent) => {
      if (!containerRef.current) return;
      if (!containerRef.current.contains(e.target as Node)) {
        closeSlashPanel();
        closeMentionPanel();
        setSkillPanelOpen(false);
        setMenuFilter("");
      }
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [closeSlashPanel, closeMentionPanel]);

  // Request full command/skill lists once; websocket reconnect already refreshes
  // command metadata, so repeated Composer remounts should not spam the backend.
  useEffect(() => {
    if (minimal || initialCatalogRequested) return;
    initialCatalogRequested = true;
    if (useAppStore.getState().activeConversationDraftId) return;
    sendClientCommand({ type: "commands.list" });
    sendClientCommand({ type: "skills.list" });
  }, [minimal]);

  const handleDrop = (e: React.DragEvent) => {
    if (hasPendingApproval || pendingConversationSwitchId) return;
    const files = e.dataTransfer.files;
    if (!files.length) return;
    e.preventDefault();
    uploadComposerFiles(Array.from(files));
  };

  const handleComposerFiles = (files: File[]) => {
    uploadComposerFiles(files);
  };

  return (
    <>
      {!minimal && <TurnPlanProgress wide={wideMode} />}
      <QueuedMessageList wide={wideMode} minimal={minimal} />
      <div
        ref={containerRef}
        onDragOver={(e) => {
          if (hasPendingApproval || pendingConversationSwitchId) return;
          if (!e.dataTransfer.types.includes("Files")) return;
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragOver(false);
        }}
        onDropCapture={() => setDragOver(false)}
        onDrop={handleDrop}
        className="composer-container relative mx-auto flex flex-col transition-[background_140ms_ease,border-color_300ms_ease,box-shadow_140ms_ease]"
        data-approval-replacement={hasPendingApproval || planImplementation ? "true" : "false"}
        data-command-mode={commandModeActive ? "true" : "false"}
        data-goal-mode={goalMode ? "true" : "false"}
        data-drag-over={dragOver ? "true" : "false"}
        data-empty={!draft.length && !hasComposerAttachments && !selectedSkills.length && !selectedMentions.length && !quotedMessage && !activeGoal && !activeSlashCommand && !commentState.comments.length
          && !hasPendingPrompt && !goalMode ? "true" : "false"}
        data-layout-mode={codeLayout ? "code" : "cowork"}
        style={{
          position: "relative",
          left: undefined,
          bottom: undefined,
          transform: undefined,
          zIndex: minimal ? undefined : "var(--z-composer)",
          display: "flex",
          flexDirection: "column",
          width: minimal ? "100%" : wideMode ? "var(--chat-wide-axis-width)" : "var(--chat-composer-axis-width)",
          marginBottom: codeLayout ? "14px" : 0,
          padding: codeLayout ? "0" : "8px 10px 10px",
        }}
      >
      {!pendingConversationSwitchId && <InlineAgentPrompt />}
      {!hasPendingPrompt && planImplementation && <PlanImplementationPrompt key={`${conversationId}:${planImplementation.messageId}`} owner={conversationId!} plan={planImplementation} />}
      <div className="composer-input-region" hidden={hasPendingApproval || Boolean(planImplementation)}>
      {!minimal && emptyConversation && !isStreaming && <ProjectScopePicker />}
      {activeGoal && <GoalBar />}
      <ContextChipRegion />
      <AttachmentStrip />
      <DiffCommentAttachments key={commentScope} comments={commentState.comments}
        onRemove={comment => removeDiffComment(commentScope, diffCommentKey(comment))}
        onRemoveAll={() => clearSubmittedDiffComments(commentScope, commentState.comments)} />
      {quotedMessage && <MessageQuote message={quotedMessage} onRemove={clearQuotedMessage} />}
      <ComposerTextarea
        rich
        richDocument={richDraft}
        documentKey={editorKey}
        references={inlineReferences}
        referenceInsertion={referenceInsertion?.owner === editorKey ? referenceInsertion.request : undefined}
        workspaceRoot={workingDirectory}
        value={draft}
        onChange={handleChange}
        onSelectionChange={refreshInputMenus}
        selectionRequest={selectionRequest}
        onSubmit={submit}
        onAlternateSubmit={submitAlternateFollowUp}
        isResponseInProgress={isStreaming}
        menuOpen={Boolean(reviewOwner && !reviewOwner.dismissed) || slashPanelOpen || mentionPanelOpen || skillPanelOpen || historyOpen}
        onHistorySearch={openPromptHistory}
        onDropFiles={handleComposerFiles}
        minimal={minimal}
        commandMode={commandModeActive}
        commandLabel={selectedSlashCommand ? selectedSlashCommand.slice(1) : null}
        onRecallHistory={recallHistory}
        onEscape={escapeInterrupt}
        onClearCommand={() => setSelectedSlashCommand(null)}
        skillTokens={selectedSkills.map((skill) => {
          const metadata = skillCatalog.find((item) => skill.path ? item.path === skill.path : item.name === skill.name);
          return { name: skill.name, displayName: metadata?.display_name, description: skill.description, icon: metadata?.icon };
        })}
        onRemoveSkill={removeSelectedSkill}
        onRemoveLastSkill={() => {
          const last = useAppStore.getState().selectedSkills.at(-1);
          if (last) removeSelectedSkill(last.name);
        }}
        placeholder={selectedSlashCommand ? tr("补充指令…") : tr("随心输入")}
      />
      <MenuOverlay
        open={!hasPendingApproval && !guardianApprovalOwner && !(reviewOwner && !reviewOwner.dismissed) && (slashPanelOpen || mentionPanelOpen || skillPanelOpen)}
        kind={slashPanelOpen ? "slash" : skillPanelOpen ? "skill" : "mention"}
        filter={menuFilter}
        onSelect={handleMenuSelect}
        placement={minimal ? "below" : "above"}
      />
      {reviewOwner && !reviewOwner.dismissed && !pendingConversationSwitchId && <Suspense fallback={null}><CodeReviewMenu owner={reviewOwner} pending={submitting || reviewPermissionPending || !isConnected || !currentModel}
        onClose={dismissCodeReview} onSelect={startCodeReview} /></Suspense>}
      {guardianApprovalOwner && !pendingConversationSwitchId && <GuardianApprovalMenu conversationId={guardianApprovalOwner.conversationId}
        onClose={() => setGuardianApprovalOwner(undefined)} onApproved={finishGuardianApproval} />}
      <FooterRow
        sendState={sendState}
        submitting={submitting}
        isStopping={isStopping}
        onSend={sendState === "stop" ? stopRun : submit}
        onStop={stopRun}
        compact={codeLayout}
        minimal={minimal}
      />
      <PromptHistoryOverlay
        open={historyOpen && !hasPendingApproval}
        items={historyItems}
        placement={minimal ? "below" : "above"}
        onClose={() => setHistoryOpen(false)}
        onSelect={(prompt) => {
          const selection = { start: prompt.length, end: prompt.length };
          handleChange(prompt, selection);
          setSelectionRequest(selection);
        }}
        onClear={() => {
          clearPromptHistory(workingDirectory);
          setHistoryItems([]);
        }}
      />
      </div>
      </div>
    </>
  );
};

const formatQuotedMessageForBackend = (quote: ComposerQuote): string => {
  const speaker = quote.role === "user" ? "User" : quote.role === "assistant" ? "Assistant" : "System";
  return [`Quoted ${speaker} message:`, quote.content.trim()].filter(Boolean).join("\n");
};

const buildSkillInvocationLine = (skills: Array<{ name: string }>): string =>
  skills.map((skill) => `$${skill.name}`).join(" ");
