import { useI18n } from "../../i18n";
import { type ComponentProps } from "react";
import { MarkdownRenderer } from "../messages/MarkdownRenderer";
import { proposedPlanParts } from "../../lib/proposed-plan";

export function ProposedPlanAnswer({ failed = false, ...markdown }: ComponentProps<typeof MarkdownRenderer> & { failed?: boolean }) {
  const { t: tr } = useI18n();
  const parts = proposedPlanParts(markdown.content);
  if (!parts) return <MarkdownRenderer {...markdown} />;
  const complete = parts.complete && !markdown.isStreaming && !failed;
  return <>
    {parts.before.trim() && <MarkdownRenderer {...markdown} content={parts.before} isStreaming={false} />}
    <section className="assistant-proposed-plan" aria-label={tr("方案")}>
      <div className="assistant-proposed-plan-heading" data-markdown-copy="exclude"><span>{complete ? tr("方案") : markdown.isStreaming ? tr("正在制定方案") : tr("方案未完整生成")}</span></div>
      {parts.plan && <MarkdownRenderer {...markdown} content={parts.plan} isStreaming={markdown.isStreaming && !parts.complete} />}
      {!complete && !markdown.isStreaming && <details className="assistant-proposed-plan-original" data-markdown-copy="exclude"><summary>{tr("原始输出")}</summary><pre>{markdown.content}</pre></details>}
    </section>
    {parts.after.trim() && <MarkdownRenderer {...markdown} content={parts.after} />}
  </>;
}
