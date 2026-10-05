import { useEffect, useState } from "react";
import { Check, GitBranch, GitCommitHorizontal, GitPullRequest, Upload } from "lucide-react";
import { apiBase, authHeaders, errorMessageFromResponseText, fetchWithTimeout } from "../protocol/api";
import { showConfirm } from "../overlays/DialogService";
import { useAppStore } from "../stores";
import { openExternal } from "../desktop/runtime";
import { readLS, writeLS } from "../stores/shared-helpers";
import { safeJsonParse } from "../lib/safe-parse";
import { SelectMenu } from "../components/SelectMenu";
import { openSettings } from "../lib/settings-navigation";

interface DeliveryStatus {
  is_git_repo: boolean; branch: string; repo_root: string; remotes: string[]; upstream: string;
  commits: Array<{ hash: string; subject: string }>; detached: boolean; error?: string;
  github?: { eligible: boolean; host: string; available: boolean; authenticated: boolean | null; message: string };
}
export function GitDelivery({ workspaceRoot, stagedCount, onChanged, refreshKey = 0 }: { workspaceRoot: string; stagedCount: number; onChanged: () => void; refreshKey?: number }) {
  const [draft] = useState(() => safeJsonParse<Record<string, string>>(readLS("minicode.git.draft:" + workspaceRoot) ?? "{}", {}));
  const [status, setStatus] = useState<DeliveryStatus | null>(null);
  const [message, setMessage] = useState(draft.message ?? "");
  const [remote, setRemote] = useState(draft.remote ?? "");
  const [branch, setBranch] = useState(draft.branch ?? "");
  const [title, setTitle] = useState(draft.title ?? "");
  const [base, setBase] = useState(draft.base ?? "main");
  const [body, setBody] = useState(draft.body ?? "");
  useEffect(() => { writeLS("minicode.git.draft:" + workspaceRoot, JSON.stringify({ message, remote, branch, title, base, body })); }, [workspaceRoot, message, remote, branch, title, base, body]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [url, setUrl] = useState("");
  const [revision, setRevision] = useState(0);
  const [statusLoading, setStatusLoading] = useState(true);
  const endpoint = new URL("/api/workspace/git/delivery", apiBase());
  endpoint.searchParams.set("workspace_root", workspaceRoot);
  useEffect(() => {
    const controller = new AbortController();
    setStatusLoading(true);
    void fetchWithTimeout(endpoint, { signal: controller.signal, headers: authHeaders() }).then(async (response) => {
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      return response.json() as Promise<DeliveryStatus>;
    }).then((next) => { if (!controller.signal.aborted) { setStatus(next); setRemote((value) => next.remotes?.includes(value) ? value : next.remotes?.[0] ?? ""); } })
      .catch((reason) => { if (!controller.signal.aborted) { setStatus(null); setError(String(reason)); } })
      .finally(() => { if (!controller.signal.aborted) setStatusLoading(false); });
    return () => controller.abort();
  }, [workspaceRoot, revision, refreshKey]);
  const run = async (action: string) => {
    const branchAtClick = status?.branch ?? "";
    if (action === "push" || action === "draft_pr") {
      const confirmed = await showConfirm({ title: action === "push" ? "推送当前分支" : "创建草稿 PR",
        message: action === "push" ? "将 " + branchAtClick + " 推送到 " + remote + "。" : "从 " + branchAtClick + " 向 " + base + " 创建草稿 PR：\n" + title,
        confirmLabel: action === "push" ? "推送" : "创建草稿 PR" });
      if (!confirmed || useAppStore.getState().workingDirectory !== workspaceRoot) return;
    }
    setBusy(action); setError(""); setFeedback("");
    try {
      const response = await fetchWithTimeout(endpoint, { method: "POST", headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({ action, message, remote, branch, title, base, body, expected_branch: branchAtClick }) }, { timeoutMs: 130000 });
      if (!response.ok) throw new Error(errorMessageFromResponseText(await response.text(), response.statusText));
      const result = await response.json() as { message: string; url?: string };
      if (useAppStore.getState().workingDirectory !== workspaceRoot) return;
      setFeedback(result.message || "操作完成"); setUrl(result.url ?? "");
      if (action === "commit") setMessage("");
      setRevision((value) => value + 1); onChanged();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(""); }
  };
  const github = status?.github;
  const githubReady = !statusLoading && Boolean(github?.eligible && github.available && github.authenticated === true);
  const githubReason = statusLoading ? "正在读取 GitHub 连接状态…" : !github ? "GitHub 连接状态待确认。"
    : !github.eligible ? github.message || "当前仓库未关联 GitHub 远端。"
    : !github.available ? "GitHub 连接组件未就绪。"
    : github.authenticated === null ? github.message || "GitHub 连接状态待确认。"
    : !github.authenticated ? "连接 GitHub 后可创建草稿 PR。" : "";
  return <section className="mc-git-delivery" aria-label="Git 交付">
    {status?.is_git_repo === false ? <button type="button" disabled={Boolean(busy)} onClick={() => void run("init")}><GitBranch size={14} />初始化 Git 仓库</button> : <>
      <textarea aria-label="提交说明" placeholder="提交说明…" rows={3} value={message} onChange={(event) => setMessage(event.target.value)} disabled={Boolean(busy)} />
      <button type="button" disabled={!status || !message.trim() || !stagedCount || Boolean(busy)} onClick={() => void run("commit")}><GitCommitHorizontal size={14} />{busy === "commit" ? "提交中…" : "提交已暂存的 " + stagedCount + " 个文件"}</button>
      <details><summary>分支与推送</summary><small>{status?.repo_root}<br />{status?.upstream ? "跟踪 " + status.upstream : "尚未设置跟踪分支"}</small>
        <div className="mc-git-delivery-row"><input aria-label="新分支名称" placeholder="新分支名称" value={branch} onChange={(event) => setBranch(event.target.value)} /><button type="button" disabled={!status || !branch.trim() || Boolean(busy)} onClick={() => void run("branch")}>创建</button></div>
        <div className="mc-git-delivery-row"><SelectMenu ariaLabel="推送远端" value={remote} onValueChange={setRemote} disabled={Boolean(busy) || !status?.remotes?.length} style={{ flex: 1, minWidth: 0 }}>{!status?.remotes?.length && <option value="">没有远端</option>}{status?.remotes?.map((item) => <option key={item} value={item}>{item}</option>)}</SelectMenu>
          <button type="button" disabled={!remote || Boolean(busy) || status?.detached} onClick={() => void run("push")}><Upload size={13} />推送</button></div>
      </details>
      {!githubReady && <div className="mc-git-pr-connection"><p role="status">{githubReason}</p><button type="button" onClick={() => openSettings("workspaceGit")}>查看 GitHub 连接设置</button></div>}
      <details><summary>创建草稿 PR</summary><input aria-label="PR 标题" placeholder="标题" value={title} disabled={!githubReady || Boolean(busy)} onChange={(event) => setTitle(event.target.value)} /><input aria-label="PR 目标分支" placeholder="目标分支" value={base} disabled={!githubReady || Boolean(busy)} onChange={(event) => setBase(event.target.value)} />
        <textarea aria-label="PR 说明" placeholder="说明与验证结果" rows={4} value={body} disabled={!githubReady || Boolean(busy)} onChange={(event) => setBody(event.target.value)} /><button type="button" disabled={!githubReady || !title.trim() || !base.trim() || Boolean(busy) || status?.detached} onClick={() => void run("draft_pr")}><GitPullRequest size={14} />创建草稿 PR</button>
      </details>
      {status?.commits?.length ? <details><summary>最近提交</summary>{status.commits.map((commit) => <p key={commit.hash}><code>{commit.hash}</code> {commit.subject}</p>)}</details> : null}
    </>}
    {error && <p role="alert">{error}</p>}{feedback && <p role="status"><Check size={13} />{feedback}</p>}
    {url && <button type="button" onClick={() => void openExternal(url)}>打开草稿 PR</button>}
  </section>;
}
