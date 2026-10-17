import type { GitDiffContentRequest, GitDiffContentResult } from "../../protocol/workspace-types";
import type { GitDiffSnapshot } from "../../stores/types";
import { commandResultSucceeded, createClientCommandId, sendClientCommandAwaitResult } from "../../protocol/ws-outbox";

type PendingRead = { request: GitDiffContentRequest; resolve: (result: GitDiffContentResult) => void; reject: (error: unknown) => void };
type ReadBatch = { snapshot: GitDiffSnapshot; reads: PendingRead[] };
type ContentsReply = { status?: "stale-snapshot"; source: string; snapshot_generation: string; results: GitDiffContentResult[] };
const pendingBatches = new Map<string, ReadBatch>();

// Source groups the same host/cwd/snapshot for 40ms, at most four objects per RPC.
export function readGitFileContents(snapshot: GitDiffSnapshot, requests: GitDiffContentRequest[]): Promise<GitDiffContentResult[]> {
  const key = JSON.stringify([snapshot.conversationId, snapshot.workspaceRoot, snapshot.environmentId,
    snapshot.cwdUri, snapshot.source, snapshot.generation, snapshot.hideWhitespace]);
  let batch = pendingBatches.get(key);
  if (!batch) {
    batch = { snapshot, reads: [] };
    pendingBatches.set(key, batch);
    window.setTimeout(() => flush(key), 40);
  }
  const queued = batch;
  return Promise.all(requests.map(request => new Promise<GitDiffContentResult>((resolve, reject) => {
    queued.reads.push({ request, resolve, reject });
  })));
}

function flush(key: string): void {
  const batch = pendingBatches.get(key)!;
  pendingBatches.delete(key);
  for (let offset = 0; offset < batch.reads.length; offset += 4) {
    const reads = batch.reads.slice(offset, offset + 4);
    const snapshot = batch.snapshot;
    const id = createClientCommandId();
    sendClientCommandAwaitResult({ type: "diff.git_file_contents", client_command_id: id, request_id: id,
      conversation_id: snapshot.conversationId, workspace: snapshot.workspaceRoot,
      environment_id: snapshot.environmentId, cwd_uri: snapshot.cwdUri,
      source: snapshot.source, snapshot_generation: snapshot.generation,
      requests: reads.map(read => read.request), max_object_bytes: 5 * 1024 * 1024,
    }, "diff.git_file_contents", { silent: true }).then(result => {
      if (!commandResultSucceeded(result)) throw new Error(result.message);
      const reply = result.data as ContentsReply;
      reads.forEach((read, index) => read.resolve(reply.status === "stale-snapshot"
        ? { type: "error", path: read.request.path, side: read.request.side, error: { type: "stale-snapshot" } }
        : reply.results[index]));
    }).catch(error => reads.forEach(read => read.reject(error)));
  }
}
