import { lazy, Suspense, useEffect, useState } from "react";
import type { GitDiffContentResult } from "../../protocol/workspace-types";
import { LoaderCircle } from "../../lib/icons";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import type { GitDiffSource } from "../../stores/types";

const PdfPreview = lazy(() => import("./DiffPdfPreview").then(module => ({ default: module.DiffPdfPreview })));
const bitmapExtensions = new Set(["avif", "bmp", "gif", "ico", "jpeg", "jpg", "png", "tif", "tiff", "webp"]);
const markdownExtensions = new Set(["markdown", "md", "mdown", "mdx", "mkd"]);
export type DiffPreviewKind = "diff" | "markdown" | "image" | "pdf";

/** Bitmap/PDF always preview; SVG/Markdown follow the review preference. */
export function diffPreviewKind(path: string, deleted: boolean, rich: boolean, source: GitDiffSource): DiffPreviewKind {
  const extension = path.split(".").at(-1)?.toLowerCase() ?? "";
  // Staged Markdown has no preview path: retain code there.
  if (rich && !deleted && source !== "staged" && markdownExtensions.has(extension)) return "markdown";
  if (bitmapExtensions.has(extension) || extension === "svg" && rich) return "image";
  if (extension === "pdf") return "pdf";
  return "diff";
}

function useVersionUrl(result: GitDiffContentResult | undefined): string | undefined {
  const [source, setSource] = useState<{ result: GitDiffContentResult; url: string }>();
  useEffect(() => {
    if (!result || result.type !== "success" || result.source_kind === "absent" || result.data_base64 === undefined) return;
    const binary = atob(result.data_base64), bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: result.media_type }));
    setSource({ result, url });
    return () => URL.revokeObjectURL(url);
  }, [result]);
  return source?.result === result ? source?.url : undefined;
}

function VersionImage({ url, loading, svg }: { url?: string; loading: boolean; svg: boolean }) {
  const [failed, setFailed] = useState(false);
  return <div className="mc-diff-version-image" data-svg={svg || undefined}>
    {loading ? <LoaderCircle size={16} className="animate-spin" aria-label="Loading image" />
      : !url || failed ? <span>No image</span> : <img src={url} alt="" onError={() => setFailed(true)} />}
  </div>;
}

export function GitDiffMediaPreview({ path, kind, results, loading, status, workspaceRoot, conversationId }: {
  path: string; kind: Exclude<DiffPreviewKind, "diff">; results?: GitDiffContentResult[]; loading: boolean;
  status: "new" | "deleted" | "changed"; workspaceRoot: string; conversationId: string;
}) {
  const old = results?.find(result => result.side === "old"), next = results?.find(result => result.side === "new");
  const oldUrl = useVersionUrl(old), newUrl = useVersionUrl(next);
  const pending = (result: GitDiffContentResult | undefined, url?: string) => loading
    || result?.type === "success" && result.source_kind !== "absent" && result.data_base64 !== undefined && !url;
  if (kind === "markdown") return loading
    ? <div className="mc-diff-version-preview"><LoaderCircle size={16} className="animate-spin" aria-label="Loading preview" /></div>
    : <div className="mc-diff-version-preview"><MarkdownRenderer content={next?.type === "success" ? next.lines!.join("") : ""}
        workspaceRoot={workspaceRoot} conversationId={conversationId} /></div>;
  if (kind === "pdf") return <Suspense fallback={null}><PdfPreview oldUrl={oldUrl} newUrl={newUrl} status={status}
    oldLoading={pending(old, oldUrl)} newLoading={pending(next, newUrl)} /></Suspense>;
  const svg = path.toLowerCase().endsWith(".svg");
  return <div className="mc-diff-version-images" data-new={status === "new" || undefined}>
    {status !== "new" && <VersionImage key={oldUrl ?? "old"} url={oldUrl} loading={pending(old, oldUrl)} svg={svg} />}
    <VersionImage key={newUrl ?? "new"} url={newUrl} loading={pending(next, newUrl)} svg={svg} />
  </div>;
}
