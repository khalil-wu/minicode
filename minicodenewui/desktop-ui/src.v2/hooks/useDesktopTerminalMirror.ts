import { useEffect } from "react";
import { desktop, ptyList, ptySnapshot } from "../desktop/runtime";
import { sendClientCommand } from "../protocol/ws-outbox";
import { pushToast } from "../overlays/ToastContainer";
import { useAppStore } from "../stores";

type OutputChunk = { sessionId: string; conversationId: string; data: string; startCursor?: number; endCursor?: number };
type ExitEvent = { sessionId: string; conversationId: string; exitCode: number | null; exitSignal?: number | string | null; exitedAt?: number };

/** Mirror native terminals for the renderer lifetime, including a closed dock. */
export const useDesktopTerminalMirror = () => {
  const connected = useAppStore((state) => state.isConnected);
  useEffect(() => {
    const pty = desktop()?.pty;
    if (!pty || !connected) return;
    let active = true;
    const registered = new Set<string>();
    const queriedOwners = new Set<string>();
    const pending = new Map<string, { chunks: OutputChunk[]; exit?: ExitEvent }>();

    const sendOutput = (chunk: OutputChunk) => sendClientCommand({
      type: "terminal.mirror.output", session_id: chunk.sessionId,
      conversation_id: chunk.conversationId, data: chunk.data,
      start_cursor: chunk.startCursor, end_cursor: chunk.endCursor,
    }, { silent: true });
    const sendExit = (event: ExitEvent) => sendClientCommand({
      type: "terminal.mirror.exit", session_id: event.sessionId,
      conversation_id: event.conversationId, exit_code: event.exitCode,
      exit_signal: event.exitSignal, exited_at: event.exitedAt,
    }, { silent: true });

    const synchronize = (id: string, owner: string) => {
      if (registered.has(id) || pending.has(id)) return;
      const capture: { chunks: OutputChunk[]; exit?: ExitEvent } = { chunks: [] };
      pending.set(id, capture);
      void ptySnapshot(id, owner).then((snapshot) => {
        if (!active || !useAppStore.getState().isConnected || !snapshot) return;
        const output = snapshot.output ?? "";
        const end = snapshot.outputEndCursor ?? output.length;
        if (!sendClientCommand({
          type: "terminal.mirror.created", session_id: id, conversation_id: owner,
          pid: snapshot.pid, cwd: snapshot.cwd, shell: snapshot.shell,
          is_alive: snapshot.isAlive !== false, output,
          exit_code: snapshot.exitCode, exit_signal: snapshot.exitSignal, exited_at: snapshot.exitedAt,
          output_start_cursor: end - output.length, output_end_cursor: end,
        }, { silent: true })) return;
        registered.add(id);
        // The authoritative snapshot can overlap these chunks. The same cursor
        // contract on the backend removes overlap without comparing the text.
        for (const chunk of capture.chunks) sendOutput(chunk);
        if (capture.exit) sendExit(capture.exit);
      }).catch((error) => {
        if (active) pushToast(`终端同步失败：${error instanceof Error ? error.message : String(error)}`, "error", 5000);
      }).finally(() => pending.delete(id));
    };

    const removeData = pty.onData((chunk) => {
      if (!useAppStore.getState().isConnected) return;
      if (registered.has(chunk.sessionId)) sendOutput(chunk);
      else {
        synchronize(chunk.sessionId, chunk.conversationId);
        pending.get(chunk.sessionId)?.chunks.push(chunk);
      }
    });
    const removeExit = pty.onExit((event) => {
      if (!useAppStore.getState().isConnected) return;
      if (registered.has(event.sessionId)) sendExit(event);
      else {
        synchronize(event.sessionId, event.conversationId);
        const capture = pending.get(event.sessionId);
        if (capture) capture.exit = event;
      }
      const state = useAppStore.getState();
      const current = state.terminalSessions.find((item) => item.id === event.sessionId && item.conversationId === event.conversationId);
      if (current) state.upsertTerminalSession({ ...current, status: "exited", exitCode: event.exitCode,
        exitSignal: event.exitSignal, exitedAt: event.exitedAt ?? Date.now() });
    });

    const discoverOwners = () => {
      const state = useAppStore.getState();
      const owners = new Set([state.conversationId, ...state.conversations.map((item) => item.id)]);
      for (const owner of owners) {
        if (!owner || queriedOwners.has(owner)) continue;
        queriedOwners.add(owner);
        void ptyList(owner).then((sessions) => {
          if (active) for (const session of sessions) synchronize(session.sessionId, owner);
        }).catch((error) => {
          if (active) pushToast(`终端列表恢复失败：${String(error)}`, "error", 5000);
        });
      }
    };
    const discoverSessions = () => {
      for (const session of useAppStore.getState().terminalSessions) {
        if (session.terminalMode === "pty") synchronize(session.id, session.conversationId);
      }
    };
    const unsubscribe = useAppStore.subscribe((state, previous) => {
      if (state.conversations !== previous.conversations || state.conversationId !== previous.conversationId) discoverOwners();
      if (state.terminalSessions !== previous.terminalSessions) discoverSessions();
    });
    discoverOwners();
    discoverSessions();
    return () => {
      active = false;
      removeData?.();
      removeExit?.();
      unsubscribe();
      for (const capture of pending.values()) capture.chunks.length = 0;
      pending.clear();
    };
  }, [connected]);
};
